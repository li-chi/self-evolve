#!/bin/bash
# Wraps the original Toolathlon grader (exit code 0 = pass) into Harbor's
# reward contract. The grader extracts gt_files.tar.gz from
# --groundtruth_workspace, so /tests doubles as the groundtruth dir.

python3 /tests/grader.py --agent_workspace /app --groundtruth_workspace /tests
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
