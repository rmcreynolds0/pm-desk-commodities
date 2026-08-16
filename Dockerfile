# Dockerfile — one image serves BOTH long-running services:
#   * engine    : scripts/scheduler.py  (fires decide/mark jobs on schedule)
#   * dashboard : streamlit run dashboard/app.py
# The service chooses its command in docker-compose.yml; keeping a single
# image means one build, one dependency set, no drift between the two.
FROM python:3.11-slim

# tzdata: the scheduler converts to US/Eastern via zoneinfo, which needs the
# system tz database on slim images.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency metadata first so `pip install` layers cache across code
# edits (rebuilds after a code change take seconds, not minutes).
COPY pyproject.toml README.md ./
COPY src ./src

# [live] -> ib_async for IBKR; [dashboard] -> streamlit + plotly.
RUN pip install --no-cache-dir -e ".[live,dashboard]"

# The rest of the repo (configs, scripts, dashboard, docs).
COPY config ./config
COPY scripts ./scripts
COPY dashboard ./dashboard

# data/ (ledger, logs) is a VOLUME in compose — state must outlive the
# container. Nothing is baked into the image.
CMD ["python", "scripts/scheduler.py"]
