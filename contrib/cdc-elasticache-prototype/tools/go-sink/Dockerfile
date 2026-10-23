# CDC-cache Go Sink Worker — Broker Valkey (Streams) → Target Valkey (Hashes)
# Multi-stage: build a static binary, run it on a minimal base with curl for health checks.
FROM golang:1.23 AS build
WORKDIR /src
# Cache deps first
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/cdc-cache-go-sink .

# Runtime: alpine (has a shell + we add curl for the ECS container health check)
FROM alpine:3.20
RUN apk add --no-cache curl ca-certificates
COPY --from=build /out/cdc-cache-go-sink /usr/local/bin/cdc-cache-go-sink
EXPOSE 8081
# WORKERS, BATCH_SIZE, BROKER_*, TARGET_*, STREAM_KEY, etc. are provided via env.
ENTRYPOINT ["/usr/local/bin/cdc-cache-go-sink"]
