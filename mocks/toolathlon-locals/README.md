# toolathlon-locals

MCP server porting Toolathlon's 15 `local-*` tools so a trajectory
generated against the mcp-env harness sees the same tool surface the
LLM will see at Toolathlon eval time. This eliminates the
distribution shift on the local-tool dimension.

Source-of-truth fidelity: tool *names*, *parameter schemas*, and
*return shapes* are copied from
`Toolathlon/utils/aux_tools/{basic,python_interpretor,web_search,
ai_webpage_summary,context_management_tools,history_tools,
overlong_tool_manager}.py`. The canonical inclusion list (the
`local_tool_mappings` dict in `Toolathlon/utils/roles/task_agent.py`)
maps to the tools below 1:1.

## Tool surface (17 tools — 15 distinct + 4 overlong-navigate which Toolathlon counts as one family)

### Group A — stateless / self-contained

| tool                          | source (Toolathlon)                                |
|-------------------------------|----------------------------------------------------|
| `local-sleep`                 | `basic.py::tool_sleep`                              |
| `local-claim_done`            | `basic.py::tool_done` + writes `claimed_done.flag` |
| `local-python-execute`        | `python_interpretor.py::tool_python_execute`        |
| `local-web_search`            | `web_search.py::tool_web_search` (Serper API)       |
| `local-ai_webpage_summary`    | `ai_webpage_summary.py::tool_ai_webpage_summary`    |

### Group B — harness-coupled (read spool files)

| tool                                              | source                                                       |
|---------------------------------------------------|--------------------------------------------------------------|
| `local-check_context_status`                      | `context_management_tools.py::tool_check_context`            |
| `local-manage_context`                            | `context_management_tools.py::tool_manage_context`           |
| `local-smart_context_truncate`                    | `context_management_tools.py::tool_smart_context_truncate`   |
| `local-search_history`                            | `history_tools.py::tool_search_history`                      |
| `local-view_history_turn`                         | `history_tools.py::tool_view_history_turn`                   |
| `local-search_in_turn`                            | `history_tools.py::tool_search_in_turn`                      |
| `local-history_stats`                             | `history_tools.py::tool_history_stats`                       |
| `local-browse_history`                            | `history_tools.py::tool_browse_history`                      |
| `local-search_overlong_tooloutput`                | `overlong_tool_manager.py::tool_search_overlong`             |
| `local-search_overlong_tooloutput_navigate`       | `overlong_tool_manager.py::tool_search_navigate`             |
| `local-view_overlong_tooloutput`                  | `overlong_tool_manager.py::tool_view_overlong`               |
| `local-view_overlong_tooloutput_navigate`         | `overlong_tool_manager.py::tool_view_navigate`               |

## Harness-integration contract

When the runner is started with `--enable-toolathlon-locals`, it
writes (and the server reads) the following files under
`<workspace>/output/_runner/`:

| file                                       | written by | consumed by                              |
|--------------------------------------------|------------|------------------------------------------|
| `transcript.jsonl`                         | runner     | history tools                            |
| `overlong/<shortuuid>.txt`                 | runner     | overlong-tool tools                      |
| `context_status.json`                      | runner     | `local-check_context_status`             |
| `pending_truncate.json`                    | server     | runner (next-turn truncation, planned)   |
| `claimed_done.flag`                        | server     | runner (breaks loop after current turn)  |

`transcript.jsonl` carries one Toolathlon-shaped record per line
(`item_type`, `raw_content`, `turn`, `timestamp`, ...) so
`HistoryManager`-style queries match the format the eval-time tools
would see.

`overlong/<shortuuid>.txt` is created by the runner whenever a tool
result's stringified content exceeds `--overlong-threshold` (default
4000 chars). The model receives an `[overlong tool output spooled
to overlong/<sid>.txt — call local-search_overlong_tooloutput …]`
stub in place of the full text. Toolathlon writes `.json` files
instead; the server accepts either extension.

## State paths

- `TOOLATHLON_LOCALS_WORKSPACE` (preferred) or `AGENT_WORKSPACE` —
  the workspace root. Default: `cwd`. All spool files are derived
  from `<workspace>/output/_runner/`.

## Enabling from the CLI

```bash
python -m runner.cli \
  --task internal/task-XYZ \
  --model gpt-4o-mini \
  --enable-toolathlon-locals \
  --overlong-threshold 4000 \
  --context-limit 128000
```

## Caveats

- **`local-web_search`** needs `SERPER_API_KEY` in the runner env (or
  `TOOLATHLON_SERPER_API_KEY`). With no key set, the tool returns a
  structured `{"error": "SERPER_API_KEY not set; route via
  replay-proxy"}` so a future Tier-B cassette layer can intercept.
- **`local-ai_webpage_summary`** is marked deprecated in Toolathlon
  but still wired into `local_tool_mappings`. Our port does *live*
  HTTP fetches; route through a `fetch` Tier-B cassette during
  training to avoid network non-determinism. Because the mcp-env
  harness has no captive LLM for summarization, we return the cleaned
  page text truncated to ~`max_tokens * 4` characters; the agent can
  summarize itself if it wants.
- **`local-python-execute`** uses `subprocess.run([sys.executable, …])`
  instead of Toolathlon's `uv run`. Stdout/stderr/return-code shapes
  are identical; only the interpreter wrapping differs.
- **`local-manage_context` / `local-smart_context_truncate`** write a
  `pending_truncate.json` directive; the runner does not yet apply it
  to the live `messages` list. Returns shape matches Toolathlon
  (`status=scheduled`), so the agent's surface behavior is the same —
  but actual truncation is a TODO on the runner side.
- **`local-history_stats`** `date_range.duration` is reported as
  `"unknown"` (vs Toolathlon's formatted string) because the
  trajectory timestamps come from the runner clock and the format
  rarely matters for agent reasoning. Easy to extend.

## Smoke test

```bash
cd /Users/chili/Projects/mcp-env
python -m mcp_servers.toolathlon-locals.tests.smoke
```

The smoke test mounts the server with a stub LLM (canned tool calls),
invokes `local-python-execute`, `local-check_context_status`,
`local-claim_done` in sequence, and asserts:

  - python_execute returns `4` for `print(2 + 2)`
  - context_status returns a `usage_percentage` field
  - `claimed_done.flag` is created
  - the loop breaks within 3 turns
