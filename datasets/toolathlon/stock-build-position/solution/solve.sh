#!/bin/bash
# Oracle: live-data grader (recomputes from the same day's yfinance open
# prices), so instead of copying groundtruth we solve the task for real from
# live data. See build_position.py.
set -e
python3 /solution/build_position.py
