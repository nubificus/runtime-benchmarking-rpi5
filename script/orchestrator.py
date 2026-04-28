import argparse
import csv
import os
import requests
import subprocess
import time
from datetime import datetime, timezone
import json

POLL_INTERVAL = 0.5
TIMELINE_DURATION = 30.0


def sh(cmd):
    return subprocess.check_output(cmd, text=True).strip()


def run(cmd):
    subprocess.check_call(cmd)


def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def helm_deploy(ns, release, chart, set_args):
    cmd = ["helm", "upgrade", "--install", release, chart, "-n", ns, "--create-namespace"]
    for s in set_args:
        cmd += ["--set", s]
    run(cmd)


def desired_replicas(ns, deploy):
    return int(sh(["kubectl","-n",ns,"get","deploy",deploy,"-o","jsonpath={.spec.replicas}"]))


def get_pods(ns, selector):
    out = sh([
        "kubectl", "-n", ns, "get", "pods",
        "-l", selector,
        "-o", "json"
    ])
    data = json.loads(out)

    pods = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        ip = item.get("status", {}).get("podIP")

        ready = False
        if item.get("status", {}).get("phase") == "Running":
            for c in item.get("status", {}).get("conditions", []):
                if c.get("type") == "Ready" and c.get("status") == "True":
                    ready = True
                    break

        pods.append((name, ip, ready))

    return pods


def blackbox_probe(blackbox_url, target):
    r = requests.get(
        f"{blackbox_url}/probe",
        params={"module": "http_2xx", "target": target},
        timeout=2,
    )
    r.raise_for_status()

    for line in r.text.splitlines():
        if line.startswith("probe_success"):
            return float(line.split()[-1]) == 1.0

    return False


def write_http_timeline(args, timeline_http, desired):
    timeline_buckets = int(TIMELINE_DURATION / POLL_INTERVAL)

    if timeline_http:
        last_elapsed, last_available, _ = timeline_http[-1]
        last_recorded_bucket = int(last_elapsed / POLL_INTERVAL)
    else:
        last_available = 0
        last_recorded_bucket = 0

    final_bucket = max(last_recorded_bucket, timeline_buckets)

    for b in range(last_recorded_bucket + 1, final_bucket + 1):
        elapsed = round(b * POLL_INTERVAL, 3)
        remaining = max(0, desired - last_available)
        timeline_http.append((elapsed, last_available, remaining))

    path = "timeline_http_success.csv"
    file_exists = os.path.isfile(path)

    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "test_id", "runtime_class", "variant",
                "elapsed_seconds", "pods_http_available",
                "pods_remaining"
            ])

        for elapsed, available, remaining in timeline_http:
            w.writerow([
                args.test_id, args.runtime_class, args.variant,
                elapsed, available, remaining
            ])


