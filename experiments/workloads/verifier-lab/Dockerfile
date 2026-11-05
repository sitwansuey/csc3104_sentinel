FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY experiments/workloads/verifier-lab/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY experiments/workloads/verifier-lab/app.py .
USER 10001:10001
CMD ["python", "app.py"]
