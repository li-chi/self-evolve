"""Google Forms v1 served from the google-forms-mock state."""

from __future__ import annotations

import sys

from mockmod import load as _load_mock  # noqa: E402

gf = _load_mock("google-forms-mock")


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = gf._load_state()
    body = body if isinstance(body, dict) else {}
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["v1"]:
        parts = parts[1:]
    if parts[:1] != ["forms"]:
        raise NotImplementedError(f"forms facade: {method} {path}")

    if len(parts) == 1 and method == "POST":
        form_id = f"form{len(state.setdefault('forms', {})) + 1}"
        form = {"formId": form_id,
                "info": body.get("info", {"title": "Untitled form"}),
                "items": [],
                "responderUri":
                    f"https://docs.google.com/forms/d/{form_id}/viewform"}
        state["forms"][form_id] = form
        gf._save_state(state)
        return 200, form

    form_id = parts[1] if len(parts) > 1 else None
    form = state.get("forms", {}).get(form_id)
    if form is None:
        return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}
    tail = parts[2:]

    if not tail and method == "GET":
        return 200, form
    if tail and tail[0].endswith("batchUpdate") and method == "POST":
        for request in body.get("requests") or []:
            if "createItem" in request:
                form.setdefault("items", []).append(
                    request["createItem"].get("item", {}))
            elif "updateFormInfo" in request:
                form.setdefault("info", {}).update(
                    request["updateFormInfo"].get("info", {}))
        gf._save_state(state)
        return 200, {"form": form, "replies": []}
    if tail[:1] == ["responses"]:
        responses = [r for r in state.get("responses", {}).values()
                     if r.get("formId") == form_id]
        if len(tail) == 1 and method == "GET":
            return 200, {"responses": responses}
        if len(tail) == 2 and method == "GET":
            match = [r for r in responses if r.get("responseId") == tail[1]]
            return (200, match[0]) if match else (404, {"error": "not found"})

    raise NotImplementedError(f"forms facade: {method} {path}")
