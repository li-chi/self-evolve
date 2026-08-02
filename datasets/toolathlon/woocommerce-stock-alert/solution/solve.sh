#!/bin/bash
# Oracle: upsert low-stock products into the requisition sheet and email
# the purchasing manager, through the same tool surface the agent has.
set -e
/opt/toolathlon/.venv/bin/python /solution/solve.py
