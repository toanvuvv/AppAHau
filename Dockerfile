FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data is the mounted volume (config.yaml, tokens, orders, logs). The app treats a
# missing config.yaml as "no accounts yet" and creates it on first save.
# `exec` makes gunicorn PID 1 so `docker stop` (SIGTERM) shuts down cleanly
# instead of being SIGKILLed (exit 137) after the grace period.
# 8 threads: dashboard stays responsive while background poll/sync jobs run.
CMD ["sh", "-c", "mkdir -p /data && exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 8 --timeout 120 --graceful-timeout 20 dashboard:app"]
