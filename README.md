# Runtime Benchmarking on Raspberry Pi 5 (K3s)

This repository contains the setup and scripts used to benchmark different container runtimes on a Raspberry Pi 5 using Kubernetes (K3s).

---

## Overview

The setup consists of:

### Node (Raspberry Pi 5)

- Single-node **K3s cluster**
- Blackbox Exporter deployed with Helm
- Runs the benchmark workload (C HTTP server)
- Deployments that use different `RuntimeClass` values:
  - `runc`
  - `runsc` (gVisor)
  - `kata` (QEMU / Firecracker)
  - `urunc` (Linux + unikernel variants)


---

## Repository structure

```text
charts/                     # Helm chart for HTTP server workload
script/orchestrator.py      # Benchmarking script
blackbox-config/            # Example configs for Blackbox
csv-data/                   # Benchmark metrics in CSV format
```
---

## RuntimeClass setup

Before running the benchmarks, make sure the required `RuntimeClass` resources are already deployed on the cluster.
The benchmark chart expects `runtimeClassName` to refer to an existing `RuntimeClass`.

You can verify the available runtime classes with:

```bash
kubectl get runtimeclass
```

---
## Workload deployment

The workload is deployed via Helm:

```text
charts/http-server-bench/
```

Each deployment is labeled for filtering.

### Required labels

```text
app=c-http-server
test_id=<test_id>
runtime_class=<runtime>
variant=<variant>
```

---

## Workload variants

The `normal` variant refers to the standard Linux container image used for non-unikernel runtimes.

It is used with:

- `runc`
- `runsc`
- `kata-qemu`
- `kata-fc`

---

## Runtimes

| Runtime | Variant       |
| ------- | ------------- |
| runc    | -             |
| kata    | qemu          |
| kata    | fc            |
| runsc   | -             |
| urunc   | unikraft/qemu |
| urunc   | linux/qemu    |
| urunc   | linux/fc      |
| urunc   | rumprun/spt   |
| urunc   | rumprun/hvt   |
| urunc   | mirage/spt    |
| urunc   | mirage/hvt    |

---

## Running a benchmark

Before you run a benchmark, you must replace `'NODE_IP:BLACKBOX_IP'` inside the orchestrator script.
Example command:

```bash
python3 script/orchestrator.py \
  --chart ./charts/http-server-bench \
  --release chttp-urunc-hvt-mirage-t001 \
  --namespace default \
  --set replicas=100 \
  --set runtimeClassName=urunc \
  --set image='REGISTRY/IMAGE:TAG' \
  --set testId=t001 \
  --set variant=hvt-mirage \
  --selector 'app=c-http-server,test_id=t001,runtime_class=urunc,variant=hvt-mirage' \
  --test-id t001 \
  --runtime-class urunc \
  --variant hvt-mirage
```

