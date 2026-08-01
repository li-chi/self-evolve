#!/bin/bash
# Upstream grader, invoked exactly as upstream does (uv run -m ...) under the
# pinned uv environment, with the runtime launch_time recorded by init.sh.
# evaluation/ + groundtruth ship only in /tests (post-agent upload).
LAUNCH_TIME="$(cat /var/run/toolathlon/launch_time)"
export PYTHONPATH=/tests/pkg
export HF_MOCK_STATE_DIR=/var/lib/mock-state/huggingface
mkdir -p /var/lib/mock-state/huggingface
export NETREDIRECT_MAP='{"huggingface.co": "http://127.0.0.1:10200/__svc/hf"}'
export PYTHONPATH=/opt/mocks/netredirect:$PYTHONPATH
# Upstream's preprocess and grader share one groundtruth_workspace: values
# preprocess generates at run time (randomised bucket / log-bucket names)
# are read back by the grader from that directory. The ported layout splits
# them, so overlay whatever preprocess wrote onto the shipped copy.
GT=/tests/pkg/tasks/finalpool/huggingface-upload/groundtruth_workspace
if [ -d /var/run/toolathlon/preprocess_generated ]; then
  mkdir -p "$GT"
  cp -a /var/run/toolathlon/preprocess_generated/. "$GT/" 2>/dev/null || true
fi

# Tasks whose groundtruth is computed from the seeded service state derive
# it here, at verify time, so the answer never exists inside the container
# while the agent is running.
if [ -x /tests/pre_grade.sh ]; then
  /tests/pre_grade.sh || echo "pre_grade.sh failed" >&2
fi

cd /opt/toolathlon
uv run -m tasks.finalpool.huggingface-upload.evaluation.main \
  --agent_workspace /app \
  --groundtruth_workspace /tests/pkg/tasks/finalpool/huggingface-upload/groundtruth_workspace \
  --res_log_file /tests/traj_placeholder.json \
  --launch_time "$LAUNCH_TIME"

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
