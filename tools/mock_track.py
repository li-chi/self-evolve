"""Mock-track wiring for ported Toolathlon tasks.

The mock track keeps the upstream task shape intact and swaps only the
service backend:

  agent side     the same MCP tool surface, served by mocks/<svc>-mock and
                 reachable from shell agents through `mcp-tool`
  harness side   upstream preprocess and upstream grader run VERBATIM; the
                 client libraries they import are shadowed by
                 /opt/sdk-shims/<svc>, which reads and writes the SAME mock
                 state the agent's tools mutate

So a mock task differs from the official one in exactly one place: what
sits behind the API. Instruction, initial workspace, preprocess and grader
logic are unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

# --------------------------------------------------------------------------
# per-service wiring
# --------------------------------------------------------------------------

# One HTTP process impersonates every redirected API; services declare the
# hosts they claim and the router that serves them.
FACADE_PORT = 10200
FACADE_DAEMON = {
    "command": "/opt/toolathlon/.venv/bin/python "
               "/opt/mocks/api-facade/server.py",
    "args": f"--port {FACADE_PORT}",
    "wait_port": FACADE_PORT,
    "log": "/var/run/toolathlon/api-facade.log",
}

MOCK_SPECS = {
    "github": {
        # Agent: the github mock's MCP surface. Harness: upstream's own
        # requests-based client, with api.github.com redirected onto the
        # facade router that reads the same state.
        "mock_dir": "/opt/mocks/github-mock",
        "server": "server.py",
        "state_env": "GITHUB_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/github",
        "seed_env": "GITHUB_MOCK_SEED_PATH",
        "seed_file": "github_seed.json",
        "server_args": [],
        "redirect": {"api.github.com": "/__svc/github"},
        "daemon": FACADE_DAEMON,
    },
    "google-cloud": {
        # mock MCP server (same tool names/params as lockon-n/google-cloud-mcp)
        "mock_dir": "/opt/mocks/google-cloud-mock",
        "server": "server.py",
        # shared state, written by both the MCP mock and the SDK shim
        "state_env": "GCP_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/gcp",
        "seed_env": "GCP_MOCK_SEED_PATH",
        "seed_file": "gcp_seed.json",
        # client-library shim that makes upstream code hit the mock
        "shim": "/opt/sdk-shims/gcp",
        "extra_env": {"GCP_MOCK_PROJECT_ID": "mcp-bench0606"},
        # Scoping flags mirror upstream's google-cloud.yaml. They stay as
        # ${token.*} placeholders and are resolved at container start from
        # the task's token_key_session.py, because several tasks only learn
        # their bucket names during preprocess.
        "server_args": [
            "--project-id", "mcp-bench0606",
            "--service-account-path",
            "/opt/toolathlon/configs/gcp-service_account.keys.json",
            "--allowed-buckets", "${token.google_cloud_allowed_buckets}",
            "--allowed-datasets",
            "${token.google_cloud_allowed_bigquery_datasets}",
            "--allowed-log-buckets",
            "${token.google_cloud_allowed_log_buckets}",
            "--allowed-instances", "${token.google_cloud_allowed_instances}",
        ],
    },
    "woocommerce": {
        # Both clients take a URL, so the store is substituted at the HTTP
        # layer: the REST facade serves /wp-json/wc/v3 out of the same
        # state.json the woocommerce MCP mock reads.
        "mock_dir": "/opt/mocks/woocommerce-mock",
        "server": "server.py",
        "state_env": "WC_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/woocommerce",
        "seed_env": "WC_MOCK_SEED_PATH",
        "seed_file": "woocommerce_seed.json",
        "server_args": [],
        "daemon": {
            "command": "/opt/toolathlon/.venv/bin/python "
                       "/opt/mocks/woocommerce-mock/rest_facade.py",
            "args": "--port 10003 "
                    "--state-dir /var/lib/mock-state/woocommerce",
            "wait_port": 10003,
            "log": "/var/run/toolathlon/woocommerce-rest.log",
        },
    },
    "notion": {
        "mock_dir": "/opt/mocks/notion-mock",
        "server": "server.py",
        "state_env": "NOTION_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/notion",
        "seed_env": "NOTION_MOCK_SEED_PATH",
        "seed_file": "notion_seed.json",
        "server_args": [],
        "redirect": {"api.notion.com": "/__svc/notion"},
        "daemon": FACADE_DAEMON,
    },
    "huggingface": {
        "mock_dir": "/opt/mocks/huggingface-mock",
        "server": "server.py",
        "state_env": "HF_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/huggingface",
        "seed_env": "HF_MOCK_SEED_PATH",
        "seed_file": "huggingface_seed.json",
        "server_args": [],
        "redirect": {"huggingface.co": "/__svc/hf"},
        "daemon": FACADE_DAEMON,
    },
    "google_sheet": {
        # Sheets and Drive are one service to a task: gspread reads the
        # grid, the Drive client finds the file. Both hosts land on their
        # router; the two mocks keep separate state files.
        "mock_dir": "/opt/mocks/google-sheets-mock",
        "server": "server.py",
        "state_env": "GSHEETS_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/gsheets",
        "seed_env": "GSHEETS_MOCK_SEED_PATH",
        "seed_file": "gsheets_seed.json",
        "server_args": [],
        "extra_env": {"GDRIVE_MOCK_STATE_DIR": "/var/lib/mock-state/gdrive"},
        "redirect": {"sheets.googleapis.com": "/__svc/gsheets",
                     "www.googleapis.com": "/__svc/googleapis",
                     "oauth2.googleapis.com": "/__svc/goauth"},
        "daemon": FACADE_DAEMON,
    },
    "google_map": {
        "mock_dir": "/opt/mocks/google-maps-mock",
        "server": "server.py",
        "state_env": "GOOGLE_MAPS_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/gmaps",
        "seed_env": "GOOGLE_MAPS_MOCK_SEED_PATH",
        "seed_file": "gmaps_seed.json",
        "server_args": [],
        "redirect": {"maps.googleapis.com": "/__svc/gmaps"},
        "daemon": FACADE_DAEMON,
    },
    "google_calendar": {
        "mock_dir": "/opt/mocks/google-calendar-mock",
        "server": "server.py",
        "state_env": "GCAL_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/gcalendar",
        "seed_env": "GCAL_MOCK_SEED_PATH",
        "seed_file": "gcalendar_seed.json",
        "server_args": [],
        "redirect": {"www.googleapis.com": "/__svc/googleapis",
                     "oauth2.googleapis.com": "/__svc/goauth"},
        "daemon": FACADE_DAEMON,
    },
    "google_forms": {
        "mock_dir": "/opt/mocks/google-forms-mock",
        "server": "server.py",
        "state_env": "GFORMS_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/gforms",
        "seed_env": "GFORMS_MOCK_SEED_PATH",
        "seed_file": "gforms_seed.json",
        "server_args": [],
        "redirect": {"forms.googleapis.com": "/__svc/gforms",
                     "oauth2.googleapis.com": "/__svc/goauth"},
        "daemon": FACADE_DAEMON,
    },
    "wandb": {
        # Graders use wandb.Api (GraphQL + login upstream); the shim reads
        # the mock's state directly, so no network is involved at all.
        "mock_dir": "/opt/mocks/wandb-mock",
        "server": "server.py",
        "state_env": "WANDB_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/wandb",
        "seed_env": "WANDB_MOCK_SEED_PATH",
        "seed_file": "wandb_seed.json",
        "server_args": [],
        "shim": "/opt/sdk-shims/wandb",
    },
    "snowflake": {
        "mock_dir": "/opt/mocks/snowflake-mock",
        "server": "server.py",
        "state_env": "SNOWFLAKE_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/snowflake",
        "seed_env": "SNOWFLAKE_MOCK_SEED_PATH",
        "seed_file": "snowflake_seed.json",
        "server_args": [],
        "shim": "/opt/sdk-shims/snowflake",
    },
    "emails": {
        # The agent drives upstream's REAL emails-mcp; only the mail server
        # behind it is substituted, at the protocol level (SMTP + IMAP), so
        # preprocess (smtplib/imaplib) and graders (imaplib) are unchanged.
        "mcp_command": "/opt/emails-mcp-venv/bin/emails-mcp",
        "server_args": [
            "--attachment_download_path", "/app/emails_download",
            "--attachment_upload_path", "/app",
            "--email_export_path", "/app/emails_export",
            "--config_file", "${token.emails_config_file}",
        ],
        # Background mail server, started by init.sh before preprocess.
        "daemon": {
            "command": "/opt/toolathlon/.venv/bin/python "
                       "/opt/mocks/poste-mock/mailserver.py",
            "args": "--state-dir /var/lib/mock-state/mail "
                    "--users /opt/toolathlon/configs/users_data.json "
                    "--smtp-port 1587 --imap-port 1143",
            "wait_port": 1143,
            "log": "/var/run/toolathlon/mailserver.log",
        },
        "state_env": "MAIL_MOCK_STATE_DIR",
        "state_dir": "/var/lib/mock-state/mail",
        "workspace_dirs": ["emails_download", "emails_export"],
    },
}


def read_task_tokens(task_dir: str) -> dict:
    """Load a task's token_key_session.py without needing `addict` installed.

    Upstream tasks override the global token config with a task-local
    `all_token_key_session` (which service scopes the task may touch). The
    mock server takes the same --allowed-* scoping, so we reuse the values
    verbatim rather than inventing our own.
    """
    path = os.path.join(task_dir, "token_key_session.py")
    if not os.path.exists(path):
        return {}

    class _Dict(dict):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.__dict__ = self

        def __getattr__(self, item):
            return self.get(item)

    stub = types.ModuleType("addict")
    stub.Dict = _Dict
    saved = sys.modules.get("addict")
    sys.modules["addict"] = stub
    saved_cwd = os.getcwd()
    try:
        os.chdir(task_dir)
        spec = importlib.util.spec_from_file_location(
            "_task_token_key_session", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return dict(getattr(mod, "all_token_key_session", {}) or {})
    except Exception as e:  # noqa: BLE001 - report, don't guess
        print(f"  ! could not read {path}: {e}", file=sys.stderr)
        return {}
    finally:
        os.chdir(saved_cwd)
        if saved is not None:
            sys.modules["addict"] = saved
        else:
            sys.modules.pop("addict", None)


def servers_json(services: list) -> str:
    """The `mcp-tool` bridge config template (with ${token.*} placeholders).

    init.sh renders it into /opt/harbor-mcp/servers.json after preprocess.
    """
    cfg = {}
    for svc in services:
        spec = MOCK_SPECS[svc]
        env = {spec["state_env"]: spec["state_dir"]}
        env.update(spec.get("extra_env", {}))
        if spec.get("mcp_command"):
            command, head = spec["mcp_command"], []
        else:
            command = "/opt/toolathlon/.venv/bin/python"
            head = [os.path.join(spec["mock_dir"], spec["server"])]
        cfg[svc] = {
            "command": command,
            "args": head + [str(a) for a in spec["server_args"]],
            "env": env,
        }
    return json.dumps(cfg, indent=2) + "\n"


def redirect_map(services: list) -> dict:
    """Merged host -> local facade URL map for the netredirect layer."""
    out = {}
    for svc in services:
        for host, route in MOCK_SPECS[svc].get("redirect", {}).items():
            out[host] = f"http://127.0.0.1:{FACADE_PORT}{route}"
    return out


def mock_env_exports(services: list) -> str:
    """Shell lines exporting mock state + shim paths (init.sh and test.sh)."""
    lines = []
    shims = []
    for svc in services:
        spec = MOCK_SPECS[svc]
        lines.append(f'export {spec["state_env"]}={spec["state_dir"]}')
        for k, v in spec.get("extra_env", {}).items():
            lines.append(f"export {k}={v}")
        lines.append(f'mkdir -p {spec["state_dir"]}')
        for d in spec.get("workspace_dirs", []):
            lines.append(f"mkdir -p /app/{d}")
        if spec.get("shim"):
            shims.append(spec["shim"])
    redirects = redirect_map(services)
    if redirects:
        # sitecustomize on PYTHONPATH: every python process in the container
        # sends the redirected hosts to the local facade instead.
        shims.append("/opt/mocks/netredirect")
        lines.append("export NETREDIRECT_MAP='" + json.dumps(redirects) + "'")
    if shims:
        lines.append(f'export PYTHONPATH={":".join(shims)}:$PYTHONPATH')
    return "\n".join(lines)


def daemon_lines(services: list) -> str:
    """Start background service backends and wait until they accept traffic.

    Several services share one backend (the API facade), so identical
    daemons are started once.
    """
    out = []
    seen = set()
    for svc in services:
        d = MOCK_SPECS[svc].get("daemon")
        if not d or d["command"] in seen:
            continue
        seen.add(d["command"])
        out.append(
            f'{d["command"]} {d["args"]} > {d["log"]} 2>&1 &\n'
            f'  for _ in $(seq 1 50); do\n'
            f'    if /opt/toolathlon/.venv/bin/python -c "import socket,sys; '
            f's=socket.socket(); s.settimeout(0.5); '
            f'sys.exit(s.connect_ex((\'127.0.0.1\', {d["wait_port"]})))" '
            f'2>/dev/null; then break; fi\n'
            f'    sleep 0.2\n'
            f'  done'
        )
    return "\n  ".join(out)


def seed_copy_lines(services: list) -> str:
    """Install each service's seed as the mock's initial state.json."""
    out = []
    for svc in services:
        spec = MOCK_SPECS[svc]
        if not spec.get("seed_file"):
            continue
        seed = f'/opt/harbor-mcp/seeds/{spec["seed_file"]}'
        out.append(
            f'if [ -f {seed} ] && [ ! -f {spec["state_dir"]}/state.json ]; then\n'
            f'    cp {seed} {spec["state_dir"]}/state.json\n'
            f'  fi'
        )
    return "\n  ".join(out) if out else ":"


TOOLS_PREAMBLE = """
## Available service tools

This environment provides the following service(s) as MCP tool servers:
{server_list}
Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools {first}                 # list tools with one-line summaries
mcp-tool schema {first} <tool_name>    # full argument schema for one tool
mcp-tool call {first} <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call {first} {example_tool} '{example_args}'
```
"""

_EXAMPLES = {
    "google-cloud": ("bigquery_run_query",
                     '{"query": "SELECT * FROM `dataset.table` LIMIT 5"}'),
    "emails": ("list_emails", '{"folder": "INBOX", "limit": 20}'),
}


def tools_preamble(services: list) -> str:
    first = services[0]
    tool, args = _EXAMPLES.get(first, ("<tool_name>", "{}"))
    listing = "".join(f"- `{s}`\n" for s in services)
    return TOOLS_PREAMBLE.format(server_list=listing, first=first,
                                 example_tool=tool, example_args=args)
