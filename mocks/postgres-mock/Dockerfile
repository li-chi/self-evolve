FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml server.py /app/
RUN pip install --no-cache-dir .

ENV PG_MOCK_STATE_DIR=/workspace/output/end_state/postgres

ENTRYPOINT ["python", "/app/server.py"]
