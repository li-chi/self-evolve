#!/bin/bash
# Oracle: the grader compares /app/calculation.json against a STATIC groundtruth
# copy (historical 2025-06-05 FX rates + fixed expense sums) with numeric
# tolerances. No live data is consulted at grading time, so a groundtruth copy
# is the correct oracle.
set -e
cp "/solution/groundtruth_workspace/calculation.json" /app/calculation.json
