import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from orchestrator import (
    POLL_INTERVAL,
    TIMELINE_DURATION,
    blackbox_probe,
    helm_deploy,
    run,
    sh,
    utc,
)

SANDBOXES = "sandboxes.agents.x-k8s.io"
CLAIMS = "sandboxclaims.extensions.agents.x-k8s.io"
WARMPOOLS = "sandboxwarmpools.extensions.agents.x-k8s.io"

LAUNCH_TYPE_LABEL = "agents.x-k8s.io/launch-type"

PROBE_WORKERS = 32
WARM_POOL_TIMEOUT = 1200
TEARDOWN_TIMEOUT = 300


def get_items(ns, resource, selector):
    out = sh(["kubectl", "-n", ns, "get", resource, "-l", selector, "-o", "json"])
    return json.loads(out).get("items", [])


def is_ready(item):
    for c in item.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready" and c.get("status") == "True":
            return True
    return False


def pod_ip(mode, item):
    # Sandbox.status.podIPs vs SandboxClaim.status.sandbox.podIPs
    status = item.get("status", {})
    if mode == "claim":
        status = status.get("sandbox", {})
    ips = status.get("podIPs") or []
    return ips[0] if ips else None


def sandbox_name(mode, item):
    if mode == "claim":
        return item.get("status", {}).get("sandbox", {}).get("name")
    return item["metadata"]["name"]


def wait_warm_pool(ns, name, replicas):
    if replicas == 0:
        return
    deadline = time.time() + WARM_POOL_TIMEOUT
    while time.time() < deadline:
        ready = sh([
            "kubectl", "-n", ns, "get", WARMPOOLS, name,
            "-o", "jsonpath={.status.readyReplicas}",
        ])
        if int(ready or 0) >= replicas:
            return
        time.sleep(1)
    raise SystemExit(f"warm pool {name} did not reach {replicas} ready sandboxes")


def launch_types(ns, names):
    out = sh(["kubectl", "-n", ns, "get", SANDBOXES, "-o", "json"])
    labels = {
        s["metadata"]["name"]: s["metadata"].get("labels", {}).get(LAUNCH_TYPE_LABEL)
        for s in json.loads(out).get("items", [])
    }
    types = [labels.get(n) for n in names]
    return types.count("warm"), types.count("cold")


def wait_pods_gone(ns, selector):
    deadline = time.time() + TEARDOWN_TIMEOUT
    while time.time() < deadline:
        if not sh(["kubectl", "-n", ns, "get", "pods", "-l", selector, "-o", "name"]):
            return
        time.sleep(1)


def pad_timeline(timeline, desired):
    # Hold the last value until TIMELINE_DURATION so runs line up when plotted.
    if not timeline:
        timeline.append((0.0, 0, desired))
    last = timeline[-1]
    elapsed = last[0]
    while elapsed + POLL_INTERVAL <= TIMELINE_DURATION:
        elapsed = round(elapsed + POLL_INTERVAL, 3)
        timeline.append((elapsed,) + last[1:])


def write_timeline(path, args, header, timeline):
    file_exists = os.path.isfile(path)

    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["test_id", "runtime_class", "variant", "mode", "elapsed_seconds"] + header)
        for row in timeline:
            w.writerow([args.test_id, args.runtime_class, args.variant, args.mode] + list(row))


def probe(blackbox_url, target):
    try:
        return blackbox_probe(blackbox_url, target)
    except Exception:
        return False


