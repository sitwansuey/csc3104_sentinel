FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py services/observer.py services/k8s_observer_adapter.py services/observer_kubernetes.py services/observer_cli.py services/observer_telemetry.py services/coordinator.py /app/services/
COPY shared/__init__.py shared/contracts.py /app/shared/
COPY runtime/__init__.py runtime/observer_pipeline.py runtime/observer_fleet.py runtime/otlp_receiver.py /app/runtime/
COPY samples/policy.json /app/samples/policy.json
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENTRYPOINT ["python", "-m", "services.observer_cli"]
