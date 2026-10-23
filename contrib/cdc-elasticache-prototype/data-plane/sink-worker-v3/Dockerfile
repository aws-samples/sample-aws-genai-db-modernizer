# CDC-cache v3 Sink Worker (Final Architecture)
# Reads: Broker Valkey (Streams, no TLS)
# Writes: Target Valkey (Hashes, TLS, ElastiCache)
FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sink_worker_streams.py .
RUN mkdir -p /opt/cdc-cache
COPY transforms.json /opt/cdc-cache/transforms.json

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8081/health || exit 1

EXPOSE 8081

CMD ["python3", "sink_worker_streams.py"]
