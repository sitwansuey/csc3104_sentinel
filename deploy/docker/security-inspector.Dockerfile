FROM python:3.12-alpine3.23@sha256:3d0ca66b723a0ff2a7a6e424cc2d01443e1b7c75ffcc110145f75d0f4daf6d63
RUN apk upgrade --no-cache && python -m pip uninstall -y pip setuptools wheel
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py services/grpc_inspection.py services/
COPY runtime/__init__.py runtime/security_inspector.py runtime/security_store.py runtime/
COPY shared/__init__.py shared/log.py shared/
USER 10001:10001
ENTRYPOINT ["python", "-m", "runtime.security_inspector"]
