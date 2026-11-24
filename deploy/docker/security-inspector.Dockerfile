FROM python:3.15-alpine3.23@sha256:32f06114a111c2467804d06194c5584f16804a78cfaf427fb1fcbb0b3391d0f1
RUN apk upgrade --no-cache && python -m pip uninstall -y pip setuptools wheel
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py services/grpc_inspection.py services/
COPY runtime/__init__.py runtime/security_inspector.py runtime/security_store.py runtime/
COPY shared/__init__.py shared/log.py shared/
USER 10001:10001
ENTRYPOINT ["python", "-m", "runtime.security_inspector"]
