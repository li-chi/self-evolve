#!/usr/bin/env python3
"""mcp-tool — call MCP tools from a shell.

Toolathlon hands its agent a set of MCP servers as native tools. Harbor's
shell agents (terminus-2 and friends) have no MCP channel, so this bridge
exposes the *same* servers, the *same* tool names and the *same* argument
schemas as a command line. The tool surface an agent sees is therefore
identical to upstream; only the calling convention differs.

    mcp-tool servers                       # configured servers
    mcp-tool tools <server>                # tool names + one-line summaries
    mcp-tool schema <server> <tool>        # full JSON schema for one tool
    mcp-tool call <server> <tool> '<json>' # call with a JSON argument object
    mcp-tool call <server> <tool> -a k=v -a n:=3   # or key/value form
                                                   # (:= parses the value as JSON)

Servers are declared in $MCP_TOOL_CONFIG (default
/opt/harbor-mcp/servers.json):

    {"google-cloud": {"command": "python",
                      "args": ["/opt/mocks/google-cloud-mock/server.py",
                               "--project-id", "mcp-bench0606"],
                      "env": {"GCP_MOCK_STATE_DIR": "/var/lib/mock-state/gcp"}}}

Each invocation spawns the stdio server, makes the call and exits; mock
state lives on disk, so state persists across calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CONFIG_PATH = os.environ.get("MCP_TOOL_CONFIG", "/opt/harbor-mcp/servers.json")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"mcp-tool: no server config at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def server_params(name: str, cfg: dict) -> StdioServerParameters:
    if name not in cfg:
        sys.exit(f"mcp-tool: unknown server '{name}'. "
                 f"Known: {', '.join(sorted(cfg))}")
    entry = cfg[name]
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    return StdioServerParameters(
        command=entry["command"], args=entry.get("args", []), env=env)


async def _with_session(params, fn):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _render(result) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(json.dumps(
                item.model_dump() if hasattr(item, "model_dump") else str(item),
                default=str))
    if not parts and getattr(result, "structuredContent", None):
        parts.append(json.dumps(result.structuredContent, indent=2,
                                default=str))
    return "\n".join(parts)


def parse_args_pairs(pairs: list) -> dict:
    out = {}
    for p in pairs or []:
        if ":=" in p:
            k, v = p.split(":=", 1)
            out[k] = json.loads(v)
        elif "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
        else:
            sys.exit(f"mcp-tool: bad --arg '{p}' (want key=value or key:=json)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="mcp-tool", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("servers", help="list configured MCP servers")

    p_tools = sub.add_parser("tools", help="list a server's tools")
    p_tools.add_argument("server")
    p_tools.add_argument("--full", action="store_true",
                         help="include full descriptions and schemas")

    p_schema = sub.add_parser("schema", help="show one tool's input schema")
    p_schema.add_argument("server")
    p_schema.add_argument("tool")

    p_call = sub.add_parser("call", help="call a tool")
    p_call.add_argument("server")
    p_call.add_argument("tool")
    p_call.add_argument("json_args", nargs="?", default=None,
                        help="JSON object of arguments")
    p_call.add_argument("-a", "--arg", action="append", dest="args_kv",
                        help="key=value (or key:=json) argument")

    ns = ap.parse_args()
    cfg = load_config()

    if ns.cmd == "servers":
        for name in sorted(cfg):
            print(name)
        return 0

    params = server_params(ns.server, cfg)

    if ns.cmd == "tools":
        tools = asyncio.run(_with_session(
            params, lambda s: s.list_tools()))
        for t in tools.tools:
            if ns.full:
                print(f"## {t.name}\n{t.description or ''}\n"
                      f"input_schema: "
                      f"{json.dumps(t.inputSchema, indent=2)}\n")
            else:
                first = (t.description or "").strip().splitlines()
                print(f"{t.name}\t{first[0] if first else ''}")
        return 0

    if ns.cmd == "schema":
        tools = asyncio.run(_with_session(params, lambda s: s.list_tools()))
        for t in tools.tools:
            if t.name == ns.tool:
                print(json.dumps({"name": t.name,
                                  "description": t.description,
                                  "input_schema": t.inputSchema},
                                 indent=2))
                return 0
        print(f"mcp-tool: no tool '{ns.tool}' on server '{ns.server}'",
              file=sys.stderr)
        return 2

    arguments = {}
    if ns.json_args:
        try:
            arguments = json.loads(ns.json_args)
        except json.JSONDecodeError as e:
            sys.exit(f"mcp-tool: argument JSON is invalid: {e}")
        if not isinstance(arguments, dict):
            sys.exit("mcp-tool: arguments must be a JSON object")
    arguments.update(parse_args_pairs(ns.args_kv))

    result = asyncio.run(_with_session(
        params, lambda s: s.call_tool(ns.tool, arguments)))
    print(_render(result))
    return 1 if getattr(result, "isError", False) else 0


if __name__ == "__main__":
    sys.exit(main())
