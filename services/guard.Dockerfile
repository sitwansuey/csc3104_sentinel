# Offline/file-only CLI. A host installation of kubectl is required for live reads.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py services/guard.py services/guard_cli.py services/guard_adapters.py /app/services/
COPY services/guard_common.py services/guard_decisions.py services/guard_eligibility.py services/guard_resources.py services/guard_preview.py /app/services/
COPY shared/__init__.py shared/contracts.py /app/shared/
USER 10001:10001
ENTRYPOINT ["python", "-m", "services.guard_cli"]
