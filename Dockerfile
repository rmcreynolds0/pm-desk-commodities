# Dockerfile — one image serves BOTH long-running services:
#   * engine    : scripts/xsec_scheduler.py  (rebalance monthly + mark daily)
#   * dashboard : streamlit run dashboard/xsec_live.py
# The service chooses its command in docker-compose.yml; keeping a single
# image means one build, one dependency set, no drift between the two.
#
# THESE POINT AT THE CROSS-SECTIONAL (PIVOT A) STRATEGY, NOT THE NG BOOK.
# The natural-gas strategy was retired after it failed its own week-1 regime
# gate (see docs/SUMMER_SUMMARY.md). Its scheduler (scripts/scheduler.py) and
# dashboard (dashboard/app.py) are still in the repo as a research record, but
# deploying them would put a strategy we deliberately killed back into
# production. If you change the CMD below, change it knowing that.
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
#
# Unbuffered (-u) so scheduler prints reach `docker compose logs` immediately.
# Without it Python block-buffers stdout when it is a pipe, and the logs stay
# empty for hours — which looks exactly like a hung container.
CMD ["python", "-u", "scripts/xsec_scheduler.py"]
