#!/bin/bash
# Oracle: the grader regex-checks /app/analysis.md against fixed expected
# numbers; the groundtruth analysis.md satisfies all checks.
set -e
cp /solution/groundtruth_workspace/analysis.md /app/analysis.md
echo "groundtruth analysis.md installed"
