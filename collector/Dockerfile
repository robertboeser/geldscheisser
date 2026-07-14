# Build from the repo root: docker build -f collector/Dockerfile .
# (the compose file does this for you)
FROM python:3.12-slim

WORKDIR /app
COPY common ./common
COPY collector ./collector
RUN pip install --no-cache-dir ./common ./collector

ENTRYPOINT ["gs"]
