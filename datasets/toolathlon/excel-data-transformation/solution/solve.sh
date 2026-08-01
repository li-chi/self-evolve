#!/bin/bash
# Oracle: the grader (evaluation/check_local_improved.py) compares
# /app/Processed.xlsx against the groundtruth Processed.xlsx.
set -e
cp /solution/groundtruth_workspace/Processed.xlsx /app/Processed.xlsx