def write_ready_timeline(args, timeline_ready, desired):
    timeline_buckets = int(TIMELINE_DURATION / POLL_INTERVAL)

    if timeline_ready:
        last_elapsed, last_ready = timeline_ready[-1]
        last_recorded_bucket = int(last_elapsed / POLL_INTERVAL)
    else:
        last_ready = 0
        last_recorded_bucket = 0

    final_bucket = max(last_recorded_bucket, timeline_buckets)

    for b in range(last_recorded_bucket + 1, final_bucket + 1):
        elapsed = round(b * POLL_INTERVAL, 3)
        timeline_ready.append((elapsed, last_ready))

    path = "timeline_pods_ready.csv"
    file_exists = os.path.isfile(path)

    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "test_id", "runtime_class", "variant",
                "elapsed_seconds", "pods_ready"
            ])

        for elapsed, ready_count in timeline_ready:
            w.writerow([
                args.test_id, args.runtime_class, args.variant,
                elapsed, ready_count
            ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy")
    ap.add_argument("--selector", required=True)
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--csv", default="test.csv")

    ap.add_argument("--chart")
    ap.add_argument("--release")
    ap.add_argument("--set", dest="set_args", action="append", default=[])

    ap.add_argument("--blackbox", default="http://<NODE_IP>:<BLACKBOX_IP>")

    ap.add_argument("--test-id", required=True)
    ap.add_argument("--runtime-class", required=True)
    ap.add_argument("--variant", required=True)

    args = ap.parse_args()

    deploy_name = args.deploy or args.release
    if not deploy_name:
        raise SystemExit("Need --deploy or --release")

    POLL = POLL_INTERVAL
    TIMEOUT = 720

    set_args = list(args.set_args)

    t0 = time.time()

    if args.chart and args.release:
        helm_deploy(args.namespace, args.release, args.chart, set_args)

    desired = desired_replicas(args.namespace, deploy_name)
    deadline = time.time() + TIMEOUT

    seen_http_ok = {}
    seen_ready = {}

    timeline_http = []
    timeline_ready = []

    poll_index = 0
    ready_count = 0

    while time.time() < deadline:
        pods = get_pods(args.namespace, args.selector)

        elapsed_seconds = round(poll_index * POLL, 3)

        ready_count = 0

        for pod_name, pod_ip, pod_ready in pods:
            if pod_ready:
                ready_count += 1
                if pod_name not in seen_ready:
                    seen_ready[pod_name] = time.time()

            if not pod_ip:
                continue

            if pod_name in seen_http_ok:
                continue

            target = f"http://{pod_ip}:80/"

            try:
                if blackbox_probe(args.blackbox, target):
                    seen_http_ok[pod_name] = time.time()
            except Exception:
                pass

        http_count = len(seen_http_ok)
        remaining = max(0, desired - http_count)

        timeline_ready.append((elapsed_seconds, ready_count))
        timeline_http.append((elapsed_seconds, http_count, remaining))

        if http_count >= desired and ready_count >= desired:
            break

        poll_index += 1
        time.sleep(POLL)

    t1 = time.time()

    ok = len(seen_http_ok) >= desired and ready_count >= desired
    ready = ready_count

    ready_latencies = [ts - t0 for ts in seen_ready.values()]
    http_latencies = [ts - t0 for ts in seen_http_ok.values()]

    first_ready_latency_s = min(ready_latencies) if ready_latencies else None
    all_ready_latency_s = max(ready_latencies) if ready_latencies else None

    http_first_latency_s = min(http_latencies) if http_latencies else None
    http_all_latency_s = max(http_latencies) if http_latencies else None
    http_mean_latency_s = (
        sum(http_latencies) / len(http_latencies)
        if http_latencies else None
    )

    first_ready_to_all_ready_s = None
    deploy_to_first_reply_after_ready_s = None
    first_reply_to_all_reply_s = None

    if first_ready_latency_s is not None and all_ready_latency_s is not None:
        first_ready_to_all_ready_s = max(0.0, all_ready_latency_s - first_ready_latency_s)

    if http_first_latency_s is not None and first_ready_latency_s is not None:
        deploy_to_first_reply_after_ready_s = max(0.0, http_first_latency_s - first_ready_latency_s)

    if http_first_latency_s is not None and http_all_latency_s is not None:
        first_reply_to_all_reply_s = max(0.0, http_all_latency_s - http_first_latency_s)

    tt = all_ready_latency_s if all_ready_latency_s is not None else http_all_latency_s

    write_http_timeline(args, timeline_http, desired)
    write_ready_timeline(args, timeline_ready, desired)

    print(
        f"[{deploy_name}] "
        f"ready_latency={tt} "
        f"pods_ready={ready}/{desired} "
        f"pods_http_ok={len(seen_http_ok)}/{desired} "
        f"status={'OK' if ok else 'TIMEOUT'} "
        f"http_first_latency_s={http_first_latency_s} "
        f"http_all_latency_s={http_all_latency_s} "
        f"http_mean_latency_s={http_mean_latency_s}"
    )

    file_exists = os.path.isfile(args.csv)

    with open(args.csv, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "start_time","end_time","deployment","test_id",
                "runtime_class","variant","selector",
                "desired_pods","ready_pods","all_ready_latency_seconds","success",
                "first_ready_latency_seconds",
                "http_first_latency_seconds","http_all_latency_seconds",
                "http_mean_latency_seconds",
                "deploy_to_first_ready_seconds","deploy_to_first_reply_after_ready_seconds",
                "first_ready_to_all_ready_seconds","first_reply_to_all_reply_seconds"
            ])

        w.writerow([
          utc(t0), utc(t1), deploy_name, args.test_id,
          args.runtime_class, args.variant, args.selector,
          desired, ready, round(tt, 6), ok,
          None if first_ready_latency_s is None else round(first_ready_latency_s, 6),
          None if http_first_latency_s is None else round(http_first_latency_s, 6),
          None if http_all_latency_s is None else round(http_all_latency_s, 6),
          None if http_mean_latency_s is None else round(http_mean_latency_s, 6),
          None if first_ready_latency_s is None else round(first_ready_latency_s, 6),
          None if deploy_to_first_reply_after_ready_s is None else round(deploy_to_first_reply_after_ready_s, 6),
          None if first_ready_to_all_ready_s is None else round(first_ready_to_all_ready_s, 6),
          None if first_reply_to_all_reply_s is None else round(first_reply_to_all_reply_s, 6),
        ])

    if not ok:
        raise SystemExit(1)

    run(["helm", "uninstall", args.release, "-n", args.namespace])
    time.sleep(45)


if __name__ == "__main__":
    main()
