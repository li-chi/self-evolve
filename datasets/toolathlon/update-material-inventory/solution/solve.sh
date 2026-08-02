#!/bin/bash
# Oracle: write material balances to the sheet and sync max-producible
# stock to WooCommerce through the same tool surface the agent has.
set -e
/opt/toolathlon/.venv/bin/python /solution/solve.py
