#!/bin/bash
# Upstream grader, invoked exactly as upstream does (uv run -m ...) under the
# pinned uv environment, with the runtime launch_time recorded by init.sh.
# evaluation/ + groundtruth ship only in /tests (post-agent upload).
LAUNCH_TIME="$(cat /var/run/toolathlon/launch_time)"
export PYTHONPATH=/tests/pkg

# Upstream's preprocess and grader share one groundtruth_workspace: values
# preprocess generates at run time (randomised bucket / log-bucket names)
# are read back by the grader from that directory. The ported layout splits
# them, so overlay whatever preprocess wrote onto the shipped copy.
GT=/tests/pkg/tasks/finalpool/logical-datasets-collection/groundtruth_workspace
if [ -d /var/run/toolathlon/preprocess_generated ]; then
  mkdir -p "$GT"
  cp -a /var/run/toolathlon/preprocess_generated/. "$GT/" 2>/dev/null || true
fi

cd /opt/toolathlon
uv run -m tasks.finalpool.logical-datasets-collection.evaluation.main \
  --agent_workspace /app \
  --groundtruth_workspace /tests/pkg/tasks/finalpool/logical-datasets-collection/groundtruth_workspace \
  --res_log_file /tests/traj_placeholder.json \
  --launch_time "$LAUNCH_TIME"

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
