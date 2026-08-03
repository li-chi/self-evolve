"""Google Forms mock MCP server.

Mirrors `matteoantoci/google-forms-mcp`, which is what Toolathlon uses
as its `google_forms` server. Every tool name and parameter matches the
official server; responses match the Google Forms API v1 response
shapes that the official server returns.

Upstream tool surface (5):

  create_form, add_text_question, add_multiple_choice_question,
  get_form, get_form_responses

Plus mock-only debug tools used by per-task setup/verification:

  mock_debug_state, mock_debug_seed_form, mock_debug_seed_response

State — one JSON file at $GFORMS_MOCK_STATE_DIR/state.json:

  state = {
    "forms": {
      "<formId>": {
        "formId", "revisionId",
        "info": {"title", "description", "documentTitle"},
        "settings": {"quizSettings": {"isQuiz": false}},
        "items": [
          {"itemId", "title",
           "questionItem": {"question": {
              "questionId", "required",
              "textQuestion": {"paragraph": false}
              | "choiceQuestion": {"type": "RADIO",
                                   "options": [{"value": "..."}]}}}}
        ],
        "responderUri": "https://docs.google.com/forms/d/.../viewform",
        "linkedSheetId": null
      }
    },
    "responses": {
      "<formId>": [
        {"responseId", "formId", "createTime", "lastSubmittedTime",
         "answers": {"<questionId>": {
            "questionId": "...",
            "textAnswers": {"answers": [{"value": "..."}]}}}}
      ]
    },
    "next_id": {...},
    "calls": [...]
  }

Return shapes mirror Google Forms API v1 (`forms.forms.*`) verbatim
where the upstream wraps the raw API response (`get_form`,
`get_form_responses`). `create_form`, `add_text_question`, and
`add_multiple_choice_question` return the simplified shapes that
upstream constructs (see `matteoantoci/google-forms-mcp` src/index.ts).
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "GFORMS_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gforms_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _empty_state() -> dict:
    return {
        "forms": {},
        "responses": {},
        "next_id": {"item": 1, "question": 1, "response": 1, "revision": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GFORMS_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    # A state file written as {} (or partially) by another process must
    # not KeyError downstream - merge the skeleton's missing keys.
    for k, v in _empty_state().items():
        s.setdefault(k, v)
    return s


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    lock_path = _state_path() + ".lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kwargs) -> None:
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


def _next_id(state: dict, kind: str) -> int:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return n


def _gen_form_id() -> str:
    # Real Google Forms IDs are 44-char URL-safe; the upstream returns
    # whatever the API returns, so we mimic length and charset.
    return "1" + secrets.token_urlsafe(33)[:43]


def _gen_response_id() -> str:
    # Real form response IDs look like "ACYDBNgX..." — a base64-ish blob.
    return "ACYDBN" + secrets.token_urlsafe(16)


def _gen_item_id() -> str:
    # Item IDs in the real API are 8 hex chars.
    return secrets.token_hex(4)


def _gen_question_id() -> str:
    # Question IDs in the real API are 8 hex chars.
    return secrets.token_hex(4)


def _bump_revision(form: dict) -> None:
    # Real API returns a base64-ish opaque token; mocked as a counter.
    n = int(form.get("_rev_seq", 0)) + 1
    form["_rev_seq"] = n
    form["revisionId"] = f"00000000{n:08d}"


def _new_form(form_id: str, title: str, description: str | None) -> dict:
    return {
        "formId": form_id,
        "info": {
            "title": title,
            "documentTitle": title,
            **({"description": description} if description else {}),
        },
        "settings": {"quizSettings": {"isQuiz": False}},
        "revisionId": "0000000000000001",
        "_rev_seq": 1,
        "responderUri":
            f"https://docs.google.com/forms/d/{form_id}/viewform",
        "items": [],
        "linkedSheetId": None,
    }


def _public_form(form: dict) -> dict:
    """Strip mock-internal fields before returning to the caller."""
    out = {k: v for k, v in form.items() if not k.startswith("_")}
    return out


mcp = FastMCP("google-forms-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Upstream tools
# ---------------------------------------------------------------------------

@mcp.tool(name="create_form")
def create_form(title: str, description: str | None = None) -> dict:
    """Create a new Google Form. Mirrors
    `matteoantoci/google-forms-mcp` `create_form`.

    Returns the simplified shape the upstream constructs:
      {"formId", "title", "description", "responderUri"}
    """
    with _lock():
        s = _load_state()
        form_id = _gen_form_id()
        form = _new_form(form_id, title, description)
        s["forms"][form_id] = form
        s["responses"].setdefault(form_id, [])
        _record(s, "create_form", form_id=form_id, title=title)
        _save_state(s)
        return {
            "formId": form_id,
            "title": title,
            "description": description or "",
            "responderUri": form["responderUri"],
        }


@mcp.tool(name="add_text_question")
def add_text_question(formId: str, questionTitle: str,
                      required: bool = False) -> dict:
    """Add a text (short answer) question to a form. Mirrors
    `matteoantoci/google-forms-mcp` `add_text_question`.

    Underlying API: `forms.forms.batchUpdate` with a single
    `createItem` request containing a `questionItem.textQuestion`.

    Returns the simplified upstream shape:
      {"success", "message", "questionTitle", "required"}
    """
    with _lock():
        s = _load_state()
        form = s["forms"].get(formId)
        if not form:
            _record(s, "add_text_question", form_id=formId,
                    result="not_found")
            _save_state(s)
            return {"success": False,
                    "message": f"Form not found: {formId}",
                    "questionTitle": questionTitle,
                    "required": bool(required)}
        item = {
            "itemId": _gen_item_id(),
            "title": questionTitle,
            "questionItem": {
                "question": {
                    "questionId": _gen_question_id(),
                    "required": bool(required),
                    "textQuestion": {"paragraph": False},
                },
            },
        }
        # Upstream inserts at index 0 (newest first). Mirror that.
        form["items"].insert(0, item)
        _bump_revision(form)
        _record(s, "add_text_question", form_id=formId,
                item_id=item["itemId"],
                question_id=item["questionItem"]["question"]["questionId"],
                title=questionTitle, required=bool(required))
        _save_state(s)
        return {
            "success": True,
            "message": f"Text question added to form {formId}",
            "questionTitle": questionTitle,
            "required": bool(required),
        }


@mcp.tool(name="add_multiple_choice_question")
def add_multiple_choice_question(formId: str, questionTitle: str,
                                 options: list[str],
                                 required: bool = False) -> dict:
    """Add a multiple-choice (RADIO) question to a form. Mirrors
    `matteoantoci/google-forms-mcp` `add_multiple_choice_question`.

    Underlying API: `forms.forms.batchUpdate` with a single
    `createItem` request containing a `questionItem.choiceQuestion`
    of `type: RADIO`.

    Returns the simplified upstream shape:
      {"success", "message", "questionTitle", "options", "required"}
    """
    with _lock():
        s = _load_state()
        form = s["forms"].get(formId)
        if not form:
            _record(s, "add_multiple_choice_question", form_id=formId,
                    result="not_found")
            _save_state(s)
            return {"success": False,
                    "message": f"Form not found: {formId}",
                    "questionTitle": questionTitle,
                    "options": list(options or []),
                    "required": bool(required)}
        item = {
            "itemId": _gen_item_id(),
            "title": questionTitle,
            "questionItem": {
                "question": {
                    "questionId": _gen_question_id(),
                    "required": bool(required),
                    "choiceQuestion": {
                        "type": "RADIO",
                        "options": [{"value": str(o)} for o in (options or [])],
                    },
                },
            },
        }
        form["items"].insert(0, item)
        _bump_revision(form)
        _record(s, "add_multiple_choice_question", form_id=formId,
                item_id=item["itemId"],
                question_id=item["questionItem"]["question"]["questionId"],
                title=questionTitle, options=list(options or []),
                required=bool(required))
        _save_state(s)
        return {
            "success": True,
            "message": f"Multiple choice question added to form {formId}",
            "questionTitle": questionTitle,
            "options": list(options or []),
            "required": bool(required),
        }


@mcp.tool(name="get_form")
def get_form(formId: str) -> dict:
    """Retrieve a form. Mirrors `matteoantoci/google-forms-mcp`
    `get_form`, which returns the raw `forms.forms.get` response.

    Returns the Google Forms API v1 Form resource verbatim.
    """
    with _lock():
        s = _load_state()
        form = s["forms"].get(formId)
        if not form:
            _record(s, "get_form", form_id=formId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found.",
                    "status": "NOT_FOUND",
                    "formId": formId,
                }
            }
        _record(s, "get_form", form_id=formId, result="ok",
                items=len(form.get("items", [])))
        _save_state(s)
        return _public_form(form)


@mcp.tool(name="get_form_responses")
def get_form_responses(formId: str) -> dict:
    """List all responses for a form. Mirrors
    `matteoantoci/google-forms-mcp` `get_form_responses`, which returns
    the raw `forms.forms.responses.list` response.

    Returns the Google Forms API v1
    `ListFormResponsesResponse` verbatim:
      {"responses": [<FormResponse>...]}.
    """
    with _lock():
        s = _load_state()
        if formId not in s["forms"]:
            _record(s, "get_form_responses", form_id=formId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": "Requested entity was not found.",
                    "status": "NOT_FOUND",
                    "formId": formId,
                }
            }
        items = s["responses"].get(formId, [])
        _record(s, "get_form_responses", form_id=formId,
                count=len(items))
        _save_state(s)
        return {"responses": list(items)}


# ---------------------------------------------------------------------------
# Debug helpers (mock-only)
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not in
    matteoantoci/google-forms-mcp."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed_form")
def mock_debug_seed_form(formId: str, title: str,
                         description: str | None = None,
                         items: list[dict] | None = None) -> dict:
    """Mock-only: insert a fully-formed Form fixture.

    `items` is a list of {"title", "type": "text"|"choice",
    "required"?: bool, "options"?: [str], "questionId"?: str,
    "itemId"?: str, "paragraph"?: bool}. Order is preserved (no
    auto-reverse).
    """
    with _lock():
        s = _load_state()
        form = _new_form(formId, title, description)
        for it in items or []:
            qtype = (it.get("type") or "text").lower()
            q = {
                "questionId": it.get("questionId") or _gen_question_id(),
                "required": bool(it.get("required", False)),
            }
            if qtype == "choice":
                q["choiceQuestion"] = {
                    "type": it.get("choice_type", "RADIO"),
                    "options": [{"value": str(o)}
                                for o in (it.get("options") or [])],
                }
            else:
                q["textQuestion"] = {"paragraph": bool(it.get("paragraph",
                                                              False))}
            form["items"].append({
                "itemId": it.get("itemId") or _gen_item_id(),
                "title": it.get("title", ""),
                **({"description": it["description"]}
                   if it.get("description") else {}),
                "questionItem": {"question": q},
            })
        s["forms"][formId] = form
        s["responses"].setdefault(formId, [])
        _record(s, "debug_seed_form", form_id=formId, title=title,
                items=len(form["items"]))
        _save_state(s)
        return _public_form(form)


@_debug_tool(name="mock_debug_seed_response")
def mock_debug_seed_response(formId: str,
                             answers: dict[str, Any],
                             responseId: str | None = None,
                             createTime: str | None = None) -> dict:
    """Mock-only: seed a Form response.

    `answers` is either:
      - {"<questionId>": "string value"} — shorthand for textAnswers
      - {"<questionId>": ["v1", "v2"]} — multi-select / multi-line text
      - {"<questionId>": {<full Answer object>}} — verbatim

    Returns the seeded FormResponse object (Google Forms API v1 shape).
    """
    with _lock():
        s = _load_state()
        if formId not in s["forms"]:
            _record(s, "debug_seed_response", form_id=formId,
                    result="form_not_found")
            _save_state(s)
            return {"error": f"Form not found: {formId}"}
        rid = responseId or _gen_response_id()
        ts = createTime or _now()
        norm_answers: dict[str, Any] = {}
        for qid, val in (answers or {}).items():
            if isinstance(val, dict):
                norm_answers[qid] = {"questionId": qid, **val}
            else:
                vals = val if isinstance(val, list) else [val]
                norm_answers[qid] = {
                    "questionId": qid,
                    "textAnswers": {
                        "answers": [{"value": str(v)} for v in vals],
                    },
                }
        resp = {
            "responseId": rid,
            "formId": formId,
            "createTime": ts,
            "lastSubmittedTime": ts,
            "answers": norm_answers,
        }
        s["responses"].setdefault(formId, []).append(resp)
        _record(s, "debug_seed_response", form_id=formId,
                response_id=rid, n_answers=len(norm_answers))
        _save_state(s)
        return resp


if __name__ == "__main__":
    mcp.run()
