FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY experiments/workloads/verifier-lab/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY experiments/workloads/verifier-lab/app.py .
USER 10001:10001
CMD ["python", "app.py"]
