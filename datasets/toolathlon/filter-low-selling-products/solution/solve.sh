#!/bin/bash
# Oracle: move low-selling products to Outlet/Clearance and email the
# subscribers, through the same tool surface the agent has. See solve.py.
set -e
/opt/toolathlon/.venv/bin/python /solution/solve.py
