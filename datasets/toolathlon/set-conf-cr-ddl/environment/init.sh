#!/bin/bash
# Upstream-equivalent initialize_workspace, executed at container start.
TASK=set-conf-cr-ddl
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
  export GCAL_MOCK_STATE_DIR=/var/lib/mock-state/gcalendar
  mkdir -p /var/lib/mock-state/gcalendar
  export MAIL_MOCK_STATE_DIR=/var/lib/mock-state/mail
  mkdir -p /var/lib/mock-state/mail
  mkdir -p /app/emails_download
  mkdir -p /app/emails_export
  export NETREDIRECT_MAP='{"www.googleapis.com": "http://127.0.0.1:10200/__svc/googleapis", "oauth2.googleapis.com": "http://127.0.0.1:10200/__svc/goauth"}'
  export PYTHONPATH=/opt/mocks/netredirect:$PYTHONPATH
  if [ -f /opt/harbor-mcp/seeds/gcalendar_seed.json ] && [ ! -f /var/lib/mock-state/gcalendar/state.json ]; then
    cp /opt/harbor-mcp/seeds/gcalendar_seed.json /var/lib/mock-state/gcalendar/state.json
  fi

  # Service backends that must be listening before preprocess runs.
  /opt/toolathlon/.venv/bin/python /opt/mocks/api-facade/server.py --port 10200 > /var/run/toolathlon/api-facade.log 2>&1 &
  for _ in $(seq 1 50); do
    if /opt/toolathlon/.venv/bin/python -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1', 10200)))" 2>/dev/null; then break; fi
    sleep 0.2
  done
  /opt/toolathlon/.venv/bin/python /opt/mocks/poste-mock/mailserver.py --state-dir /var/lib/mock-state/mail --users /opt/toolathlon/configs/users_data.json --smtp-port 1587 --imap-port 1143 > /var/run/toolathlon/mailserver.log 2>&1 &
  for _ in $(seq 1 50); do
    if /opt/toolathlon/.venv/bin/python -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1', 1143)))" 2>/dev/null; then break; fi
    sleep 0.2
  done

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
