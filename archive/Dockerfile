# Build from the repo root: docker build -f archive/Dockerfile .
# (the compose file does this for you)
FROM python:3.12-slim

WORKDIR /app
COPY common ./common
COPY archive ./archive
RUN pip install --no-cache-dir ./common ./archive

ENTRYPOINT ["gs-archive"]
