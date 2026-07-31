"""Toolathlon local-tools MCP server.

Mirrors the 15 `local-*` tools from
`Toolathlon/utils/aux_tools/{basic,python_interpretor,web_search,
ai_webpage_summary,context_management_tools,history_tools,
overlong_tool_manager}.py`. Tool *names*, *parameter schemas*, and
*return shapes* match Toolathlon's `FunctionTool(...)` definitions
verbatim, so a trajectory produced against this server is a
drop-in substitute for one produced inside Toolathlon's `task_agent.py`
local-tool registration path.

GROUP A — stateless / self-contained:
  - local-sleep, local-claim_done, local-python-execute,
    local-web_search, local-ai_webpage_summary

GROUP B — reads spool files the agent harness writes per rollout:
  - history tools: local-search_history, local-view_history_turn,
    local-search_in_turn, local-history_stats, local-browse_history
    Reads `$TOOLATHLON_LOCALS_WORKSPACE/output/_runner/transcript.jsonl`.
  - overlong-output tools: local-search_overlong_tooloutput,
    local-search_overlong_tooloutput_navigate,
    local-view_overlong_tooloutput, local-view_overlong_tooloutput_navigate
    Reads `$TOOLATHLON_LOCALS_WORKSPACE/output/_runner/overlong/<id>.txt`.
  - context-management tools: local-check_context_status,
    local-manage_context, local-smart_context_truncate
    Reads `$TOOLATHLON_LOCALS_WORKSPACE/output/_runner/context_status.json`,
    writes `_pending_truncate` directives to a sibling file that the
    harness then honors.
  - local-claim_done writes
    `$TOOLATHLON_LOCALS_WORKSPACE/output/_runner/claimed_done.flag`
    so the harness can break the ReAct loop.

If the harness has not yet started writing those files, Group B tools
return `{"error": "harness state not initialized; spooling disabled?"}`
rather than crashing — matches Toolathlon's behavior when context
isn't wired up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
# urllib.parse: only used by deferred web_search / ai_webpage_summary; re-import when re-enabling
# from urllib.parse import urljoin, urlparse

# requests / bs4: only used by deferred web_search / ai_webpage_summary; re-import when re-enabling
# import requests
# from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP


log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Workspace + spool paths
# ----------------------------------------------------------------------

def _workspace_root() -> str:
    return os.environ.get(
        "TOOLATHLON_LOCALS_WORKSPACE",
        os.environ.get("AGENT_WORKSPACE", os.getcwd()),
    )


def _runner_dir() -> Path:
    """Where the harness spools per-rollout state."""
    d = Path(_workspace_root()) / "output" / "_runner"
    return d


def _transcript_path() -> Path:
    return _runner_dir() / "transcript.jsonl"


def _overlong_dir() -> Path:
    return _runner_dir() / "overlong"


def _context_status_path() -> Path:
    return _runner_dir() / "context_status.json"


def _pending_truncate_path() -> Path:
    return _runner_dir() / "pending_truncate.json"


def _claimed_done_path() -> Path:
    return _runner_dir() / "claimed_done.flag"


def _harness_initialized() -> bool:
    """Group-B tools refuse to run until the harness writes any spool."""
    return _runner_dir().exists()


def _harness_uninitialized_error() -> dict:
    return {
        "error": "harness state not initialized; spooling disabled?",
        "expected_dir": str(_runner_dir()),
    }


# ----------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------

mcp = FastMCP("toolathlon-locals")


# ======================================================================
# GROUP A — stateless / self-contained
# ======================================================================

# ----- local-sleep ----------------------------------------------------
# Source: Toolathlon/utils/aux_tools/basic.py L8-30
# Cap at 60s to prevent abuse (Toolathlon has no cap because it runs
# inside a 1-task subprocess; trajectory generators run many tasks).
_SLEEP_CAP = 60.0


@mcp.tool(name="local-sleep")
async def local_sleep(seconds: float) -> str:
    """use this tool to sleep for a while"""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        s = 1.0
    s = max(0.0, min(s, _SLEEP_CAP))
    await asyncio.sleep(s)
    return f"has slept {seconds} seconds, wake up!"


# ----- local-claim_done -----------------------------------------------
# Source: Toolathlon/utils/aux_tools/basic.py L32-46
@mcp.tool(name="local-claim_done")
def local_claim_done() -> str:
    """claim the task is done"""
    try:
        _runner_dir().mkdir(parents=True, exist_ok=True)
        _claimed_done_path().write_text("done", encoding="utf-8")
    except Exception as e:
        log.warning("could not write claimed_done flag: %s", e)
    return "you have claimed the task is done!"


# ----- local-python-execute -------------------------------------------
# Source: Toolathlon/utils/aux_tools/python_interpretor.py L10-116

# Network egress guard (2026-06-03): tasks ship with allow_internet=false
# but nothing enforced it — agent code could urllib straight to the live
# internet, contaminating probe verdicts (live data ≠ cassette gold).
# Default-deny: a sitecustomize.py is injected via PYTHONPATH into the
# executed subprocess and blocks socket connects to non-local hosts.
# Opt out per-run with MCPENV_ALLOW_NET=1. This stops the honest-path
# escapes (urllib/requests/httpx all route through socket.socket.connect);
# it is not an adversarial sandbox (raw _socket / shelling out to curl
# would bypass it).
_NETGUARD_SITECUSTOMIZE = '''\
import os
if os.environ.get("MCPENV_ALLOW_NET") != "1":
    import socket as _s
    _ALLOWED = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
    _oc = _s.socket.connect
    _ocx = _s.socket.connect_ex
    def _ok(addr):
        if isinstance(addr, (str, bytes)):
            return True  # unix domain socket path
        try:
            h = addr[0]
        except Exception:
            return False
        return isinstance(h, str) and h in _ALLOWED
    def _gc(self, addr):
        if not _ok(addr):
            raise OSError(13, "network egress disabled in this environment (netguard): %r" % (addr,))
        return _oc(self, addr)
    def _gcx(self, addr):
        if not _ok(addr):
            return 13
        return _ocx(self, addr)
    _s.socket.connect = _gc
    _s.socket.connect_ex = _gcx
'''

_NETGUARD_DIR: str | None = None


def _netguard_dir() -> str:
    global _NETGUARD_DIR
    if _NETGUARD_DIR is None:
        import tempfile
        d = tempfile.mkdtemp(prefix="mcpenv_netguard_")
        with open(os.path.join(d, "sitecustomize.py"), "w", encoding="utf-8") as f:
            f.write(_NETGUARD_SITECUSTOMIZE)
        _NETGUARD_DIR = d
    return _NETGUARD_DIR


@mcp.tool(name="local-python-execute")
def local_python_execute(
    code: str,
    filename: str | None = None,
    timeout: float = 30,
) -> str:
    """Execute Python code directly under the agent workspace, and returns
    stdout, stderr, return code, and execution time in a structured format."""
    try:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 30.0
        if timeout > 120:
            timeout = 120
        if timeout <= 0:
            timeout = 30

        if not filename:
            filename = f"{uuid.uuid4()}.py"
        if not filename.endswith(".py"):
            filename += ".py"

        agent_workspace = os.path.abspath(_workspace_root())
        tmp_dir = os.path.join(agent_workspace, ".python_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        file_path = os.path.join(tmp_dir, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        start_time = time.time()
        # Toolathlon uses `uv run`; we fall back to plain python3 when uv
        # is not available, since the trajectory generator host may not
        # have uv. Either way the captured stdout/stderr shape is the same.
        cmd = [sys.executable, file_path]
        _env = dict(os.environ)
        if _env.get("MCPENV_ALLOW_NET") != "1":
            _env["PYTHONPATH"] = _netguard_dir() + os.pathsep + _env.get("PYTHONPATH", "")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                cwd=agent_workspace,
                env=_env,
            )
        except subprocess.TimeoutExpired:
            execution_time = time.time() - start_time
            return (
                "=== EXECUTION TIMEOUT ===\n"
                f"Execution timed out after {timeout} seconds\n"
                f"Execution time: {execution_time:.3f} seconds"
            )

        execution_time = time.time() - start_time

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append("=== STDOUT ===")
            output_parts.append(result.stdout.rstrip())
        if result.stderr:
            output_parts.append("=== STDERR ===")
            output_parts.append(result.stderr.rstrip())
        output_parts.append("=== EXECUTION INFO ===")
        output_parts.append(f"Return code: {result.returncode}")
        output_parts.append(f"Execution time: {execution_time:.3f} seconds")
        output_parts.append(f"Timeout limit: {timeout} seconds")
        if not result.stdout and not result.stderr:
            output_parts.insert(0, "No console output produced.")
        return "\n".join(output_parts)
    except Exception as e:
        return f"Error executing Python code: {str(e)}"


# ======================================================================
# DEFERRED — local-web_search and local-ai_webpage_summary
# Both make live network calls. Re-enable once they route through the
# replay-proxy (configs/web-search.json + configs/fetch.json cassettes)
# so training rollouts stay deterministic + side-effect-free.
# Source preserved inside the triple-quoted block below; flip to a real
# code block by removing the surrounding r''' / ''' markers.
# ======================================================================
r'''
# ----- local-web_search ----------------------------------------------
# Source: Toolathlon/utils/aux_tools/web_search.py L254-338
@mcp.tool(name="local-web_search")
def local_web_search(query: str, num_results: int = 10) -> str:
    """Search the web using Google Serper API with concurrency control and
    retry mechanisms. Supports various Google search operators."""
    query = (query or "").strip()
    if not query:
        return "Error: Query parameter is required and cannot be empty"
    num_results = min(max(int(num_results or 10), 1), 50)

    api_key = (
        os.environ.get("SERPER_API_KEY")
        or os.environ.get("TOOLATHLON_SERPER_API_KEY")
        or ""
    ).strip()
    if not api_key:
        # Structured error so a future cassette layer can route this
        # through replay-proxy.
        return json.dumps({
            "error": "SERPER_API_KEY not set; route via replay-proxy",
            "tool": "local-web_search",
            "query": query,
        })
    if "," in api_key:
        import random
        api_key = random.choice(api_key.split(","))

    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            data=json.dumps({"q": query, "num": num_results}),
            timeout=30,
        )
        resp.raise_for_status()
        results = json.loads(resp.text).get("organic", [])
    except Exception as e:
        return f"Error: google serper search failed for query={query!r}. The error is: {e!r}"

    if not results:
        return "No search results found."

    formatted_results: list[str] = []
    for result in results:
        if "error" in result:
            formatted_results.append(f"Error: {result['error']}")
        else:
            title = result.get("title", "No title")
            link = result.get("link", "No link")
            snippet = result.get("snippet", "No description")
            sitelinks = result.get("sitelinks", "No sitelinks")
            formatted_results.append(
                f"Title: {title}\nLink: {link}\nSnippet: {snippet}\n"
                f"Sitelinks: {sitelinks}\n"
            )
    return "\n".join(formatted_results)


# ----- local-ai_webpage_summary --------------------------------------
# Source: Toolathlon/utils/aux_tools/ai_webpage_summary.py L258-315
# Toolathlon notes this tool is "DEPRECATED, DO NOT USE IT" but it's still
# wired in local_tool_mappings, so we ship a faithful port. Live HTTP
# fetches; route through fetch Tier-B cassette during training.
def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_text_from_html(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "nav", "header", "footer",
                    "aside", "iframe", "noscript"]):
        el.decompose()
    parts: list[str] = []
    for tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        for el in soup.find_all(tag):
            t = _clean_text(el.get_text())
            if t:
                parts.append(f"{tag.upper()}: {t}")
    for el in soup.find_all(["p", "div", "span", "li", "td", "th"]):
        t = _clean_text(el.get_text())
        if t and len(t) > 10:
            parts.append(t)
    for link in soup.find_all("a", href=True):
        lt = _clean_text(link.get_text())
        if lt and len(lt) > 3:
            href = link.get("href")
            if href:
                if not href.startswith(("http://", "https://")):
                    href = urljoin(url, href)
                parts.append(f"Link: {lt} ({href})")
    full = "\n\n".join(parts)
    if len(full) < 100:
        full = _clean_text(soup.get_text())
    return full


@mcp.tool(name="local-ai_webpage_summary")
def local_ai_webpage_summary(url: str, max_tokens: int = 1000) -> str:
    """use this tool to get a summary of a webpage, powered by GPT-4.1-nano"""
    if not url:
        return "Error: URL parameter cannot be empty"
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return "Error: Invalid URL format"
    except Exception as e:
        return f"Error: URL parsing failed: {e}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        text = _extract_text_from_html(resp.text, url)
    except Exception as e:
        return f"Error: Request failed: {e}"

    if not text or len(text.strip()) < 10:
        return "Error: Cannot get valid webpage content"
    if len(text) > 180000:
        text = text[:180000] + "\n\n[Content truncated...]"

    # Toolathlon delegates summarization to an LLM. In the mcp-env harness
    # we have no captive LLM, so we return the cleaned page text truncated
    # to roughly max_tokens (4 chars/token approximation). The agent can
    # still read/summarize it. Flagged in README.
    max_chars = int(max_tokens) * 4
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Truncated by local-ai_webpage_summary at ~max_tokens chars]"
    return text
'''
# ======================================================================
# end DEFERRED block
# ======================================================================


# ======================================================================
# GROUP B — harness-coupled (read spool files)
# ======================================================================

# ----- context status helpers ----------------------------------------

def _read_context_status() -> dict:
    p = _context_status_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _status_recommendation(usage_pct: float) -> dict:
    """Toolathlon parity: context_management_tools._get_status_recommendation."""
    if usage_pct >= 90:
        return {
            "level": "critical",
            "message": "Context is about to be exhausted! Strongly recommend cleaning up conversation history immediately.",
            "recommended_action": "manage_context",
        }
    if usage_pct >= 80:
        return {
            "level": "warning",
            "message": "Context usage is high, recommend cleaning up some conversation history.",
            "recommended_action": "manage_context",
        }
    if usage_pct >= 70:
        return {
            "level": "info",
            "message": "Context usage is moderate, consider preventive cleanup.",
            "recommended_action": "monitor",
        }
    return {
        "level": "good",
        "message": "Context usage is healthy.",
        "recommended_action": "none",
    }


# ----- local-check_context_status ------------------------------------
# Source: Toolathlon/utils/aux_tools/context_management_tools.py L6-103
@mcp.tool(name="local-check_context_status")
def local_check_context_status() -> dict:
    """Query current conversation context status, including turn statistics,
    token usage, truncation history and other information"""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    try:
        status = _read_context_status()
        session_id = status.get("session_id", "unknown")
        context_limit = int(status.get("context_limit") or 128000)
        total_tokens = int(status.get("total_tokens") or 0)
        current_turn = int(status.get("turn") or 0)
        turns_in_seq = int(status.get("turns_in_current_sequence") or current_turn)
        total_turns_ever = int(status.get("total_turns_ever") or current_turn)
        truncated_turns = int(status.get("truncated_turns") or 0)
        truncation_history = status.get("truncation_history", [])
        history_dir = status.get("history_dir", str(_runner_dir()))
        started_at = status.get("started_at", "unknown")

        usage_pct = (
            round(total_tokens / context_limit * 100, 2)
            if context_limit > 0 else 0.0
        )
        return {
            "session_info": {
                "session_id": session_id,
                "started_at": started_at,
                "history_dir": history_dir,
            },
            "turn_statistics (turns before invoking this tool)": {
                "current_turn": current_turn,
                "turns_in_current_sequence": turns_in_seq,
                "total_turns_ever": total_turns_ever,
                "truncated_turns": truncated_turns,
            },
            "token_usage": {
                "total_tokens": total_tokens,
                "context_limit": context_limit,
                "usage_percentage": usage_pct,
                "remaining_tokens": max(0, context_limit - total_tokens),
            },
            "truncation_history": truncation_history,
            "status": _status_recommendation(usage_pct),
        }
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc(),
            "message": "Unable to get context status",
        }


# ----- local-manage_context ------------------------------------------
# Source: Toolathlon/utils/aux_tools/context_management_tools.py L105-236
@mcp.tool(name="local-manage_context")
def local_manage_context(
    method: str,
    value: float,
    action: str = "truncate",
    preserve_system: bool = True,
    preserve_first_user_input: bool = True,
) -> dict:
    """Manage conversation context by deleting historical messages.
    Supports keep_recent_turns / keep_recent_percent / delete_first_turns /
    delete_first_percent."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    if action != "truncate":
        return {"status": "error",
                "message": f"Unsupported operation: {action}"}
    valid_methods = [
        "keep_recent_turns", "keep_recent_percent",
        "delete_first_turns", "delete_first_percent",
    ]
    if method not in valid_methods:
        return {"status": "error",
                "message": f"Invalid method: {method}. Supported methods: {valid_methods}"}
    if not isinstance(value, (int, float)) or value <= 0:
        return {"status": "error",
                "message": f"Invalid value: {value}. Must be a positive number."}
    if "percent" in method and (value <= 0 or value >= 100):
        return {"status": "error",
                "message": f"Percentage must be between 0-100, current value: {value}"}

    status = _read_context_status()
    current_turns = int(status.get("turns_in_current_sequence")
                        or status.get("turn") or 0)
    protected = 1 if preserve_first_user_input and current_turns > 0 else 0
    eligible = current_turns - protected
    if method == "keep_recent_turns":
        keep = protected + min(int(value), eligible)
    elif method == "keep_recent_percent":
        keep = protected + (max(1, int(eligible * value / 100))
                            if eligible > 0 else 0)
    elif method == "delete_first_turns":
        keep = protected + max(0, eligible - int(value))
    else:  # delete_first_percent
        delete_turns = int(eligible * value / 100)
        keep = protected + max(0, eligible - delete_turns)

    if keep >= current_turns:
        return {
            "status": "no_action",
            "message": f"Currently only {current_turns} turns of conversation, no truncation needed.",
            "current_turns": current_turns,
            "requested_keep": keep,
        }

    pending = {
        "method": method,
        "value": value,
        "preserve_system": preserve_system,
        "preserve_first_user_input": preserve_first_user_input,
        "requested_at_turn": int(status.get("turn") or 0),
        "expected_keep_turns": keep,
        "expected_delete_turns": current_turns - keep,
    }
    try:
        _runner_dir().mkdir(parents=True, exist_ok=True)
        _pending_truncate_path().write_text(json.dumps(pending),
                                            encoding="utf-8")
    except Exception as e:
        return {"status": "error",
                "message": f"Could not write pending_truncate: {e}"}

    return {
        "status": "scheduled",
        "message": "Truncation operation completed.",
        "details": {
            "method": method,
            "value": value,
            "current_turns": current_turns,
            "will_keep": keep,
            "will_delete": current_turns - keep,
            "preserve_system_messages": preserve_system,
            "preserve_first_user_input": preserve_first_user_input,
        },
    }


