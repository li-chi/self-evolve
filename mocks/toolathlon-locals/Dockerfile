FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml server.py /app/
RUN pip install --no-cache-dir .

# Default points at the standard mcp-env workspace mount. Override with
# -e TOOLATHLON_LOCALS_WORKSPACE=/some/path or -e AGENT_WORKSPACE=...
ENV TOOLATHLON_LOCALS_WORKSPACE=/workspace

ENTRYPOINT ["python", "/app/server.py"]
