FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py /app/services/
COPY services/observer/ /app/services/observer/
COPY services/coordinator/ /app/services/coordinator/
COPY shared/__init__.py shared/canonical.py shared/contracts.py shared/log.py shared/tracing.py /app/shared/
COPY runtime/__init__.py runtime/observer_pipeline.py runtime/observer_fleet.py runtime/otlp_receiver.py /app/runtime/
COPY runtime/remote_worker.py runtime/worker_http.py runtime/worker_transport.py runtime/service_identity.py runtime/fault_points.py /app/runtime/
COPY data/sentinel/samples/policy.json /app/data/sentinel/samples/policy.json
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENTRYPOINT ["python", "-m", "services.observer.cli"]
