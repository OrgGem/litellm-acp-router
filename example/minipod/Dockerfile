ARG CODEX_SOURCE=install-codex
FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS node-base

FROM node-base AS install-codex
ARG CODEX_VERSION=0.167.0
RUN npm install --prefix /opt/codex --omit=dev --no-audit --no-fund "@openai/codex@${CODEX_VERSION}"

FROM ${CODEX_SOURCE} AS codex-source
FROM node-base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=codex-source /opt/codex /opt/codex
RUN ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex \
    && install -d -o node -g node /workspace

USER node
WORKDIR /workspace
CMD ["sleep", "infinity"]
