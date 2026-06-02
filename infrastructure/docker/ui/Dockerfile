# Stage 1: Build the React app
FROM public.ecr.aws/docker/library/node:22-alpine@sha256:8094c002d08262dba12645a3b4a15cd6cd627d30bc782f53229a2ec13ee22a00 AS build

WORKDIR /app

# Install dependencies first (layer caching)
COPY src/ui/package.json src/ui/package-lock.json src/ui/.npmrc ./
RUN npm ci --production=false

# Copy source and build
COPY src/ui/ ./
RUN npm run build

# Stage 2: Serve with lightweight static server
FROM public.ecr.aws/docker/library/node:22-alpine@sha256:8094c002d08262dba12645a3b4a15cd6cd627d30bc782f53229a2ec13ee22a00

RUN npm install -g serve

WORKDIR /app
COPY --from=build --chown=node:node /app/build ./build

USER node

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "-qO-", "http://localhost:8080/"]

# serve -s enables SPA mode (all routes -> index.html)
CMD ["serve", "-s", "build", "-l", "8080"]
