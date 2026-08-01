#!/bin/bash
# Runs the original Toolathlon grader exactly as upstream does
# (python -m tasks.finalpool.<task>.evaluation.main) and maps its
# exit code onto Harbor's reward contract.
export PYTHONPATH=/tests/pkg:/opt/toolathlon
LAUNCH_TIME="$(cat /tests/launch_time.txt)"
# Graders that read the launch time out of the result log (rather than the
# CLI flag) get it in upstream's shape; no trajectory content is invented.
RES_LOG=/tmp/res_log.json
printf '{"config": {"launch_time": "%s"}}' "$LAUNCH_TIME" > "$RES_LOG"

python -m tasks.finalpool.sales-accounting.evaluation.main \
  --agent_workspace /app \
  --groundtruth_workspace /tests/pkg/tasks/finalpool/sales-accounting/groundtruth_workspace \
  --res_log_file "$RES_LOG" \
  --launch_time "$LAUNCH_TIME"

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
