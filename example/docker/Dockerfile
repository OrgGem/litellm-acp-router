# syntax=docker/dockerfile:1
FROM node:24.22.1-bookworm-slim AS codex
ARG CODEX_VERSION=0.167.0
RUN npm install --prefix /opt/codex --omit=dev @openai/codex@${CODEX_VERSION}

FROM python:3.12-slim-bookworm
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /opt/codex /opt/codex
RUN ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex \
    && useradd --uid 10001 --create-home agent \
    && mkdir -p /home/agent/.codex /app \
    && chown -R agent:agent /home/agent /app

ARG PLUGIN_WHEEL=litellm_agent_rpc-0.2.0.dev0-py3-none-any.whl
ARG PLUGIN_EXTRAS=""
COPY dist/${PLUGIN_WHEEL} /tmp/wheels/
RUN pip install --no-cache-dir "/tmp/wheels/${PLUGIN_WHEEL}${PLUGIN_EXTRAS}" \
    && rm -rf /tmp/wheels

# Optional TUI-managed Compose overlay persists metadata here, separate from CLI auth.
RUN mkdir -p /home/agent/.local/state/agent-rpc \
    && chown -R agent:agent /home/agent/.local

ENV HOME=/home/agent \
    CODEX_HOME=/home/agent/.codex \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    LITELLM_TELEMETRY=False
WORKDIR /app
USER agent
EXPOSE 4000
ENTRYPOINT ["agent-rpc"]
CMD ["serve", "--config", "/app/config.json", "--host", "0.0.0.0", "--port", "4000"]
