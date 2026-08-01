#!/usr/bin/env python3
"""Generate a Harbor task from a Toolathlon task — v2 (fidelity contract).

Contract: identical verifier + identical initial environment to official
Toolathlon; only the service backend (real vs mock) may differ.

How fidelity is achieved:
  - Environment image = official lockon0927/toolathlon-task-image (via
    toolathlon-harbor-base:v2, which adds the uv-synced harness at
    /opt/toolathlon).
  - Preprocess runs AT CONTAINER START (entrypoint), exactly like upstream's
    initialize_workspace: copy initial_workspace -> run
    `uv run -m tasks.finalpool.<task>.preprocess.main` with the REAL launch
    time -> create MCP-specific dirs (memory/, arxiv_local_storage/,
    .playwright_output/) per needed_mcp_servers. A healthcheck gates the agent
    on /var/run/toolathlon/ready.
  - The agent image ships the task subtree WITHOUT evaluation/ and
    groundtruth_workspace/ (upstream's artifact-guard equivalent).
  - Verifier = upstream grader verbatim, invoked exactly like upstream
    (`uv run -m tasks.finalpool.<task>.evaluation.main`) under the pinned uv
    env, with the recorded runtime launch_time. Graders that consume
    --res_log_file get a placeholder; the generator WARNS so the port owner
    audits that the grader is None/{}-safe (else the task needs a trajectory
    bridge and must be flagged).

Usage: python3 tools/port_task.py <task-name> [--toolathlon PATH] [--force]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import mock_track

PREAMBLE = (
    "Your workspace directory is `/app`. When a relative path is mentioned, "
    "resolve it against this workspace directory.\n\n"
)

PLACEHOLDERS = {
    "!!<<<<||||workspace_dir||||>>>>!!": "/app",
    "!!<<<<||||workspace_dir_rela||||>>>>!!": ".",
    "!!<<<<||||current_working_dir||||>>>>!!": "/app",
}

TASK_TOML = """schema_version = "1.3"

[task]
name = "toolathlon/{name}"
description = {description}
keywords = ["toolathlon", {kw}]

[metadata]
toolathlon_task = "{name}"
toolathlon_split = "finalpool"
needed_mcp_servers = {servers}

artifacts = ["/app"]

[agent]
timeout_sec = 3600.0

[verifier]
timeout_sec = 600.0

[environment]
build_timeout_sec = 1200.0
cpus = 2
memory_mb = 4096
network_mode = "public"
workdir = "/app"

[environment.healthcheck]
command = "test -f /var/run/toolathlon/ready"
interval_sec = 2.0
timeout_sec = 10.0
start_period_sec = 5.0
start_interval_sec = 2.0
retries = 60
"""

DOCKERFILE = """FROM toolathlon-harbor-base:v3

# Task subtree WITHOUT evaluation/ and groundtruth_workspace/ (artifact guard):
# preprocess + fixtures live where upstream expects them, under the harness root.
COPY task/ /opt/toolathlon/tasks/finalpool/{name}/
COPY init.sh /opt/harbor-init/init.sh
RUN chmod +x /opt/harbor-init/init.sh

WORKDIR /app
ENTRYPOINT ["/opt/harbor-init/init.sh"]
"""

DOCKERFILE_MOCK = """FROM toolathlon-harbor-base:v3

# Task subtree WITHOUT evaluation/ and groundtruth_workspace/ (artifact guard):
# preprocess + fixtures live where upstream expects them, under the harness root.
COPY task/ /opt/toolathlon/tasks/finalpool/{name}/

# Mock wiring: the agent-facing MCP bridge config and the initial mock state.
COPY mcp/servers.template.json /opt/harbor-mcp/servers.template.json
COPY mock_seed/ /opt/harbor-mcp/seeds/

COPY init.sh /opt/harbor-init/init.sh
RUN chmod +x /opt/harbor-init/init.sh

WORKDIR /app
ENTRYPOINT ["/opt/harbor-init/init.sh"]
"""

INIT_SH = """#!/bin/bash
# Upstream-equivalent initialize_workspace, executed at container start.
TASK={name}
STATE=/var/run/toolathlon
mkdir -p "$STATE" "$STATE/preprocess_generated"

