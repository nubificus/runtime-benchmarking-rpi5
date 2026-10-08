# Python runtime sandbox

FastAPI server from the agent-sandbox
[python-runtime-sandbox](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/python-runtime-sandbox)
example (commit `fb9d818`, Apache-2.0, see `LICENSE`). It listens on port `8888`:

- `GET /`: health check, used as the HTTP readiness probe target
- `POST /execute`: runs a shell command in the sandbox

`Dockerfile`, `main.py` and `requirements.*` are unchanged from upstream.
`Dockerfile.urunc` is the same file with the
[bunny](https://github.com/nubificus/bunny) frontend directive as its first line, so that
the build attaches a Linux kernel and urunit for urunc.

## Build

Build on the Raspberry Pi (arm64), or anywhere that produces arm64 images, and push
to a registry the cluster can pull from.

Standard image (`runc`, `runsc`, `kata-*`):

```bash
docker build -t REGISTRY/python-runtime-sandbox:TAG .
docker push REGISTRY/python-runtime-sandbox:TAG
```

urunc image (QEMU + Linux by default):

```bash
docker build -f Dockerfile.urunc -t REGISTRY/python-runtime-sandbox-urunc:TAG .
docker push REGISTRY/python-runtime-sandbox-urunc:TAG
```

To target another VMM, add `LABEL "com.urunc.unikernel.hypervisor"="firecracker"` (or
`cloud-hypervisor`) to `Dockerfile.urunc`. Firecracker needs a block-based snapshotter
(e.g. `devmapper`, `blockfile`).

The server only imports FastAPI, but `requirements.txt` also installs pandas,
scikit-learn and lightgbm for the agent's tasks, so the image is large. Trim
`requirements.in` and re-run `pip-compile --generate-hashes` for a smaller rootfs.

## Quick check

With `--set service=true`, each Sandbox gets a headless Service named after it:

```bash
kubectl run client --rm -it --image=curlimages/curl --restart=Never -- \
  curl -s -X POST http://<SANDBOX_NAME>:8888/execute \
  -H 'Content-Type: application/json' -d '{"command":"uname -r"}'
```

On urunc the reported kernel differs from the host's. `kubectl exec` and
`kubectl port-forward` are not available there.
