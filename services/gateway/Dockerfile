# syntax=docker/dockerfile:1
FROM python:3.12-slim

# No .pyc files, unbuffered stdout/stderr (logs reach `docker logs` immediately)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1) Dependencies first: this layer is reused as long as requirements.txt doesn't change
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) Application code (changes often -> last)
COPY app/ ./app/

# Non-root numeric user (numeric so Kubernetes runAsNonRoot can verify it)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8000

# slim has no curl: use Python for the healthcheck (used by docker compose, ignored by k8s)
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly (graceful shutdown)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
