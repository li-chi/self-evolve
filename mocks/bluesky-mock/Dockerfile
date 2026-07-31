FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml server.py /app/
RUN pip install --no-cache-dir .

ENV BLUESKY_MOCK_STATE_DIR=/workspace/output/end_state/bluesky

ENTRYPOINT ["python", "/app/server.py"]
