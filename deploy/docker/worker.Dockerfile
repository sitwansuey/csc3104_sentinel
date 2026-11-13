# One image for every networked worker role: python -m runtime.worker_service.
# Pick the role with --role and mount its trusted configuration at /config/worker.json.
# Secrets come only from environment variables that the configuration names.
# The kubectl stage is the demo runner's, line for line, so both images share it.
FROM --platform=$BUILDPLATFORM golang:1.27.3-alpine@sha256:b8c9b82affbbb82a020bbcf998dad2ba9516944ac7e883524642a481a9505e9c AS kubectl-build
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.37.1
ARG KUBECTL_COMMIT=f78e722310e50bcaca9276be22276d9e91d91308
RUN apk add --no-cache git
WORKDIR /kubernetes
# Rebuild the same Kubernetes client version with security-patched Go modules.
# Only the static binary and its license enter the final runtime image.
RUN git init . && git remote add origin https://github.com/kubernetes/kubernetes.git \
    && git fetch --depth 1 origin "refs/tags/${KUBECTL_VERSION}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${KUBECTL_COMMIT}"
# Build the unchanged upstream CLI entrypoint against published client modules.
# This avoids linking the Kubernetes server's root module into a client binary.
WORKDIR /client
RUN cp /kubernetes/cmd/kubectl/kubectl.go main.go \
    && go mod init sentinel.local/kubectl-build \
    && go get k8s.io/kubectl@v0.37.1 k8s.io/component-base@v0.37.1 k8s.io/client-go@v0.37.1 \
       golang.org/x/net@v0.62.0 golang.org/x/text@v0.43.0 \
    && go mod tidy \
    && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} GOMAXPROCS=4 \
       go build -p 4 -mod=mod -trimpath \
       -ldflags="-s -w -X k8s.io/component-base/version.gitMajor=1 -X k8s.io/component-base/version.gitMinor=37 -X k8s.io/component-base/version.gitVersion=${KUBECTL_VERSION} -X k8s.io/component-base/version.gitCommit=${KUBECTL_COMMIT} -X k8s.io/component-base/version.gitTreeState=clean" \
       -o /out/kubectl .

FROM python:3.12-alpine3.23@sha256:3d0ca66b723a0ff2a7a6e424cc2d01443e1b7c75ffcc110145f75d0f4daf6d63
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Sentinel worker" \
      org.opencontainers.image.revision=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates-bundle
# The agent Planner's only third-party packages, exactly as locked: every file is hash-checked
# and only prebuilt wheels are accepted, so nothing is compiled from an unpinned source.
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock \
    && python -m pip uninstall -y pip setuptools wheel
COPY --from=kubectl-build /out/kubectl /usr/local/bin/kubectl
COPY --from=kubectl-build /kubernetes/LICENSE /usr/local/share/licenses/kubernetes-LICENSE
WORKDIR /app
COPY shared/ shared/
COPY services/ services/
COPY runtime/ runtime/
COPY agent/ agent/
COPY runbooks/ runbooks/
COPY data/sentinel/samples/rca-config.json data/sentinel/samples/rca-config.json
# Agent mode exports Observer snapshots with the harness's exporter; only that module chain is copied.
COPY experiments/__init__.py experiments/
COPY experiments/boutique/__init__.py experiments/boutique/
COPY experiments/boutique/harness/__init__.py experiments/boutique/harness/agent_live.py experiments/boutique/harness/durable_case.py experiments/boutique/harness/scenarios.py experiments/boutique/harness/scoring.py experiments/boutique/harness/targets.py experiments/boutique/harness/
COPY experiments/recovery/__init__.py experiments/recovery/live.py experiments/recovery/
# State lives only in /data (mount a volume there); /tmp is the only other path written, so the
# root filesystem can be read-only. The runtime needs no shell or BusyBox, so both are removed.
RUN mkdir /data /config && chown 10001:10001 /data \
    && apk del --no-cache ca-certificates alpine-baselayout busybox busybox-binsh \
    && ! apk info -e busybox \
    && python -c "import os, pathlib, ssl; pathlib.Path('/tmp').mkdir(exist_ok=True); os.chmod('/tmp', 0o1777); assert pathlib.Path(ssl.get_default_verify_paths().cafile).is_file()" \
    && python -c "import pandas, runtime.worker_service, services.planner.agent_provider, experiments.boutique.harness.agent_live"
USER 10001:10001
VOLUME ["/data"]
# /readyz is served on 127.0.0.1 inside the container only (runtime/worker_health.py), so the probe runs
# here too. Keep --health-port 8080 when you override the command. Kubernetes ignores HEALTHCHECK: use an
# exec probe with the same command (docs/remote-workers.md). /readyz counts every successful Coordinator
# exchange as contact, lease renewals during a long model call included, so a Planner mid-inference stays
# healthy while it renews (default --ready-seconds 60, renewals at least every 5 s); it turns unhealthy once
# no exchange has succeeded for the readiness window, or a preflight, clock or store check fails. A worker
# whose own component fails on every pass (a model call that crashes, a cluster it cannot reach) still
# exchanges claims and retries, so it stays healthy here: watch its retry and back-off log lines for that.
# Timing: Docker reports unhealthy at most interval*retries + timeout = 10*3 + 5 = 35 s after /readyz starts
# answering 503 (45 s, retries*(interval + timeout), if every probe times out instead); docs/remote-workers.md
# and tests/test_worker_dockerfile.py hold the same number. The 60 s start period covers startup and the
# first preflight. With interval 30 s, a live run saw /readyz 503 62 s and Docker unhealthy 126 s after the
# Coordinator stopped (C-WORKER-CONTAINER-READINESS); the 10 s timing has not been measured live.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3)"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "runtime.worker_service"]
CMD ["--role", "planner", "--config", "/config/worker.json", "--health-port", "8080"]
