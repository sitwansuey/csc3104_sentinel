FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS frontend
WORKDIR /frontend
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build && npm test

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
       golang.org/x/net@v0.60.0 golang.org/x/text@v0.42.0 \
    && go mod tidy \
    && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} GOMAXPROCS=4 \
       go build -p 4 -mod=mod -trimpath \
       -ldflags="-s -w -X k8s.io/component-base/version.gitMajor=1 -X k8s.io/component-base/version.gitMinor=37 -X k8s.io/component-base/version.gitVersion=${KUBECTL_VERSION} -X k8s.io/component-base/version.gitCommit=${KUBECTL_COMMIT} -X k8s.io/component-base/version.gitTreeState=clean" \
       -o /out/kubectl .

FROM python:3.12-alpine3.23@sha256:3d0ca66b723a0ff2a7a6e424cc2d01443e1b7c75ffcc110145f75d0f4daf6d63
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Sentinel Boutique demo runner" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.revision=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates-bundle \
    && python -m pip uninstall -y pip setuptools wheel
COPY --from=kubectl-build /out/kubectl /usr/local/bin/kubectl
COPY --from=kubectl-build /kubernetes/LICENSE /usr/local/share/licenses/kubernetes-LICENSE
WORKDIR /app
COPY services/ services/
COPY runtime/ runtime/
COPY shared/ shared/
COPY scripts/ scripts/
COPY infra/ infra/
COPY samples/ samples/
COPY runbooks/ runbooks/
COPY evaluation/ evaluation/
COPY tests/ tests/
COPY .github/scripts/ .github/scripts/
COPY .github/workflows/ .github/workflows/
COPY dashboard/ dashboard/
COPY --from=frontend /frontend/dist/ dashboard/dist/
# A cached frontend stage can predate freshly checked-out source timestamps.
# These exact sources were already built above; stamp that build without Node.
RUN touch dashboard/dist/index.html \
    && python -c "from scripts.dashboard import ensure_frontend; ensure_frontend()" \
    && mkdir /data && chown 10001:10001 /data \
    && apk del --no-cache ca-certificates alpine-baselayout busybox busybox-binsh \
    && ! apk info -e busybox \
    && python -c "import os, pathlib, ssl; pathlib.Path('/tmp').mkdir(exist_ok=True); os.chmod('/tmp', 0o1777); assert pathlib.Path(ssl.get_default_verify_paths().cafile).is_file()"
# The runtime executes Python/kubectl directly; no shell or BusyBox is needed.
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "scripts/test_boutique_live.py"]
