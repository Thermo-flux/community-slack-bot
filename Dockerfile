# Gateway image: the Slack bot process and its core. It holds no LLM key; the gateway reaches the
# separate voice container over HTTP, on the private network in docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system goose \
    && useradd --system --gid goose --home-dir /app --shell /usr/sbin/nologin goose

WORKDIR /app

# Pinned, with upper bounds. Keep in step with pyproject.toml.
RUN pip install "PyYAML==6.0.*" "slack_bolt==1.*" "tzdata>=2024.1,<2027"

COPY goose/ goose/
COPY config/ config/
COPY slack/ slack/

# The data directory is a volume (see docker-compose.yml). The image itself stays read-only friendly.
RUN mkdir -p /app/data && chown goose:goose /app/data

USER goose

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "goose.gateway.app", "--version"]

CMD ["python", "-m", "goose.gateway.app", "--config", "config/goose.yaml"]
