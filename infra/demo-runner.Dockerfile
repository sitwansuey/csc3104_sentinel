FROM node:22-bookworm-slim AS frontend
WORKDIR /frontend
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build && npm test

FROM python:3.12-slim-bookworm
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.37.1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" -o /usr/local/bin/kubectl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" -o /tmp/kubectl.sha256 \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum --check \
    && chmod 755 /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /tmp/kubectl.sha256
WORKDIR /app
COPY services/ services/
COPY runtime/ runtime/
COPY shared/ shared/
COPY scripts/ scripts/
COPY infra/ infra/
COPY samples/ samples/
COPY tests/ tests/
COPY dashboard/ dashboard/
COPY --from=frontend /frontend/dist/ dashboard/dist/
# The frontend stage built these exact sources. Refresh its index timestamp so
# a cached build also gets stamped after a fresh checkout/archive extraction.
# Node/npm are absent at runtime.
RUN touch dashboard/dist/index.html \
    && python -c "from scripts.dashboard import ensure_frontend; ensure_frontend()" \
    && mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "scripts/test_boutique_live.py"]
