FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY experiments/workloads/verifier-lab/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY experiments/workloads/verifier-lab/app.py .
USER 10001:10001
CMD ["python", "app.py"]
