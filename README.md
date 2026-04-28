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
