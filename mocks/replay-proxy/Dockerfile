FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml server.py /app/
COPY replay /app/replay
RUN pip install --no-cache-dir .

ENV REPLAY_PROXY_STATE_DIR=/workspace/output/end_state/replay_proxy
ENV REPLAY_PROXY_MISS_POLICY=error

ENTRYPOINT ["python", "/app/server.py"]