# ----- local-smart_context_truncate ----------------------------------
# Source: Toolathlon/utils/aux_tools/context_management_tools.py L238-403
@mcp.tool(name="local-smart_context_truncate")
def local_smart_context_truncate(
    ranges: list[list[int]],
    preserve_system: bool = True,
    preserve_first_user_input: bool = True,
) -> dict:
    """Smart context truncation tool that precisely controls retained content
    by specifying ranges. Accepts 2D list [[start1,end1],...]."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    try:
        if not isinstance(ranges, list):
            return {"status": "error",
                    "message": "ranges parameter must be a 2D list"}
        if not ranges:
            return {"status": "error",
                    "message": "ranges cannot be empty, must specify at least one retention range"}
        status = _read_context_status()
        current_turns = int(status.get("turns_in_current_sequence")
                            or status.get("turn") or 0)
        validated: list[tuple[int, int]] = []
        for i, r in enumerate(ranges):
            if not isinstance(r, list) or len(r) != 2:
                return {"status": "error",
                        "message": f"ranges[{i}] must be a list containing two elements [start, end]"}
            start, end = r
            if not isinstance(start, int) or not isinstance(end, int):
                return {"status": "error",
                        "message": f"start and end in ranges[{i}] must be integers"}
            if start < 0 or end < 0:
                return {"status": "error",
                        "message": f"Indexes in ranges[{i}] cannot be negative"}
            if start > end:
                return {"status": "error",
                        "message": f"start({start}) in ranges[{i}] cannot be greater than end({end})"}
            if end >= current_turns:
                return {"status": "error",
                        "message": f"end({end}) in ranges[{i}] exceeds current turn range (0-{current_turns-1})"}
            validated.append((start, end))
        validated.sort()
        for i in range(1, len(validated)):
            if validated[i][0] <= validated[i-1][1]:
                return {"status": "error",
                        "message": f"Range overlap: [{validated[i-1][0]}, {validated[i-1][1]}] with [{validated[i][0]}, {validated[i][1]}]"}

        retained = set()
        if preserve_first_user_input and current_turns > 0:
            retained.add(0)
        for s, e in validated:
            retained.update(range(s, e + 1))
        keep = len(retained)
        delete = current_turns - keep
        if delete <= 0:
            return {
                "status": "no_action",
                "message": "Specified ranges already cover all turns, no truncation needed.",
                "current_turns": current_turns,
                "keep_turns": keep,
            }
        pending = {
            "method": "smart_ranges",
            "ranges": validated,
            "preserve_system": preserve_system,
            "preserve_first_user_input": preserve_first_user_input,
            "requested_at_turn": int(status.get("turn") or 0),
            "expected_keep_turns": keep,
            "expected_delete_turns": delete,
        }
        _runner_dir().mkdir(parents=True, exist_ok=True)
        _pending_truncate_path().write_text(json.dumps(pending),
                                            encoding="utf-8")
        return {
            "status": "scheduled",
            "message": "Smart truncation operation completed.",
            "details": {
                "method": "smart_ranges",
                "ranges": validated,
                "current_turns": current_turns,
                "will_keep": keep,
                "will_delete": delete,
                "preserve_system_messages": preserve_system,
                "preserve_first_user_input": preserve_first_user_input,
            },
        }
    except Exception as e:
        import traceback
        return {"status": "error",
                "message": f"Error occurred while executing smart truncation: {e}",
                "traceback": traceback.format_exc()}


# ----- history-tools helpers -----------------------------------------
# Source: history_tools.py + history_manager.py
# Toolathlon's HistoryManager reads `<session_id>_history.jsonl`. We read
# the harness-spooled `transcript.jsonl` and treat each JSON line as one
# history record with the same field conventions (`turn`, `timestamp`,
# `item_type`, `raw_content`, `type`, `content`).

def _load_transcript() -> list[dict]:
    p = _transcript_path()
    if not p.exists():
        return []
    history: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rec.setdefault("_line_index", line_num)
                history.append(rec)
            except json.JSONDecodeError:
                continue
    return history


def _truncate_content(content: str, max_length: int = 1000,
                     head_tail_length: int = 500) -> str:
    if len(content) <= max_length:
        return content
    if len(content) <= head_tail_length * 2:
        return content
    head = content[:head_tail_length]
    tail = content[-head_tail_length:]
    return (f"{head}\n... [{len(content) - head_tail_length * 2}"
            f" characters omitted] ...\n{tail}")


def _extract_search_content(record: dict) -> str:
    """Toolathlon parity: HistoryManager._extract_search_content."""
    item_type = record.get("item_type", record.get("type", ""))
    if item_type == "message_output_item":
        raw = record.get("raw_content", {})
        if isinstance(raw, dict):
            parts = []
            for c in raw.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
            return " ".join(parts)
    elif item_type in ("tool_call_item", "tool_call_output_item"):
        raw = record.get("raw_content", {})
        if isinstance(raw, dict):
            return f"{raw.get('name', '')} {raw.get('arguments', '')}"
    elif item_type in ("initial_input", "user_input"):
        c = record.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                (i.get("content", "") if isinstance(i, dict) else str(i))
                for i in c
            )
    return ""


def _extract_searchable_content(record: dict) -> str:
    """Toolathlon parity: HistoryManager._extract_searchable_content (used by
    regex search)."""
    parts: list[str] = []
    if record.get("type") == "initial_input":
        parts.append(record.get("content", ""))
    elif record.get("item_type") == "message_output_item":
        raw = record.get("raw_content", {})
        if isinstance(raw, dict):
            role = raw.get("role", "")
            if role:
                parts.append(f"[{role}]")
            for c in raw.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    elif record.get("item_type") == "tool_call_item":
        raw = record.get("raw_content", {})
        if isinstance(raw, dict):
            tn = raw.get("name", "")
            if tn:
                parts.append(f"[Tool: {tn}]")
            args = raw.get("arguments", {})
            if args:
                parts.append(json.dumps(args, ensure_ascii=False)
                             if not isinstance(args, str) else args)
    elif record.get("item_type") == "tool_call_output_item":
        raw = record.get("raw_content", {})
        if isinstance(raw, dict):
            out = raw.get("output", "")
            if out:
                parts.append(str(out))
    elif record.get("item_type") == "user_input":
        parts.append(record.get("content", ""))
    return " ".join(parts)


def _extract_match_context(content: str, keywords: list[str],
                           context_length: int = 50) -> str:
    content_lower = content.lower()
    first_pos = len(content)
    matched_kw = ""
    for kw in keywords:
        pos = content_lower.find(kw.lower())
        if pos != -1 and pos < first_pos:
            first_pos = pos
            matched_kw = kw
    if first_pos == len(content):
        return content[:100] + "..." if len(content) > 100 else content
    start = max(0, first_pos - context_length)
    end = min(len(content), first_pos + len(matched_kw) + context_length)
    out = content[start:end]
    if start > 0:
        out = "..." + out
    if end < len(content):
        out = out + "..."
    return out


def _get_match_context(text: str, start: int, end: int,
                       context_size: int = 500) -> str:
    """Toolathlon parity: history_tools.get_match_context."""
    ctx_start = max(0, start - context_size // 2)
    ctx_end = min(len(text), end + context_size // 2)
    if ctx_start > 0:
        while ctx_start > 0 and text[ctx_start] not in " \n\t":
            ctx_start -= 1
    if ctx_end < len(text):
        while ctx_end < len(text) and text[ctx_end] not in " \n\t":
            ctx_end += 1
    prefix = "..." if ctx_start > 0 else ""
    suffix = "..." if ctx_end < len(text) else ""
    context = text[ctx_start:ctx_end].strip()
    h_start = start - ctx_start
    h_end = end - ctx_start
    if 0 <= h_start < len(context) and 0 < h_end <= len(context):
        context = (context[:h_start] + "**" + context[h_start:h_end]
                   + "**" + context[h_end:])
    return prefix + context + suffix


def _search_in_text(text: str, pattern: str,
                    is_regex: bool = True) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    try:
        if is_regex:
            for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
                matches.append((m.start(), m.end()))
        else:
            pl = pattern.lower()
            tl = text.lower()
            start = 0
            while True:
                pos = tl.find(pl, start)
                if pos == -1:
                    break
                matches.append((pos, pos + len(pattern)))
                start = pos + 1
    except re.error:
        return _search_in_text(text, pattern, is_regex=False)
    return matches


def _search_by_keywords(history: list[dict], keywords: list[str],
                         max_results: int | None, skip: int
                         ) -> tuple[list[dict], int]:
    """Toolathlon parity: HistoryManager.search_by_keywords."""
    matches: list[dict] = []
    kws_lower = [k.lower() for k in keywords]
    for rec in history:
        search_content = _extract_search_content(rec)
        if not search_content:
            continue
        sc_lower = search_content.lower()
        if all(k in sc_lower for k in kws_lower):
            rec = dict(rec)
            rec["match_context"] = _extract_match_context(
                search_content, keywords)
            matches.append(rec)
    total = len(matches)
    if skip > 0:
        matches = matches[skip:]
    if max_results is not None:
        matches = matches[:max_results]
    return matches, total


def _get_turn_details(history: list[dict], turn_number: int,
                      context_turns: int = 2) -> list[dict]:
    """Toolathlon parity: HistoryManager.get_turn_details."""
    if not any(r.get("turn", -1) == turn_number for r in history):
        return []
    turn_indices: dict[int, list[int]] = {}
    for i, rec in enumerate(history):
        t = rec.get("turn", -1)
        turn_indices.setdefault(t, []).append(i)
    min_turn = max(0, turn_number - context_turns)
    max_turn = turn_number + context_turns
    out: list[dict] = []
    for t in range(min_turn, max_turn + 1):
        for idx in turn_indices.get(t, []):
            rec = dict(history[idx])
            rec["is_target_turn"] = (t == turn_number)
            out.append(rec)
    return out


# Session caches for history search pagination
_history_search_sessions: dict[str, dict] = {}
_turn_search_sessions: dict[str, dict] = {}


# ----- local-search_history ------------------------------------------
# Source: Toolathlon/utils/aux_tools/history_tools.py L92-241
@mcp.tool(name="local-search_history")
def local_search_history(
    keywords: list[str] | None = None,
    use_regex: bool = False,
    page: int = 1,
    per_page: int = 10,
    search_id: str | None = None,
) -> dict:
    """Search history conversation records. Support multiple keyword search or
    regular expression search, return records containing all keywords. Support
    paging to browse all results."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    warning = None
    if search_id and search_id in _history_search_sessions:
        cached = _history_search_sessions[search_id]
        if keywords and keywords != cached["keywords"]:
            warning = (f"Provided keywords '{keywords}' ignored, using "
                       f"cached search conditions '{cached['keywords']}'")
        keywords = cached["keywords"]
        use_regex = cached.get("use_regex", False)
        per_page = cached.get("per_page", per_page)
    else:
        if not keywords:
            return {"status": "error",
                    "message": "Please provide keywords for search"}
        search_id = f"search_{uuid.uuid4().hex[:8]}"

    history = _load_transcript()
    skip = (page - 1) * per_page

    if use_regex:
        try:
            patterns = [re.compile(k, re.IGNORECASE | re.MULTILINE)
                        for k in keywords]
        except re.error as e:
            return {"status": "error",
                    "message": f"Invalid regex pattern: {e}"}
        matches: list[dict] = []
        for rec in history:
            content = _extract_searchable_content(rec)
            if content and all(p.search(content) for p in patterns):
                m = patterns[0].search(content)
                if m:
                    mc = _get_match_context(content, m.start(), m.end(), 250)
                    matches.append({**rec,
                                    "match_context": mc[:500] + "..." if len(mc) > 500 else mc})
        total = len(matches)
        matches = matches[skip:skip + per_page]
    else:
        matches, total = _search_by_keywords(history, keywords, per_page, skip)

    _history_search_sessions[search_id] = {
        "keywords": keywords,
        "use_regex": use_regex,
        "per_page": per_page,
        "total_matches": total,
        "created_at": json.dumps(datetime.now().isoformat()),
        "last_updated": datetime.now().isoformat(),
    }
    if len(_history_search_sessions) > 10:
        for old in sorted(_history_search_sessions.keys())[:len(_history_search_sessions) - 10]:
            del _history_search_sessions[old]

    results: list[dict] = []
    for m in matches:
        role = "unknown"
        if m.get("item_type") == "message_output_item":
            rc = m.get("raw_content", {})
            if isinstance(rc, dict):
                role = rc.get("role", "unknown")
        elif m.get("item_type") in ("initial_input", "user_input"):
            role = "user"
        elif m.get("item_type") == "tool_call_item":
            role = "assistant"
        elif m.get("item_type") == "tool_call_output_item":
            role = "tool"
        results.append({
            "turn": m.get("turn", -1),
            "timestamp": m.get("timestamp", "unknown"),
            "role": role,
            "preview": m.get("match_context", ""),
            "item_type": m.get("item_type", m.get("type", "unknown")),
        })
    total_pages = (total + per_page - 1) // per_page if per_page > 0 else 1
    return {
        "search_id": search_id,
        "keywords": keywords,
        "use_regex": use_regex,
        "total_matches": total,
        "total_pages": total_pages,
        "current_page": page,
        "per_page": per_page,
        "has_more": page < total_pages,
        "results": results,
        "warning": warning,
        "search_info": {
            "is_cached_search": search_id in _history_search_sessions,
            "last_updated": _history_search_sessions[search_id]["last_updated"],
            "search_type": "regex" if use_regex else "keyword",
        },
    }


