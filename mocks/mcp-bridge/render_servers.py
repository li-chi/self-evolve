#!/usr/bin/env python3
"""Resolve ${token.*} placeholders in the MCP bridge config at run time.

Upstream launches its MCP servers AFTER preprocess, passing scoping flags
(--allowed-buckets, --allowed-datasets, ...) whose values come from the
task's token_key_session.py. Several tasks generate those names during
preprocess (a fresh `iot_anomaly_reports-<uuid>` bucket each run) and
token_key_session.py reads them back from groundtruth_workspace, so the
values only exist once preprocess has run.

This does the same: it is invoked from init.sh after preprocess, with the
task's groundtruth_workspace still in place, and rewrites
servers.template.json into servers.json.

    render_servers.py <task-name> <template> <output>
"""

import json
import os
import re
import shutil
import sys

PLACEHOLDER = re.compile(r"\$\{token\.([A-Za-z0-9_]+)\}")


def load_tokens(task_dir: str) -> dict:
    path = os.path.join(task_dir, "token_key_session.py")
    if not os.path.exists(path):
        return {}
    import importlib.util
    saved_cwd = os.getcwd()
    try:
        os.chdir(task_dir)
        spec = importlib.util.spec_from_file_location(
            "_task_token_key_session", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return dict(getattr(mod, "all_token_key_session", {}) or {})
    except Exception as e:  # noqa: BLE001 - surface, never guess a scope
        print(f"render_servers: could not read {path}: {e}", file=sys.stderr)
        return {}
    finally:
        os.chdir(saved_cwd)


def main() -> int:
    task, template, output = sys.argv[1], sys.argv[2], sys.argv[3]
    tokens = load_tokens(f"/opt/toolathlon/tasks/finalpool/{task}")
    with open(template, "r", encoding="utf-8") as f:
        raw = f.read()

    missing = []

    task_dir = f"/opt/toolathlon/tasks/finalpool/{task}"
    stable_dir = os.path.dirname(output)

    def sub(m):
        key = m.group(1)
        value = tokens.get(key)
        if value is None:
            missing.append(key)
            return "null"
        value = str(value)
        # init.sh removes the staged task tree once preprocess is done (the
        # agent must not see preprocess sources), so any config file the MCP
        # server needs at run time is copied out first.
        if value.startswith(task_dir) and os.path.isfile(value):
            dest = os.path.join(stable_dir, os.path.basename(value))
            shutil.copyfile(value, dest)
            return dest
        return value

    resolved = PLACEHOLDER.sub(sub, raw)
    if missing:
        print(f"render_servers: unresolved token(s) {sorted(set(missing))} "
              f"-> 'null' (service scope closed)", file=sys.stderr)
    json.loads(resolved)  # fail loudly on a malformed config
    with open(output, "w", encoding="utf-8") as f:
        f.write(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
