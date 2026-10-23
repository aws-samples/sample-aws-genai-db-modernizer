# CDC-cache — Debezium Server (Streams sink; offset + schema history in the broker)
# Option B: offsets live in the broker Valkey, so NO SQLite/EFS/jdbc jar needed.
FROM quay.io/debezium/server:3.0.6.Final

# Env-templated config; Quarkus resolves ${VAR:default} at runtime.
# DB credentials are injected by ECS from the Secrets Manager secret (not baked in).
COPY application.properties /debezium/config/application.properties

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8080/q/health || exit 1

EXPOSE 8080
