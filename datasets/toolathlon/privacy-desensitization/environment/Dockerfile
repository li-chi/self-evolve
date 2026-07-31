FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils tar ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY workspace/ /app/
RUN mkdir -p /app/desensitized_documents
