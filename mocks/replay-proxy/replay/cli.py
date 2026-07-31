"""replay CLI — cassette management commands.

Usage:
    python -m replay.cli list      <cassette.jsonl>
    python -m replay.cli lookup    <cassette.jsonl> <tool> '<args-json>'
    python -m replay.cli validate  <cassette.jsonl>
    python -m replay.cli hash      '<args-json>'
    python -m replay.cli record    --server <s> --cassette <p>
                                   --tool <t> --args '<json>'
                                   --response '<json>'
    python -m replay.cli record-server         --config <path>
    python -m replay.cli record-replay-trajectory
                                   --trajectory <path.jsonl>
                                   --server <name>
                                   --cassette <path>
                                   --upstream-cmd "uv run server.py"
                                   [--upstream-env KEY=VAL ...]
                                   [--upstream-cwd <dir>]

`record` is a *manual* recorder: you paste in a tool name + args +
real response and it appends one canonicalized entry.

`record-server` is the long-lived recorder: it spawns a real
upstream MCP server, exposes the same tool surface on its own stdio
to the agent, and appends every (tool, args, response) to the
cassette as a transparent middleman.

`record-replay-trajectory` is the offline variant: it reads an
existing tool-use trajectory (Toolathlon / OpenAI-style) and runs
each call against the real upstream once to capture responses into
a fresh cassette.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys

from .cassette import (
    Cassette,
    CassetteEntry,
    args_hash,
    canonicalize_args,
    load_cassette,
    validate_cassette,
    write_entry,
    _utc_now,
)


def _cmd_list(args: argparse.Namespace) -> int:
    cas = load_cassette(args.cassette)
    print(f"# {args.cassette}  ({len(cas)} entries, "
          f"{len(cas.tools())} tools)")
    for e in cas.entries:
        h = e.args_hash.split(":", 1)[-1][:12]
        a = json.dumps(e.args, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
        if len(a) > 80:
            a = a[:77] + "..."
        print(f"  {e.server:<20} {e.tool:<28} {h}  {a}")
    return 0


def _cmd_lookup(args: argparse.Namespace) -> int:
    cas = load_cassette(args.cassette)
    parsed = json.loads(args.args) if args.args else {}
    entry = cas.lookup(args.tool, parsed)
    if entry is None:
        h = args_hash(parsed)
        print(json.dumps({
            "ok": False,
            "error": "cassette_miss",
            "tool": args.tool,
            "args_hash": h,
            "canonical_args": canonicalize_args(parsed),
        }, indent=2))
        return 1
    print(json.dumps({"ok": True, "entry": entry.to_dict()}, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    issues = validate_cassette(args.cassette)
    if not issues:
        print(f"OK: {args.cassette} is clean")
        return 0
    print(f"FAIL: {args.cassette}  ({len(issues)} issue(s))")
    for line in issues:
        print(f"  - {line}")
    return 2


def _cmd_hash(args: argparse.Namespace) -> int:
    parsed = json.loads(args.args) if args.args else {}
    canon = canonicalize_args(parsed)
    print(json.dumps({
        "canonical_args": canon,
        "args_hash": args_hash(parsed),
    }, indent=2))
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    parsed_args = json.loads(args.args) if args.args else {}
    parsed_resp = json.loads(args.response) if args.response else None
    entry = CassetteEntry(
        server=args.server,
        tool=args.tool,
        args_hash=args_hash(parsed_args),
        args=canonicalize_args(parsed_args),
        response=parsed_resp,
        recorded_at=_utc_now(),
    )
    write_entry(args.cassette, entry)
    print(f"appended to {args.cassette}: "
          f"{entry.tool} {entry.args_hash[:24]}…")
    return 0


def _cmd_record_server(args: argparse.Namespace) -> int:
    """Launch the long-lived recording-proxy MCP server.

    Imported lazily so the rest of the CLI keeps working even if the
    recorder's extra deps (anyio, etc.) aren't on the path during
    smoke tests of the replay-only flow."""
    # ``record_server`` lives at the repo root, not under ``replay/``.
    # The recorder is intentionally a separate top-level module so it
    # can be the entrypoint of its own MCP server (mirroring server.py).
    try:
        from record_server import run_server  # type: ignore
    except ImportError:
        # Fall back to package-relative if someone reorganizes things.
        import os
        import sys as _sys
        _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from record_server import run_server  # type: ignore
    run_server(args.config)
    return 0


def _parse_env_kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(
                f"--upstream-env expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _cmd_record_replay_trajectory(args: argparse.Namespace) -> int:
    try:
        from record_server import replay_trajectory  # type: ignore
    except ImportError:
        import os
        import sys as _sys
        _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from record_server import replay_trajectory  # type: ignore

    # The user passes the whole upstream command as a single shell-ish
    # string for ergonomics; split it here.
    parts = shlex.split(args.upstream_cmd)
    if not parts:
        raise SystemExit("--upstream-cmd is empty")
    cmd, cmd_args = parts[0], parts[1:]
    env = _parse_env_kv(args.upstream_env)

    stats = replay_trajectory(
        trajectory=args.trajectory,
        server=args.server,
        cassette=args.cassette,
        upstream_cmd=cmd,
        upstream_args=cmd_args,
        upstream_env=env or None,
        upstream_cwd=args.upstream_cwd,
    )
    print(json.dumps({"ok": True, **stats,
                      "cassette": args.cassette}, indent=2))
    # Non-zero exit if any step failed so callers can gate on it.
    return 0 if stats.get("errors", 0) == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="replay",
                                description="Replay-proxy cassette tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list entries in a cassette")
    sp.add_argument("cassette")
    sp.set_defaults(fn=_cmd_list)

    sp = sub.add_parser("lookup",
                        help="resolve (tool, args) against a cassette")
    sp.add_argument("cassette")
    sp.add_argument("tool")
    sp.add_argument("args", nargs="?", default="{}",
                    help="args as a JSON object (default {})")
    sp.set_defaults(fn=_cmd_lookup)

    sp = sub.add_parser("validate",
                        help="schema + hash check a cassette")
    sp.add_argument("cassette")
    sp.set_defaults(fn=_cmd_validate)

    sp = sub.add_parser("hash",
                        help="print the canonical hash of an args dict")
    sp.add_argument("args", nargs="?", default="{}")
    sp.set_defaults(fn=_cmd_hash)

    sp = sub.add_parser("record",
                        help="append one (tool, args, response) entry")
    sp.add_argument("--cassette", required=True)
    sp.add_argument("--server", required=True)
    sp.add_argument("--tool", required=True)
    sp.add_argument("--args", default="{}")
    sp.add_argument("--response", default="null")
    sp.set_defaults(fn=_cmd_record)

    sp = sub.add_parser(
        "record-server",
        help=("launch the recording-proxy MCP server (sits in front of "
              "a real upstream, appends every call to a cassette)"))
    sp.add_argument(
        "--config",
        help=("recorder config JSON; if omitted, $REPLAY_RECORDER_CONFIG "
              "is used"))
    sp.set_defaults(fn=_cmd_record_server)

    sp = sub.add_parser(
        "record-replay-trajectory",
        help=("replay every tool call in a trajectory against a real "
              "upstream MCP server and capture responses into a "
              "fresh cassette"))
    sp.add_argument("--trajectory", required=True,
                    help="JSONL of tool-use events")
    sp.add_argument("--server", required=True,
                    help="logical server name (stamped on cassette entries)")
    sp.add_argument("--cassette", required=True,
                    help="output cassette path (will be appended to)")
    sp.add_argument("--upstream-cmd", required=True,
                    help="shell-style command to launch the upstream "
                         "(e.g. 'uv run /path/to/server.py')")
    sp.add_argument("--upstream-env", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="extra env var for the upstream subprocess; repeatable")
    sp.add_argument("--upstream-cwd", default=None,
                    help="working dir for the upstream subprocess")
    sp.set_defaults(fn=_cmd_record_replay_trajectory)

    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    ns = p.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