init() {
  set -e
  # Upstream stamps the weekday too, and graders parse it with
  # "%Y-%m-%d %H:%M:%S %A" — keep the format identical.
  LAUNCH_TIME="$(date '+%Y-%m-%d %H:%M:%S %A')"
  printf '%s' "$LAUNCH_TIME" > "$STATE/launch_time"

{mock_setup}

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
    uv run -m tasks.finalpool.$TASK.preprocess.main \\
      --agent_workspace /app --launch_time "$LAUNCH_TIME" \\
      > "$STATE/preprocess.log" 2>&1
    status=$?
    printf '%s' "$status" > "$STATE/preprocess_status"
    if [ "$status" -ne 0 ]; then
      echo "preprocess exited $status — see $STATE/preprocess.log" >&2
      return "$status"
    fi
  fi

  # specifical_inialize_for_mcp equivalent
{mcp_dirs}
{mock_post}
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
"""

TEST_SH = """#!/bin/bash
# Upstream grader, invoked exactly as upstream does (uv run -m ...) under the
# pinned uv environment, with the runtime launch_time recorded by init.sh.
# evaluation/ + groundtruth ship only in /tests (post-agent upload).
LAUNCH_TIME="$(cat /var/run/toolathlon/launch_time)"
export PYTHONPATH=/tests/pkg
{mock_env}
# Upstream's preprocess and grader share one groundtruth_workspace: values
# preprocess generates at run time (randomised bucket / log-bucket names)
# are read back by the grader from that directory. The ported layout splits
# them, so overlay whatever preprocess wrote onto the shipped copy.
GT=/tests/pkg/tasks/finalpool/{name}/groundtruth_workspace
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
uv run -m tasks.finalpool.{name}.evaluation.main \\
  --agent_workspace /app \\
  --groundtruth_workspace /tests/pkg/tasks/finalpool/{name}/groundtruth_workspace \\
  --res_log_file /tests/traj_placeholder.json \\
  --launch_time "$LAUNCH_TIME"

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
"""

SOLVE_SH = """#!/bin/bash
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
"""

# Runs after preprocess on mock tasks: preprocess may write generated
# fixtures into groundtruth_workspace (upstream shares one repo dir between
# preprocess and grader). Those must not reach the agent, so the values are
# stashed for the port owner and the directory is removed from the image.
MOCK_POST = """
# Resolve the MCP server scoping flags exactly as upstream does: after
# preprocess, from the task's token_key_session.py (which may read names
# preprocess just generated).
/opt/toolathlon/.venv/bin/python /opt/harbor-mcp/render_servers.py \\
  "$TASK" /opt/harbor-mcp/servers.template.json /opt/harbor-mcp/servers.json \\
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
"""

MCP_DIR_MAP = {
    "arxiv_local": "arxiv_local_storage",
    "memory": "memory",
    "xmind": "xmind",
    "playwright_with_chunk": ".playwright_output",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--toolathlon", default=os.path.expanduser("~/Projects/Toolathlon"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "datasets", "toolathlon"))
    ap.add_argument("--force", action="store_true", help="overwrite, preserving an existing solution/ and mock_seed/")
    ap.add_argument("--mock", default="",
                    help="comma-separated services to run on the mock track "
                         f"(known: {','.join(sorted(mock_track.MOCK_SPECS))})")
    args = ap.parse_args()

    task = args.task
    src = os.path.join(args.toolathlon, "tasks", "finalpool", task)
    if not os.path.isdir(src):
        sys.exit(f"no such task: {src}")
    dst = os.path.abspath(os.path.join(args.out, task))

    mocks = [m.strip() for m in args.mock.split(",") if m.strip()]
    for m in mocks:
        if m not in mock_track.MOCK_SPECS:
            sys.exit(f"unknown mock service: {m}")

    saved_solution = None
    saved_seed = None
    if os.path.exists(dst):
        if not args.force:
            sys.exit(f"already exists: {dst} (use --force)")
        sol = os.path.join(dst, "solution")
        if os.path.exists(sol):
            saved_solution = dst + ".solution.bak"
            if os.path.exists(saved_solution):
                shutil.rmtree(saved_solution)
            shutil.move(sol, saved_solution)
        seed = os.path.join(dst, "environment", "mock_seed")
        if os.path.exists(seed):
            saved_seed = dst + ".mock_seed.bak"
            if os.path.exists(saved_seed):
                shutil.rmtree(saved_seed)
            shutil.move(seed, saved_seed)
        shutil.rmtree(dst)

    cfg = json.load(open(os.path.join(src, "task_config.json")))
    servers = cfg.get("needed_mcp_servers", [])

    # environment: task subtree (no evaluation/, no groundtruth) + init.sh
    env_task = os.path.join(dst, "environment", "task")
    shutil.copytree(src, env_task, ignore=shutil.ignore_patterns(
        "__pycache__", ".DS_Store", "__MACOSX", "evaluation", "groundtruth_workspace"))
    mcp_dirs = "".join(
        f"  mkdir -p /app/{d}\n"
        for s, d in MCP_DIR_MAP.items() if s in servers) or "  :\n"

    tokens = mock_track.read_task_tokens(src) if mocks else {}
    if mocks:
        daemons = mock_track.daemon_lines(mocks)
        mock_setup = ("  # Mock backends: shared state for the agent's MCP\n"
                      "  # tools and for upstream preprocess/grader code.\n  "
                      + mock_track.mock_env_exports(mocks).replace("\n", "\n  ")
                      + "\n  " + mock_track.seed_copy_lines(mocks))
        if daemons:
            mock_setup += ("\n\n  # Service backends that must be listening "
                           "before preprocess runs.\n  " + daemons)
        mock_post = "  " + MOCK_POST.strip().replace("\n", "\n  ")
        dockerfile = DOCKERFILE_MOCK.format(name=task)
    else:
        mock_setup = ""
        mock_post = ""
        dockerfile = DOCKERFILE.format(name=task)

    init = (INIT_SH.replace("{name}", task)
            .replace("{mcp_dirs}", mcp_dirs.rstrip("\n"))
            .replace("{mock_setup}", mock_setup)
            .replace("{mock_post}", mock_post))
    open(os.path.join(dst, "environment", "init.sh"), "w").write(init)
    open(os.path.join(dst, "environment", "Dockerfile"), "w").write(dockerfile)

    if mocks:
        mcp_dir = os.path.join(dst, "environment", "mcp")
        os.makedirs(mcp_dir, exist_ok=True)
        open(os.path.join(mcp_dir, "servers.template.json"), "w").write(
            mock_track.servers_json(mocks))
        seed_dir = os.path.join(dst, "environment", "mock_seed")
        if saved_seed:
            shutil.move(saved_seed, seed_dir)
        else:
            os.makedirs(seed_dir, exist_ok=True)
            for m in mocks:
                spec = mock_track.MOCK_SPECS[m]
                if not spec.get("seed_file"):
                    continue  # service seeds itself from preprocess
                path = os.path.join(seed_dir, spec["seed_file"])
                if not os.path.exists(path):
                    open(path, "w").write(
                        "{}\n")  # TODO(port): task-specific initial state

    # instruction
    task_md = open(os.path.join(src, "docs", "task.md"), encoding="utf-8").read()
    for k, v in PLACEHOLDERS.items():
        task_md = task_md.replace(k, v)
    instruction = PREAMBLE + task_md + "\n"
    if mocks:
        instruction += mock_track.tools_preamble(mocks)
    open(os.path.join(dst, "instruction.md"), "w").write(instruction)

    # task.toml
    desc = task_md.strip().splitlines()[0][:150]
    open(os.path.join(dst, "task.toml"), "w").write(TASK_TOML.format(
        name=task, description=json.dumps(desc),
        kw=json.dumps(servers)[1:-1], servers=json.dumps(servers)))

    # tests: full task dir (grader + groundtruth + fixtures), grader parity
    pkg = os.path.join(dst, "tests", "pkg", "tasks", "finalpool", task)
    shutil.copytree(src, pkg, ignore=shutil.ignore_patterns(
        "__pycache__", "initial_workspace", ".DS_Store", "__MACOSX"))
    open(os.path.join(dst, "tests", "traj_placeholder.json"), "w").write("{}")
    test_sh = os.path.join(dst, "tests", "test.sh")
    open(test_sh, "w").write(TEST_SH.replace("{name}", task).replace(
        "{mock_env}",
        mock_track.mock_env_exports(mocks) if mocks else ""))
    os.chmod(test_sh, 0o755)

    # solution
    sol = os.path.join(dst, "solution")
    if saved_solution:
        shutil.move(saved_solution, sol)
    else:
        os.makedirs(sol, exist_ok=True)
        gt = os.path.join(src, "groundtruth_workspace")
        if os.path.isdir(gt):
            shutil.copytree(gt, os.path.join(sol, "groundtruth_workspace"))
        solve = os.path.join(sol, "solve.sh")
        open(solve, "w").write(SOLVE_SH)
        os.chmod(solve, 0o755)

    # res_log audit warning
    uses_res_log = False
    ev = os.path.join(src, "evaluation")
    for root, _, files in os.walk(ev):
        for f in files:
            if f.endswith(".py"):
                s = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
                if re.search(r"res_log(?!_file..?.?\s*,?\s*required|_file.,\s*.--)", s.replace("args.res_log_file", "")) and "read_json" in s and "res_log" in s:
                    uses_res_log = uses_res_log or bool(re.search(r"read_json\(args\.res_log_file\)", s))
    flag = "  ⚠ grader READS res_log_file — audit {}-safety or bridge trajectory" if uses_res_log else ""
    print(f"{task}: generated (v2){flag}")


if __name__ == "__main__":
    main()
