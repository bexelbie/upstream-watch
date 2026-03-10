# ABOUTME: Container image for running the upstream watch checker.
# ABOUTME: Minimal Python image with no additional dependencies.

FROM python:3.13-slim

WORKDIR /app
COPY check.py .

# Config and state live on a bind-mounted volume
VOLUME /data

ENV UPSTREAM_WATCH_CONFIG=/data/config.json

ENTRYPOINT ["python3", "/app/check.py"]
