FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py /app/services/
COPY services/observer/ /app/services/observer/
COPY services/coordinator/ /app/services/coordinator/
COPY shared/__init__.py shared/contracts.py /app/shared/
COPY runtime/__init__.py runtime/observer_pipeline.py runtime/observer_fleet.py runtime/otlp_receiver.py /app/runtime/
COPY data/sentinel/samples/policy.json /app/data/sentinel/samples/policy.json
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENTRYPOINT ["python", "-m", "services.observer.cli"]
