FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py /app/services/
COPY services/coordinator/ /app/services/coordinator/
COPY services/executor/__init__.py services/executor/delivery_store.py services/executor/store.py /app/services/executor/
COPY shared/__init__.py shared/actions.py shared/canonical.py shared/contracts.py shared/http.py shared/log.py shared/tracing.py /app/shared/
COPY runtime/__init__.py runtime/service_identity.py runtime/worker_http.py runtime/worker_transport.py /app/runtime/
COPY runtime/approval_http.py runtime/coordinator_server.py runtime/fault_points.py /app/runtime/
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8766 8767
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "runtime.coordinator_server"]
