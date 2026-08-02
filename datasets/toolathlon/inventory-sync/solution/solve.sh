#!/bin/bash
# Oracle: perform the inventory sync through the same tool surface the
# agent has (mcp-tool -> woocommerce MCP server). See solve.py.
set -e
/opt/toolathlon/.venv/bin/python /solution/solve.py
