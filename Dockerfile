# MLOps Service Desk application image.
# Build and run everything (app + PostgreSQL) with: docker compose up -d --build
FROM python:3.12-slim

# tzdata lets TZ work, so ticket times are shown in local time instead of UTC
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mlops_service_desk.py chai_logo.png ./

# Run as a normal user, not root
RUN useradd --system --uid 10001 servicedesk
USER servicedesk

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)" || exit 1

CMD ["python", "mlops_service_desk.py"]
