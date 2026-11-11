# Offline/file-only CLI. A host installation of kubectl is required for live reads.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py /app/services/
COPY services/guard/ /app/services/guard/
COPY shared/__init__.py shared/canonical.py shared/contracts.py /app/shared/
USER 10001:10001
ENTRYPOINT ["python", "-m", "services.guard.cli"]
