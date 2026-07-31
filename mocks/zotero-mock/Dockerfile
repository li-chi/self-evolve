FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml server.py /app/
RUN pip install --no-cache-dir .

ENV ZOTERO_MOCK_STATE_DIR=/workspace/output/end_state/zotero

ENTRYPOINT ["python", "/app/server.py"]