> Replace `'REGISTRY/IMAGE:TAG'` with a container image compatible with the selected runtime.
For urunc variants, this requires images built accordingly (using [bunny](https://github.com/nubificus/bunny)).


#### Important behavior

After each run, the script waits before starting the next one:

```bash
run(["helm", "uninstall", args.release, "-n", args.namespace])
time.sleep(45)
```

This delay ensures:

- old pods are fully deleted
- blackbox exporter stops probing old pods

If runs overlap, increase the sleep time.

### Metrics collected

The setup measures:

- Pod readiness (Kubernetes API)
- HTTP availability (blackbox exporter)
- Timeline of pods becoming ready/responding

All metrics are sampled every 0.5 seconds.

### Metrics for 100 replicas (except for kata deployments)

Reproducible metrics and timelines are available under:

```text
csv-data/
├── test.csv
├── timeline_http_success.csv
└── timeline_pods_ready.csv
```
---

## agent-sandbox benchmarking

The same workloads can be deployed through [agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
instead of a `Deployment`. Each replica becomes a `Sandbox` (one pod each), either created directly
or handed out by a `SandboxClaim` from a `SandboxWarmPool`.

```text
charts/agent-sandbox-bench/         # Helm chart: Sandbox / SandboxTemplate + SandboxWarmPool + SandboxClaim
script/agent_sandbox_orchestrator.py
workloads/python-runtime-sandbox/   # FastAPI example from agent-sandbox (port 8888)
```

### Prerequisites

1. The `RuntimeClass` resources from [RuntimeClass setup](#runtimeclass-setup).
   For urunc, the agent-sandbox [urunc example](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/urunc-sandbox)
   ships a `setup.sh --flavor k3s` that installs urunc-deploy and registers the `urunc` RuntimeClass.
2. The agent-sandbox controller with extensions (needed for `claim` mode):

```bash
export VERSION=v1.0.2
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml
kubectl -n agent-sandbox-system rollout status deployment/agent-sandbox-controller
kubectl get crd | grep agents.x-k8s.io
```

3. Blackbox Exporter, as for the `Deployment` runs (NodePort `30113`).

### Modes

| Mode      | Objects created                                                  | What is measured                                  |
| --------- | ---------------------------------------------------------------- | ------------------------------------------------- |
| `sandbox` | `replicas` × `Sandbox`                                           | Cold start through the Sandbox controller         |
| `claim`   | `SandboxTemplate`, `SandboxWarmPool`, `replicas` × `SandboxClaim` | Claim → Ready, warm (adopted) and/or cold          |

In `claim` mode the template and warm pool are installed first and the script waits until
`warmPoolReplicas` sandboxes are ready (untimed). The timer starts when the claims are created.

- `--warm-pool-replicas` >= `--replicas`: every claim adopts a warm sandbox.
- `--warm-pool-replicas 0`: every claim cold-starts from the template.
- In between: a mix. The summary records how many were `warm` / `cold`
  (from the `agents.x-k8s.io/launch-type` label on each Sandbox).

### Running a benchmark

Replace `<NODE_IP>` in `--blackbox` (or the default in the script) with the node IP.
The selector must match the chart labels (`app` is `c-http-server` by default,
`python-runtime` with `values-python.yaml`).

C HTTP server, bare Sandboxes on urunc:

```bash
python3 script/agent_sandbox_orchestrator.py \
  --chart ./charts/agent-sandbox-bench \
  --release as-urunc-hvt-mirage-t001 \
  --mode sandbox \
  --replicas 100 \
  --set runtimeClassName=urunc \
  --set image='REGISTRY/IMAGE:TAG' \
  --set testId=t001 \
  --set variant=hvt-mirage \
  --selector 'app=c-http-server,test_id=t001,runtime_class=urunc,variant=hvt-mirage' \
  --blackbox http://<NODE_IP>:30113 \
  --test-id t001 \
  --runtime-class urunc \
  --variant hvt-mirage
```

C HTTP server, 100 claims against a 100-sandbox warm pool on runc:

```bash
python3 script/agent_sandbox_orchestrator.py \
  --chart ./charts/agent-sandbox-bench \
  --release as-runc-claim-t001 \
  --mode claim \
  --replicas 100 \
  --warm-pool-replicas 100 \
  --set runtimeClassName=runc \
  --set image='REGISTRY/IMAGE:TAG' \
  --set testId=t001 \
  --set variant=normal \
  --selector 'app=c-http-server,test_id=t001,runtime_class=runc,variant=normal' \
  --blackbox http://<NODE_IP>:30113 \
  --test-id t001 \
  --runtime-class runc \
  --variant normal
```

Python runtime example (see [workloads/python-runtime-sandbox](workloads/python-runtime-sandbox/README.md)
for building the images), cold claims on urunc:

```bash
python3 script/agent_sandbox_orchestrator.py \
  --chart ./charts/agent-sandbox-bench \
  --values ./charts/agent-sandbox-bench/values-python.yaml \
  --release as-urunc-python-t001 \
  --mode claim \
  --replicas 20 \
  --warm-pool-replicas 0 \
  --port 8888 \
  --set runtimeClassName=urunc \
  --set image='REGISTRY/python-runtime-sandbox-urunc:TAG' \
  --set testId=t001 \
  --set variant=qemu-linux \
  --selector 'app=python-runtime,test_id=t001,runtime_class=urunc,variant=qemu-linux' \
  --blackbox http://<NODE_IP>:30113 \
  --test-id t001 \
  --runtime-class urunc \
  --variant qemu-linux
```

urunc sizes the microVM from the container limits (default 256 MiB / 1 vCPU). To change it, add e.g.
`--set resources.limits.memory=512Mi --set resources.limits.cpu=1`.

### Behavior

- Readiness is the `Ready` condition of the `Sandbox` (`sandbox` mode) or `SandboxClaim` (`claim` mode).
- HTTP availability is probed through Blackbox Exporter at `http://<podIP>:<--port><--path>`,
  using the pod IP from the object's status. Probes run concurrently.
- `elapsed_seconds` in the timelines is wall-clock time since `t0`, sampled every 0.5 s.
- As in `orchestrator.py`, `t0` is taken right before `helm upgrade --install`.
- The release is always uninstalled (also on timeout). The script then waits for the
  selected pods to disappear and sleeps `--cooldown` seconds (default 45).

### Output

| File                                  | Content                                               |
| ------------------------------------- | ----------------------------------------------------- |
| `agent_sandbox_test.csv`              | One summary row per run (latencies, warm/cold counts) |
| `timeline_sandbox_ready.csv`          | Ready objects over time                               |
| `timeline_sandbox_http_success.csv`   | Sandboxes answering HTTP over time                    |

All rows carry `test_id`, `runtime_class`, `variant` and `mode`.

---
