FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/__init__.py /app/services/
COPY services/coordinator/ /app/services/coordinator/
COPY shared/__init__.py shared/canonical.py shared/contracts.py /app/shared/
COPY runtime/__init__.py runtime/service_identity.py runtime/worker_http.py runtime/worker_transport.py /app/runtime/
COPY runtime/approval_http.py runtime/coordinator_server.py /app/runtime/
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8766 8767
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "runtime.coordinator_server"]
