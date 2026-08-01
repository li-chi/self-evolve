#!/bin/bash
# Upstream-equivalent initialize_workspace, executed at container start.
TASK=machine-operating
STATE=/var/run/toolathlon
mkdir -p "$STATE" "$STATE/preprocess_generated"

init() {
  set -e
  # Upstream stamps the weekday too, and graders parse it with
  # "%Y-%m-%d %H:%M:%S %A" — keep the format identical.
  LAUNCH_TIME="$(date '+%Y-%m-%d %H:%M:%S %A')"
  printf '%s' "$LAUNCH_TIME" > "$STATE/launch_time"

  # Mock backends: shared state for the agent's MCP
  # tools and for upstream preprocess/grader code.
  export GCP_MOCK_STATE_DIR=/var/lib/mock-state/gcp
  export GCP_MOCK_PROJECT_ID=mcp-bench0606
  mkdir -p /var/lib/mock-state/gcp
  export PYTHONPATH=/opt/sdk-shims/gcp:$PYTHONPATH
  if [ -f /opt/harbor-mcp/seeds/gcp_seed.json ] && [ ! -f /var/lib/mock-state/gcp/state.json ]; then
    cp /opt/harbor-mcp/seeds/gcp_seed.json /var/lib/mock-state/gcp/state.json
  fi

  SRC=/opt/toolathlon/tasks/finalpool/$TASK/initial_workspace
  if [ -d "$SRC" ]; then
    cp -a "$SRC/." /app/
  fi

  if [ -f "/opt/toolathlon/tasks/finalpool/$TASK/preprocess/main.py" ]; then
    # Upstream preprocess writes generated fixtures next to the grader, in
    # groundtruth_workspace/; the dir is stripped from the agent image, so
    # recreate it for the run and remove it again below.
    mkdir -p "/opt/toolathlon/tasks/finalpool/$TASK/groundtruth_workspace"
    cd /opt/toolathlon
    uv run -m tasks.finalpool.$TASK.preprocess.main \
      --agent_workspace /app --launch_time "$LAUNCH_TIME" \
      > "$STATE/preprocess.log" 2>&1
    status=$?
    printf '%s' "$status" > "$STATE/preprocess_status"
    if [ "$status" -ne 0 ]; then
      echo "preprocess exited $status — see $STATE/preprocess.log" >&2
      return "$status"
    fi
  fi

  # specifical_inialize_for_mcp equivalent
  :
  # Resolve the MCP server scoping flags exactly as upstream does: after
  # preprocess, from the task's token_key_session.py (which may read names
  # preprocess just generated).
  /opt/toolathlon/.venv/bin/python /opt/harbor-mcp/render_servers.py \
    "$TASK" /opt/harbor-mcp/servers.template.json /opt/harbor-mcp/servers.json \
    >> "$STATE/preprocess.log" 2>&1
  
  GT_DIR="/opt/toolathlon/tasks/finalpool/$TASK/groundtruth_workspace"
  if [ -d "$GT_DIR" ]; then
    # Preprocess writes into the grader's groundtruth directory. Only carry
    # over the RESOURCE NAMES the grader has to agree on (randomised bucket /
    # log-bucket names). Anything else preprocess computed there is the
    # task's answer and must not survive into the agent's container — that
    # material has to be re-derived verifier-side (tests/pre_grade.sh).
    for f in "$GT_DIR"/*name*.txt "$GT_DIR"/task_date*; do
      [ -f "$f" ] && cp -a "$f" "$STATE/preprocess_generated/"
    done
    rm -rf "$GT_DIR"
  fi
  # Upstream runs preprocess OUTSIDE the task container, so the agent never
  # sees preprocess sources or task fixtures — several of them compute the
  # expected answer (e.g. calculate_groundtruth.py). The port has to stage
  # them in the image to run them, so remove the tree once it has served
  # its purpose. Everything the agent legitimately gets is already in /app.
  rm -rf "/opt/toolathlon/tasks/finalpool/$TASK"

  touch "$STATE/ready"
}

# NOTE: errexit is disabled for a function on the left of `||`, so init()
# reports failure through its return code and we deliberately do NOT create
# the readiness file — the healthcheck then fails loudly instead of handing
# the agent a half-initialised environment.
if ! init; then
  echo "INIT FAILED — see $STATE/preprocess.log" >&2
fi

exec "$@"
