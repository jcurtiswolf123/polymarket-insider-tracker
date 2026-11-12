# Polywatch on Fly: tracker + Postgres + Redis + the bot jobs on one machine. State lives on /data.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends postgresql redis-server tzdata ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev && uv pip install cryptography  # fav_live signing; not in the lock
ENV HOME=/data PATH=/app/.venv/bin:$PATH TZ=America/Los_Angeles PYTHONUNBUFFERED=1
CMD ["bash", "/app/deploy/fly/run.sh"]
