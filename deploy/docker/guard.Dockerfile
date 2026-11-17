# Offline/file-only CLI. A host installation of kubectl is required for live reads.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py services/image_security.py /app/services/
COPY services/guard/ /app/services/guard/
COPY runtime/__init__.py runtime/image_scans.py /app/runtime/
COPY shared/__init__.py shared/actions.py shared/canonical.py shared/contracts.py /app/shared/
USER 10001:10001
ENTRYPOINT ["python", "-m", "services.guard.cli"]
