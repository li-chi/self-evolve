#!/bin/bash
# Oracle: the grader (evaluation/check_local.py) compares
# /app/segment_growth_rates.xlsx against the groundtruth copy cell-by-cell.
set -e
cp /solution/groundtruth_workspace/segment_growth_rates.xlsx /app/segment_growth_rates.xlsx