# ----- local-view_history_turn ---------------------------------------
# Source: history_tools.py L243-336
@mcp.tool(name="local-view_history_turn")
def local_view_history_turn(
    turn: int,
    context_turns: int = 2,
    truncate: bool = True,
) -> dict:
    """View the complete conversation content of a specific turn, including the
    context of previous and subsequent turns. Support content truncation."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    if turn is None:
        return {"status": "error", "message": "Please provide the turn number"}
    history = _load_transcript()
    records = _get_turn_details(history, turn, context_turns)
    if not records:
        return {"status": "not_found",
                "message": f"No records found for turn {turn}"}
    out: list[dict] = []
    for rec in records:
        f = {"turn": rec.get("turn", -1),
             "timestamp": rec.get("timestamp", "unknown"),
             "is_target": rec.get("is_target_turn", False)}
        if rec.get("type") == "initial_input":
            f["type"] = "Initial Input"
            content = rec.get("content", "")
            f["content"] = _truncate_content(content) if truncate else content
            f["original_length"] = len(content)
        elif rec.get("item_type") == "message_output_item":
            f["type"] = "Message"
            raw = rec.get("raw_content", {})
            if isinstance(raw, dict):
                f["role"] = raw.get("role", "unknown")
                parts = []
                for c in raw.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        parts.append(c.get("text", ""))
                content = " ".join(parts)
                f["content"] = _truncate_content(content) if truncate else content
                f["original_length"] = len(content)
            else:
                f["role"] = "unknown"
                f["content"] = ""
                f["original_length"] = 0
        elif rec.get("item_type") == "tool_call_item":
            f["type"] = "Tool Call"
            raw = rec.get("raw_content", {})
            if isinstance(raw, dict):
                f["tool_name"] = raw.get("name", "unknown")
                args = raw.get("arguments", {})
                if args:
                    args_str = json.dumps(args, ensure_ascii=False, indent=2) \
                        if not isinstance(args, str) else args
                    f["arguments"] = _truncate_content(args_str) if truncate else args_str
                    f["original_length"] = len(args_str)
            else:
                f["tool_name"] = "unknown"
        elif rec.get("item_type") == "tool_call_output_item":
            f["type"] = "Tool Output"
            raw = rec.get("raw_content", {})
            if isinstance(raw, dict):
                output = str(raw.get("output", ""))
                f["output"] = _truncate_content(output) if truncate else output
                f["original_length"] = len(output)
            else:
                f["output"] = ""
                f["original_length"] = 0
        out.append(f)
    return {
        "status": "success",
        "target_turn": turn,
        "context_range": f"Displaying turn {turn - context_turns} to {turn + context_turns}",
        "truncated": truncate,
        "records": out,
    }


# ----- local-search_in_turn ------------------------------------------
# Source: history_tools.py L338-508
@mcp.tool(name="local-search_in_turn")
def local_search_in_turn(
    turn: int,
    pattern: str | None = None,
    page: int = 1,
    per_page: int = 10,
    search_id: str | None = None,
    jump_to: Any = None,
) -> dict:
    """Search content within a specific turn, support regular expressions."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    if turn is None:
        return {"status": "error", "message": "Please provide the turn number"}
    warning = None
    if search_id and search_id in _turn_search_sessions:
        cached = _turn_search_sessions[search_id]
        if pattern and pattern != cached["pattern"]:
            warning = (f"Provided pattern '{pattern}' ignored, using "
                       f"cached search pattern '{cached['pattern']}'")
        turn = cached["turn"]
        pattern = cached["pattern"]
        matches = cached["matches"]
        total = len(matches)
        if jump_to:
            tp = max(1, (total + per_page - 1) // per_page)
            if jump_to == "first":
                page = 1
            elif jump_to == "last":
                page = tp
            elif jump_to == "next":
                page = cached.get("current_page", 1) + 1
            elif jump_to == "prev":
                page = max(1, cached.get("current_page", 1) - 1)
            elif isinstance(jump_to, int):
                page = max(1, min(jump_to, tp))
        cached["current_page"] = page
    else:
        if not pattern:
            return {"status": "error",
                    "message": "Please provide the search pattern"}
        history = _load_transcript()
        records = _get_turn_details(history, turn, 0)
        if not records:
            return {"status": "not_found",
                    "message": f"No records found for turn {turn}"}
        all_matches: list[dict] = []
        for rec in records:
            content = ""
            rec_type = ""
            if rec.get("type") == "initial_input":
                content = rec.get("content", "")
                rec_type = "Initial Input"
            elif rec.get("item_type") == "message_output_item":
                raw = rec.get("raw_content", {})
                if isinstance(raw, dict):
                    parts = []
                    for c in raw.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "output_text":
                            parts.append(c.get("text", ""))
                    content = " ".join(parts)
                    rec_type = f"Message ({raw.get('role', 'unknown')})"
            elif rec.get("item_type") == "tool_call_item":
                raw = rec.get("raw_content", {})
                if isinstance(raw, dict):
                    content = f"Tool: {raw.get('name', 'unknown')}\n"
                    args = raw.get("arguments", {})
                    if args:
                        content += f"Arguments: {json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args}"
                rec_type = "Tool Call"
            elif rec.get("item_type") == "tool_call_output_item":
                raw = rec.get("raw_content", {})
                if isinstance(raw, dict):
                    content = str(raw.get("output", ""))
                rec_type = "Tool Output"
            if content:
                for start, end in _search_in_text(content, pattern, True):
                    mc = _get_match_context(content, start, end, 500)
                    all_matches.append({
                        "record_type": rec_type,
                        "position": f"Character {start}-{end}",
                        "match_text": content[start:end],
                        "context": mc,
                        "item_type": rec.get("item_type",
                                             rec.get("type", "unknown")),
                    })
        search_id = f"turn_search_{uuid.uuid4().hex[:8]}"
        matches = all_matches
        total = len(matches)
        _turn_search_sessions[search_id] = {
            "turn": turn, "pattern": pattern, "matches": matches,
            "current_page": page,
            "created_at": datetime.now().isoformat(),
        }
        if len(_turn_search_sessions) > 20:
            for old in sorted(_turn_search_sessions.keys(),
                              key=lambda x: _turn_search_sessions[x].get("created_at", ""))[:10]:
                del _turn_search_sessions[old]

    per_page = min(per_page, 20)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, total)
    page_matches = matches[start_idx:end_idx] if matches else []
    return {
        "status": "success",
        "search_id": search_id,
        "turn": turn,
        "pattern": pattern,
        "total_matches": total,
        "warning": warning,
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "has_next": page < total_pages,
            "has_prev": page > 1,
            "showing": f"{start_idx + 1}-{end_idx}" if page_matches else "0-0",
        },
        "matches": page_matches,
        "navigation_hint": "Use jump_to parameter to navigate: 'first', 'last', 'next', 'prev' or specific page number",
    }