def r6(v):
    return None if v is None else round(v, 6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chart", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--set", dest="set_args", action="append", default=[])
    ap.add_argument("--values", dest="values_files", action="append", default=[])
    ap.add_argument("--selector", required=True)

    ap.add_argument("--mode", choices=["sandbox", "claim"], default="sandbox")
    ap.add_argument("--replicas", type=int, required=True)
    ap.add_argument("--warm-pool-replicas", type=int, default=0)

    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--path", default="/")
    ap.add_argument("--blackbox", default="http://<NODE_IP>:30113")

    ap.add_argument("--csv", default="agent_sandbox_test.csv")
    ap.add_argument("--timeline-ready", default="timeline_sandbox_ready.csv")
    ap.add_argument("--timeline-http", default="timeline_sandbox_http_success.csv")
    ap.add_argument("--timeout", type=int, default=720)
    ap.add_argument("--cooldown", type=int, default=45)

    ap.add_argument("--test-id", required=True)
    ap.add_argument("--runtime-class", required=True)
    ap.add_argument("--variant", required=True)

    args = ap.parse_args()

    set_args = list(args.set_args) + [
        f"mode={args.mode}",
        f"replicas={args.replicas}",
        f"warmPoolReplicas={args.warm_pool_replicas}",
    ]
    helm_args = []
    for v in args.values_files:
        helm_args += ["-f", v]

    resource = CLAIMS if args.mode == "claim" else SANDBOXES
    desired = args.replicas

    try:
        if args.mode == "claim":
            # Provision template + pool first (untimed), then create the claims.
            helm_deploy(args.namespace, args.release, args.chart,
                        set_args + ["replicas=0"], helm_args)
            wait_warm_pool(args.namespace, args.release, args.warm_pool_replicas)

        t0 = time.time()
        helm_deploy(args.namespace, args.release, args.chart, set_args, helm_args)
        deadline = t0 + args.timeout

        seen_ready = {}
        seen_http_ok = {}
        sandbox_names = {}

        timeline_ready = []
        timeline_http = []

        ready_count = 0

        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            while time.time() < deadline:
                poll_start = time.time()
                items = get_items(args.namespace, resource, args.selector)

                ready_count = 0
                targets = []

                for item in items:
                    name = item["metadata"]["name"]
                    if is_ready(item):
                        ready_count += 1
                        seen_ready.setdefault(name, time.time())
                    sb = sandbox_name(args.mode, item)
                    if sb:
                        sandbox_names[name] = sb

                    ip = pod_ip(args.mode, item)
                    if ip and name not in seen_http_ok:
                        targets.append((name, f"http://{ip}:{args.port}{args.path}"))

                urls = [url for _, url in targets]
                results = pool.map(lambda url: probe(args.blackbox, url), urls)
                for (name, _), success in zip(targets, results):
                    if success:
                        seen_http_ok[name] = time.time()

                elapsed = round(poll_start - t0, 3)
                http_count = len(seen_http_ok)
                timeline_ready.append((elapsed, ready_count, max(0, desired - ready_count)))
                timeline_http.append((elapsed, http_count, max(0, desired - http_count)))

                if http_count >= desired and ready_count >= desired:
                    break

                time.sleep(max(0.0, POLL_INTERVAL - (time.time() - poll_start)))

        t1 = time.time()
        ok = len(seen_http_ok) >= desired and ready_count >= desired

        warm, cold = launch_types(args.namespace, list(sandbox_names.values()))

        ready_latencies = [ts - t0 for ts in seen_ready.values()]
        http_latencies = [ts - t0 for ts in seen_http_ok.values()]

        first_ready = min(ready_latencies) if ready_latencies else None
        all_ready = max(ready_latencies) if ready_latencies else None
        http_first = min(http_latencies) if http_latencies else None
        http_all = max(http_latencies) if http_latencies else None
        http_mean = sum(http_latencies) / len(http_latencies) if http_latencies else None

        first_ready_to_all_ready = None
        first_reply_after_ready = None
        first_reply_to_all_reply = None

        if first_ready is not None:
            first_ready_to_all_ready = max(0.0, all_ready - first_ready)
        if http_first is not None and first_ready is not None:
            first_reply_after_ready = max(0.0, http_first - first_ready)
        if http_first is not None:
            first_reply_to_all_reply = max(0.0, http_all - http_first)

        pad_timeline(timeline_ready, desired)
        pad_timeline(timeline_http, desired)
        write_timeline(args.timeline_ready, args, ["ready", "remaining"], timeline_ready)
        write_timeline(args.timeline_http, args, ["http_available", "remaining"], timeline_http)

        print(
            f"[{args.release}] mode={args.mode} "
            f"ready={ready_count}/{desired} "
            f"http_ok={len(seen_http_ok)}/{desired} "
            f"warm={warm} cold={cold} "
            f"status={'OK' if ok else 'TIMEOUT'} "
            f"all_ready_latency_s={all_ready} "
            f"http_first_latency_s={http_first} "
            f"http_all_latency_s={http_all} "
            f"http_mean_latency_s={http_mean}"
        )

        file_exists = os.path.isfile(args.csv)

        with open(args.csv, "a", newline="") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow([
                    "start_time", "end_time", "release", "test_id",
                    "runtime_class", "variant", "mode", "selector",
                    "desired", "ready", "http_ok",
                    "warm_pool_replicas", "warm_launches", "cold_launches", "success",
                    "first_ready_latency_seconds", "all_ready_latency_seconds",
                    "http_first_latency_seconds", "http_all_latency_seconds",
                    "http_mean_latency_seconds",
                    "first_reply_after_ready_seconds",
                    "first_ready_to_all_ready_seconds", "first_reply_to_all_reply_seconds",
                ])
            w.writerow([
                utc(t0), utc(t1), args.release, args.test_id,
                args.runtime_class, args.variant, args.mode, args.selector,
                desired, ready_count, len(seen_http_ok),
                args.warm_pool_replicas, warm, cold, ok,
                r6(first_ready), r6(all_ready),
                r6(http_first), r6(http_all), r6(http_mean),
                r6(first_reply_after_ready),
                r6(first_ready_to_all_ready), r6(first_reply_to_all_reply),
            ])
    finally:
        run(["helm", "uninstall", args.release, "-n", args.namespace, "--wait"])
        wait_pods_gone(args.namespace, args.selector)
        time.sleep(args.cooldown)

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
