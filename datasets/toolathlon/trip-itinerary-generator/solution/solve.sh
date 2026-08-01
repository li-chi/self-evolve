#!/bin/bash
# Default oracle: install the upstream groundtruth workspace as the agent's
# output. Correct for graders that only compare workspace files; tasks that
# also require service-side state need those calls added here (through the
# same tool surface the agent has).
set -e
if [ -d /solution/groundtruth_workspace ]; then
  cp -a /solution/groundtruth_workspace/. /app/
  echo "oracle: groundtruth workspace installed"
else
  echo "oracle: no groundtruth_workspace shipped — implement this task's oracle"
  exit 1
fi