# ----- local-history_stats -------------------------------------------
# Source: history_tools.py L510-528 + history_manager.py L192-243
@mcp.tool(name="local-history_stats")
def local_history_stats() -> dict:
    """Get statistics of history records, including total turns, time range,
    message type distribution, etc."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    history = _load_transcript()
    if not history:
        stats = {"total_records": 0, "total_turns": 0, "date_range": None}
    else:
        turns: set = set()
        roles: dict[str, int] = {}
        item_types: dict[str, int] = {}
        timestamps: list[str] = []
        for rec in history:
            if "turn" in rec:
                turns.add(rec["turn"])
            item_type = rec.get("item_type", rec.get("type", ""))
            role = "unknown"
            if item_type == "message_output_item":
                rc = rec.get("raw_content", {})
                if isinstance(rc, dict):
                    role = rc.get("role", "unknown")
            elif item_type in ("initial_input", "user_input"):
                role = "user"
            elif item_type == "tool_call_item":
                role = "assistant"
            elif item_type == "tool_call_output_item":
                role = "tool"
            roles[role] = roles.get(role, 0) + 1
            item_types[item_type or "unknown"] = item_types.get(item_type or "unknown", 0) + 1
            if "timestamp" in rec:
                timestamps.append(rec["timestamp"])
        date_range = None
        if timestamps:
            timestamps.sort()
            date_range = {"start": timestamps[0], "end": timestamps[-1],
                          "duration": "unknown"}
        stats = {
            "total_records": len(history),
            "total_turns": len(turns),
            "roles_distribution": roles,
            "item_types_distribution": item_types,
            "date_range": date_range,
            "file_size_bytes": _transcript_path().stat().st_size
                if _transcript_path().exists() else 0,
        }
    status = _read_context_status()
    stats["current_session"] = {
        "active_turns": int(status.get("turns_in_current_sequence")
                            or status.get("turn") or 0),
        "truncated_turns": int(status.get("truncated_turns") or 0),
        "started_at": status.get("started_at", "unknown"),
    }
    return stats


# ----- local-browse_history ------------------------------------------
# Source: history_tools.py L530-668
@mcp.tool(name="local-browse_history")
def local_browse_history(
    start_turn: int = 0,
    end_turn: int | None = None,
    limit: int = 20,
    direction: str = "forward",
    truncate: bool = True,
) -> dict:
    """Browse history records in chronological order."""
    if not _harness_initialized():
        return _harness_uninitialized_error()
    history = _load_transcript()
    turns_map: dict[int, list[dict]] = {}
    for rec in history:
        t = rec.get("turn", -1)
        turns_map.setdefault(t, []).append(rec)
    all_turns = sorted([t for t in turns_map if t >= 0])
    if not all_turns:
        return {"status": "empty", "message": "No history records"}
    if end_turn is None:
        end_turn = all_turns[-1]
    selected = [t for t in all_turns if start_turn <= t <= end_turn]
    if direction == "backward":
        selected.reverse()
    if len(selected) > limit:
        selected = selected[:limit]
    results: list[dict] = []
    for t in selected:
        recs = turns_map[t]
        summary = {
            "turn": t,
            "timestamp": recs[0].get("timestamp", "unknown") if recs else "unknown",
            "messages": [],
        }
        for rec in recs:
            if rec.get("type") == "user_input":
                summary["messages"].append({
                    "type": "user_input",
                    "content": rec.get("content", "unknown"),
                })
            if rec.get("item_type") == "message_output_item":
                raw = rec.get("raw_content", {})
                role = "unknown"
                content = ""
                if isinstance(raw, dict):
                    role = raw.get("role", "unknown")
                    parts = []
                    for c in raw.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "output_text":
                            parts.append(c.get("text", ""))
                    content = " ".join(parts)
                display = _truncate_content(content, 1000, 500) if truncate else content
                summary["messages"].append({
                    "role": role,
                    "content": display[:200] + "..." if len(display) > 200 else display,
                    "original_length": len(content),
                    "truncated": truncate and len(content) > 1000,
                })
            elif rec.get("item_type") == "tool_call_item":
                raw = rec.get("raw_content", {})
                tn = raw.get("name", "unknown") if isinstance(raw, dict) else "unknown"
                summary["messages"].append({"type": "tool_call", "tool": tn})
            elif rec.get("item_type") == "tool_call_output_item":
                raw = rec.get("raw_content", {})
                if isinstance(raw, dict):
                    output = str(raw.get("output", ""))
                    display = _truncate_content(output, 500, 250) if truncate else output
                    summary["messages"].append({
                        "type": "tool_output",
                        "preview": display[:100] + "..." if len(display) > 100 else display,
                        "original_length": len(output),
                        "truncated": truncate and len(output) > 500,
                    })
        results.append(summary)
    has_more_forward = end_turn < all_turns[-1] if direction == "forward" else start_turn > all_turns[0]
    has_more_backward = start_turn > all_turns[0] if direction == "forward" else end_turn < all_turns[-1]
    return {
        "status": "success",
        "direction": direction,
        "truncated": truncate,
        "turn_range": {
            "start": selected[0] if selected else start_turn,
            "end": selected[-1] if selected else end_turn,
            "total_returned": len(selected),
        },
        "navigation": {
            "has_more_forward": has_more_forward,
            "has_more_backward": has_more_backward,
            "total_turns_available": len(all_turns),
            "first_turn": all_turns[0],
            "last_turn": all_turns[-1],
        },
        "results": results,
    }


# ----- overlong-tool helpers + tools ---------------------------------
# Source: Toolathlon/utils/aux_tools/overlong_tool_manager.py
# Toolathlon stores per-shortuuid files as `.json` (despite contents being
# raw text). The harness here spools to `<id>.txt` for clarity, but we
# accept either extension for backward compatibility.

_SEARCH_PAGE_SIZE = 10
_VIEW_PAGE_SIZE = 10000
_MAX_VIEW_PAGE_SIZE = 100000
_OVERLONG_CONTEXT_SIZE = 1000
_overlong_search_sessions: dict[str, dict] = {}
_overlong_view_sessions: dict[str, dict] = {}


def _overlong_file_path(shortuuid: str) -> Path | None:
    base = _overlong_dir()
    for ext in (".txt", ".json"):
        p = base / f"{shortuuid}{ext}"
        if p.exists():
            return p
    return None


def _touch(p: Path) -> None:
    try:
        now = time.time()
        os.utime(p, (now, now))
    except Exception:
        pass


def _search_in_content(content: str, pattern: str,
                       context_size: int) -> list[dict]:
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")
    out: list[dict] = []
    for m in rx.finditer(content):
        start, end = m.start(), m.end()
        cs = max(0, start - context_size // 2)
        ce = min(len(content), end + context_size // 2)
        out.append({
            "match_text": content[start:end],
            "start_pos": start, "end_pos": end,
            "line_num": content[:start].count("\n") + 1,
            "before_context": content[cs:start],
            "after_context": content[end:ce],
            "context_start": cs, "context_end": ce,
        })
    return out


@mcp.tool(name="local-search_overlong_tooloutput")
def local_search_overlong_tooloutput(
    shortuuid: str,
    pattern: str,
    page_size: int = _SEARCH_PAGE_SIZE,
    context_size: int = _OVERLONG_CONTEXT_SIZE,
) -> str:
    """Search within overlong tool output content using regex patterns and
    return first page with session ID"""
    if not _harness_initialized():
        return json.dumps(_harness_uninitialized_error())
    shortuuid = (shortuuid or "").strip()
    pattern = (pattern or "").strip()
    if not shortuuid:
        return "Error: shortuuid parameter is required"
    if not pattern:
        return "Error: pattern parameter is required"
    if page_size < 1 or page_size > 50:
        return "Error: page_size must be between 1 and 50"
    fp = _overlong_file_path(shortuuid)
    if fp is None:
        return f"Error: No overlong tool output found for shortuuid: {shortuuid}"
    try:
        _touch(fp)
        content = fp.read_text(encoding="utf-8")
        matches = _search_in_content(content, pattern, context_size)
        if not matches:
            return (f"No matches found for pattern '{pattern}' in shortuuid: {shortuuid}\n"
                    f"File size: {len(content)} characters")
        sid = str(uuid.uuid4())[:8]
        _overlong_search_sessions[sid] = {
            "shortuuid": shortuuid, "pattern": pattern, "matches": matches,
            "page_size": page_size, "context_size": context_size,
            "content_length": len(content), "current_page": 1,
            "created_time": time.time(),
        }
        total = len(matches)
        total_pages = (total + page_size - 1) // page_size if total else 1
        page_matches = matches[:page_size]
        result = (f"Search Results in {shortuuid} (Page 1/{total_pages})\n"
                  f"Pattern: '{pattern}' | Total matches: {total} | "
                  f"File size: {len(content)} chars\n"
                  f"Search Session ID: {sid}\n"
                  + "=" * 80 + "\n\n")
        for i, m in enumerate(page_matches):
            num = i + 1
            result += (f"Match {num} (Line ~{m['line_num']}, Pos "
                       f"{m['start_pos']}-{m['end_pos']}):\n"
                       + "-" * 60 + "\n")
            ctx = m["before_context"] + f">>>{m['match_text']}<<<" + m["after_context"]
            if len(ctx) > context_size * 2:
                ctx = ctx[:context_size * 2] + "...[truncated]"
            result += ctx + "\n\n"
        result += (f"Use search_session_id '{sid}' with search_navigate tool for pagination\n"
                   "Available commands: next_page, prev_page, jump_to_page, first_page, last_page")
        return result
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error processing file for shortuuid {shortuuid}: {e}"


@mcp.tool(name="local-search_overlong_tooloutput_navigate")
def local_search_overlong_tooloutput_navigate(
    search_session_id: str,
    action: str = "next_page",
    target_page: int | None = None,
) -> str:
    """Navigate through search results using search session ID"""
    if not _harness_initialized():
        return json.dumps(_harness_uninitialized_error())
    search_session_id = (search_session_id or "").strip()
    action = (action or "next_page").strip().lower()
    if not search_session_id:
        return "Error: search_session_id parameter is required"
    if search_session_id not in _overlong_search_sessions:
        return f"Error: Invalid or expired search session ID: {search_session_id}"
    s = _overlong_search_sessions[search_session_id]
    matches = s["matches"]
    page_size = s["page_size"]
    total = len(matches)
    total_pages = (total + page_size - 1) // page_size if total else 1
    cur = s.get("current_page", 1)
    if action == "next_page":
        target_page = min(cur + 1, total_pages)
    elif action == "prev_page":
        target_page = max(cur - 1, 1)
    elif action == "first_page":
        target_page = 1
    elif action == "last_page":
        target_page = total_pages
    elif action == "jump_to_page":
        if target_page is None:
            return "Error: target_page parameter is required for jump_to_page action"
        if target_page < 1 or target_page > total_pages:
            return f"Error: target_page {target_page} must be between 1 and {total_pages}"
    else:
        return (f"Error: Invalid action '{action}'. Valid actions: "
                "next_page, prev_page, jump_to_page, first_page, last_page")
    s["current_page"] = target_page
    si = (target_page - 1) * page_size
    ei = min(si + page_size, total)
    pm = matches[si:ei]
    result = (f"Search Results in {s['shortuuid']} (Page {target_page}/{total_pages})\n"
              f"Pattern: '{s['pattern']}' | Total matches: {total} | "
              f"File size: {s['content_length']} chars\n"
              f"Search Session ID: {search_session_id}\n"
              + "=" * 80 + "\n\n")
    for i, m in enumerate(pm):
        num = si + i + 1
        result += (f"Match {num} (Line ~{m['line_num']}, Pos "
                   f"{m['start_pos']}-{m['end_pos']}):\n" + "-" * 60 + "\n")
        ctx = m["before_context"] + f">>>{m['match_text']}<<<" + m["after_context"]
        if len(ctx) > s["context_size"] * 2:
            ctx = ctx[:s["context_size"] * 2] + "...[truncated]"
        result += ctx + "\n\n"
    nav = []
    if target_page > 1:
        nav.append("prev_page")
    if target_page < total_pages:
        nav.append("next_page")
    nav.extend(["first_page", "last_page", "jump_to_page"])
    result += f"Available navigation: {', '.join(nav)}\n"
    result += f"Use search_session_id '{search_session_id}' to continue navigation"
    return result


@mcp.tool(name="local-view_overlong_tooloutput")
def local_view_overlong_tooloutput(
    shortuuid: str,
    page_size: int = _VIEW_PAGE_SIZE,
) -> str:
    """View overlong tool output content with pagination and return first page
    with session ID"""
    if not _harness_initialized():
        return json.dumps(_harness_uninitialized_error())
    shortuuid = (shortuuid or "").strip()
    if not shortuuid:
        return "Error: shortuuid parameter is required"
    if page_size < 1 or page_size > _MAX_VIEW_PAGE_SIZE:
        return f"Error: page_size must be between 1 and {_MAX_VIEW_PAGE_SIZE}"
    fp = _overlong_file_path(shortuuid)
    if fp is None:
        return f"Error: No overlong tool output found for shortuuid: {shortuuid}"
    try:
        _touch(fp)
        content = fp.read_text(encoding="utf-8")
        total = len(content)
        total_pages = (total + page_size - 1) // page_size if total else 1
        sid = str(uuid.uuid4())[:8]
        _overlong_view_sessions[sid] = {
            "shortuuid": shortuuid, "content_length": total,
            "page_size": page_size, "current_page": 1,
            "created_time": time.time(),
        }
        end_pos = min(page_size, total)
        excerpt = content[:end_pos]
        start_line = 1
        end_line = content[:end_pos].count("\n") + 1
        result = (f"Viewing {shortuuid} (Page 1/{total_pages})\n"
                  f"Characters 0-{end_pos} of {total} | "
                  f"Lines ~{start_line}-{end_line}\n"
                  f"View Session ID: {sid}\n"
                  + "=" * 80 + "\n\n"
                  + excerpt)
        if end_pos < total:
            result += (f"\n\n[Page 1 of {total_pages} - "
                       f"{total - end_pos} more characters available]\n"
                       f"Use view_session_id '{sid}' with view_navigate tool for pagination\n"
                       "Available commands: next_page, prev_page, jump_to_page, first_page, last_page")
        else:
            result += f"\n\n[End of file - {total} characters total]"
        return result
    except Exception as e:
        return f"Error reading file for shortuuid {shortuuid}: {e}"


@mcp.tool(name="local-view_overlong_tooloutput_navigate")
def local_view_overlong_tooloutput_navigate(
    view_session_id: str,
    action: str = "next_page",
    target_page: int | None = None,
) -> str:
    """Navigate through view content using view session ID"""
    if not _harness_initialized():
        return json.dumps(_harness_uninitialized_error())
    view_session_id = (view_session_id or "").strip()
    action = (action or "next_page").strip().lower()
    if not view_session_id:
        return "Error: view_session_id parameter is required"
    if view_session_id not in _overlong_view_sessions:
        return f"Error: Invalid or expired view session ID: {view_session_id}"
    s = _overlong_view_sessions[view_session_id]
    page_size = s["page_size"]
    total = s["content_length"]
    total_pages = (total + page_size - 1) // page_size if total else 1
    cur = s.get("current_page", 1)
    if action == "next_page":
        target_page = min(cur + 1, total_pages)
    elif action == "prev_page":
        target_page = max(cur - 1, 1)
    elif action == "first_page":
        target_page = 1
    elif action == "last_page":
        target_page = total_pages
    elif action == "jump_to_page":
        if target_page is None:
            return "Error: target_page parameter is required for jump_to_page action"
        if target_page < 1 or target_page > total_pages:
            return f"Error: target_page {target_page} must be between 1 and {total_pages}"
    else:
        return (f"Error: Invalid action '{action}'. Valid actions: "
                "next_page, prev_page, jump_to_page, first_page, last_page")
    s["current_page"] = target_page
    fp = _overlong_file_path(s["shortuuid"])
    if fp is None:
        return f"Error: file for {s['shortuuid']} is no longer available"
    try:
        _touch(fp)
        content = fp.read_text(encoding="utf-8")
        sp = (target_page - 1) * page_size
        ep = min(sp + page_size, total)
        excerpt = content[sp:ep]
        sl = content[:sp].count("\n") + 1
        el = content[:ep].count("\n") + 1
        result = (f"Viewing {s['shortuuid']} (Page {target_page}/{total_pages})\n"
                  f"Characters {sp}-{ep} of {total} | Lines ~{sl}-{el}\n"
                  f"View Session ID: {view_session_id}\n"
                  + "=" * 80 + "\n\n" + excerpt)
        if ep < total:
            result += (f"\n\n[Page {target_page} of {total_pages} - "
                       f"{total - ep} more characters available]\n")
        else:
            result += f"\n\n[End of file reached - {total} characters total]\n"
        nav = []
        if target_page > 1:
            nav.append("prev_page")
        if target_page < total_pages:
            nav.append("next_page")
        nav.extend(["first_page", "last_page", "jump_to_page"])
        result += f"Available navigation: {', '.join(nav)}\n"
        result += f"Use view_session_id '{view_session_id}' to continue navigation"
        return result
    except Exception as e:
        return f"Error reading file for shortuuid {s['shortuuid']}: {e}"


if __name__ == "__main__":
    mcp.run()
