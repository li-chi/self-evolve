"""Brevo (formerly Sendinblue) mock MCP server.

Mirrors the Brevo REST API v3 surface
(https://developers.brevo.com/reference/getting-started-1). Every tool
name matches the Brevo `operationId` convention (e.g. `sendTransacEmail`,
`getContactInfo`, `createSmtpTemplate`) and accepts the same parameter
shape as the real REST endpoint. Responses match Brevo's JSON shapes
(`id` integers for contacts/lists/templates, `messageId` strings like
`<201801301613.46577236487@smtp-relay.mailin.fr>` for transactional
emails, etc).

Backed by a single JSON state file (default
`$BREVO_MOCK_STATE_DIR/state.json`, falling back to
`~/.openclaw/brevo_mock/state.json`) holding all Brevo entities (contacts,
lists, templates, senders, transactional emails, SMS, blocked addresses)
plus a `calls` log used by the verifier.

Errors are returned as Brevo error objects, not raised, so the trace
mirrors a real failed HTTP response:
    {"code": "document_not_found", "message": "..."}

Per-rollout isolation should clear the state dir between rollouts.
Optional `BREVO_MOCK_SEED_PATH` preloads state when no `state.json`
exists yet. Every call (reads included) appends to `state["calls"]`
so verifiers can replay the trace.

Tools (18 + 2 mock helpers):

  Transactional Email
    sendTransacEmail, getTransacEmailsList, getTransacEmailContent,
    getTransacBlockedContacts, getSmtpReport
  Templates
    getSmtpTemplates, getSmtpTemplate, createSmtpTemplate,
    updateSmtpTemplate, deleteSmtpTemplate, sendTestTemplate
  Contacts
    getContacts, getContactInfo, createContact, updateContact,
    deleteContact
  Lists
    getLists, getList, createList, updateList, deleteList,
    getContactsFromList, addContactToList, removeContactFromList
  Senders
    getSenders, createSender
  SMS
    sendTransacSms, getTransacSmsActivity, getSmsCampaigns

Plus `mock_debug_state` and `mock_debug_seed`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import random
import re
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "BREVO_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/brevo_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "+00:00"))


def _empty_state() -> dict:
    return {
        "account": {
            "email": "mock@brevo.local",
            "firstName": "Mock",
            "lastName": "Account",
            "companyName": "Mock Co",
            "plan": [
                {"type": "free", "creditsType": "sendLimit",
                 "credits": 300, "startDate": None, "endDate": None}
            ],
        },
        "contacts": {},          # int id -> contact dict
        "contacts_by_email": {},  # lower-cased email -> id
        "lists": {},             # int id -> list dict
        "list_members": {},      # int list id -> set/list of contact ids
        "folders": {1: {"id": 1, "name": "Default Folder",
                        "totalSubscribers": 0, "totalBlacklisted": 0,
                        "uniqueSubscribers": 0}},
        "templates": {},         # int id -> template dict
        "senders": {},           # int id -> sender dict
        "transac_emails": [],    # list of {messageId, ...}
        "blocked_contacts": [],  # list of blocked-contact records
        "smtp_events": [],       # raw event log for getSmtpReport
        "sms_messages": [],      # transactional SMS sends
        "sms_campaigns": {},     # int id -> sms campaign dict
        "next_id": {
            "contact": 1, "list": 1, "template": 1,
            "sender": 1, "sms_campaign": 1, "message_seq": 1,
            "folder": 2,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("BREVO_MOCK_SEED_PATH")
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
        json.dump(state, f, indent=2, default=str)
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
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kwargs)
    state["calls"].append(entry)


def _err(code: str, message: str) -> dict:
    """Brevo-shaped error body."""
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# ID / lookup helpers
# ---------------------------------------------------------------------------

def _next_id(state: dict, kind: str) -> int:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return n


def _new_message_id(state: dict) -> str:
    """Brevo format: '<datetime.seq@smtp-relay.mailin.fr>'."""
    seq = _next_id(state, "message_seq")
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%d%H%M")
    # 14-digit numeric suffix mimicking real Brevo IDs
    suffix = f"{seq:014d}"
    return f"<{stamp}.{suffix}@smtp-relay.mailin.fr>"


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _find_contact(state: dict, identifier: str | int) -> dict | None:
    """Brevo accepts either contact id (int) or email (URL-encoded).
    The real API URL-decodes; we accept the raw string."""
    if identifier is None:
        return None
    # numeric / int-looking?
    try:
        cid = int(identifier)
        if cid in state["contacts"]:
            return state["contacts"][cid]
        if str(cid) in state["contacts"]:
            return state["contacts"][str(cid)]
    except (TypeError, ValueError):
        pass
    em = _norm_email(str(identifier))
    cid = state["contacts_by_email"].get(em)
    if cid is None:
        return None
    return state["contacts"].get(cid) or state["contacts"].get(str(cid))


def _contact_ids_as_int(state: dict) -> dict:
    """Helper: contacts may be stored with int or str keys after JSON
    round-trip. Yield (id_int, contact) pairs in id order."""
    pairs = []
    for k, v in state["contacts"].items():
        try:
            pairs.append((int(k), v))
        except (TypeError, ValueError):
            continue
    pairs.sort(key=lambda p: p[0])
    return pairs


def _find_list(state: dict, list_id: int | str) -> dict | None:
    try:
        lid = int(list_id)
    except (TypeError, ValueError):
        return None
    return state["lists"].get(lid) or state["lists"].get(str(lid))


def _find_template(state: dict, template_id: int | str) -> dict | None:
    try:
        tid = int(template_id)
    except (TypeError, ValueError):
        return None
    return state["templates"].get(tid) or state["templates"].get(str(tid))


def _list_members(state: dict, list_id: int) -> list[int]:
    """Return contact ids that belong to `list_id`."""
    out = []
    for cid, c in _contact_ids_as_int(state):
        if int(list_id) in (c.get("listIds") or []):
            out.append(cid)
    return out


def _recalc_list_totals(state: dict, list_id: int) -> None:
    L = _find_list(state, list_id)
    if not L:
        return
    members = _list_members(state, list_id)
    L["totalSubscribers"] = len(members)
    L["uniqueSubscribers"] = len(members)
    L["totalBlacklisted"] = sum(
        1 for cid in members
        if (state["contacts"].get(cid) or state["contacts"].get(str(cid))
            or {}).get("emailBlacklisted")
    )


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("brevo-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ===========================================================================
# Transactional Email
# ===========================================================================

@mcp.tool(name="sendTransacEmail")
def send_transac_email(sender: dict | None = None,
                       to: list | None = None,
                       bcc: list | None = None,
                       cc: list | None = None,
                       htmlContent: str | None = None,
                       textContent: str | None = None,
                       subject: str | None = None,
                       replyTo: dict | None = None,
                       attachment: list | None = None,
                       headers: dict | None = None,
                       templateId: int | None = None,
                       params: dict | None = None,
                       messageVersions: list | None = None,
                       tags: list | None = None,
                       scheduledAt: str | None = None,
                       batchId: str | None = None) -> dict:
    """Brevo REST: POST /smtp/email — send a transactional email.

    Returns {"messageId": "<...@smtp-relay.mailin.fr>"} on a single
    send, or {"messageIds": [...]} when `messageVersions` is supplied.
    Honors `templateId` (subject/htmlContent/sender pulled from the
    template if not overridden), `params` for handlebars-style
    substitution, and `scheduledAt` for delayed sends.
    """
    with _lock():
        s = _load_state()
        recipients = to or []
        if not recipients and not messageVersions:
            _record(s, "sendTransacEmail", result="bad_request",
                    reason="missing to")
            _save_state(s)
            return _err("missing_parameter",
                        "'to' is required (or 'messageVersions')")
        # Resolve template if given
        eff_subject = subject
        eff_html = htmlContent
        eff_text = textContent
        eff_sender = sender
        eff_reply = replyTo
        if templateId is not None:
            t = _find_template(s, templateId)
            if not t:
                _record(s, "sendTransacEmail", templateId=templateId,
                        result="template_not_found")
                _save_state(s)
                return _err("document_not_found",
                            f"Template ID {templateId} does not exist")
            if not t.get("isActive", True):
                _record(s, "sendTransacEmail", templateId=templateId,
                        result="template_inactive")
                _save_state(s)
                return _err("invalid_parameter",
                            f"Template ID {templateId} is not active")
            eff_subject = eff_subject or t.get("subject")
            eff_html = eff_html or t.get("htmlContent")
            eff_sender = eff_sender or t.get("sender")
            eff_reply = eff_reply or t.get("replyTo")
        # blocked-contact check (simple): refuse if every `to` is blocked
        blocked = {b.get("email") for b in s.get("blocked_contacts", [])}
        delivered = []
        for r in recipients:
            em = (r or {}).get("email")
            if em and em in blocked:
                continue
            delivered.append(r)
        if recipients and not delivered:
            _record(s, "sendTransacEmail", result="all_blocked",
                    recipients=[r.get("email") for r in recipients])
            _save_state(s)
            return _err("permission_denied",
                        "All recipients are blocked")
        # Build message records (one per version if messageVersions)
        out_ids = []
        versions = messageVersions or [None]
        for v in versions:
            mid = _new_message_id(s)
            rec_to = (v or {}).get("to") if v else delivered
            rec = {
                "messageId": mid,
                "to": rec_to,
                "cc": (v or {}).get("cc") if v else cc,
                "bcc": (v or {}).get("bcc") if v else bcc,
                "sender": eff_sender,
                "subject": ((v or {}).get("subject") if v else None)
                or eff_subject,
                "htmlContent": ((v or {}).get("htmlContent") if v else None)
                or eff_html,
                "textContent": eff_text,
                "replyTo": eff_reply,
                "templateId": templateId,
                "params": (v or {}).get("params") if v else params,
                "tags": tags or [],
                "headers": headers,
                "attachment": attachment,
                "scheduledAt": scheduledAt,
                "batchId": batchId,
                "date": _now_iso(),
                "status": ("scheduled" if scheduledAt
                           else ("sent" if not blocked else "sent")),
            }
            s["transac_emails"].append(rec)
            # also push an smtp_events entry per recipient
            for r in (rec_to or []):
                s["smtp_events"].append({
                    "date": rec["date"],
                    "messageId": mid,
                    "email": (r or {}).get("email"),
                    "event": "requests",
                    "subject": rec["subject"],
                    "tag": (tags[0] if tags else ""),
                    "templateId": templateId,
                })
            out_ids.append(mid)
        _record(s, "sendTransacEmail",
                messageIds=out_ids, templateId=templateId,
                tags=tags, scheduledAt=scheduledAt)
        _save_state(s)
        if messageVersions:
            return {"messageIds": out_ids}
        return {"messageId": out_ids[0]}


@mcp.tool(name="getTransacEmailsList")
def get_transac_emails_list(email: str | None = None,
                            templateId: int | None = None,
                            messageId: str | None = None,
                            startDate: str | None = None,
                            endDate: str | None = None,
                            sort: str = "desc",
                            limit: int = 50,
                            offset: int = 0) -> dict:
    """Brevo REST: GET /smtp/emails — list transactional emails sent,
    with optional filters. Returns `{transactionalEmails: [...], count}`.
    """
    with _lock():
        s = _load_state()
        items = list(s.get("transac_emails", []))
        if email:
            em = email.lower()
            items = [m for m in items
                     if any((r or {}).get("email", "").lower() == em
                            for r in (m.get("to") or []))]
        if templateId is not None:
            items = [m for m in items
                     if str(m.get("templateId")) == str(templateId)]
        if messageId:
            items = [m for m in items if m.get("messageId") == messageId]
        if startDate:
            items = [m for m in items
                     if (m.get("date") or "") >= startDate]
        if endDate:
            items = [m for m in items
                     if (m.get("date") or "") <= endDate]
        items.sort(key=lambda m: m.get("date") or "",
                   reverse=(sort != "asc"))
        total = len(items)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 1000))
        page = items[offset: offset + limit]
        # Brevo shape: shallow record per email
        out = []
        for m in page:
            tos = m.get("to") or []
            first = tos[0] if tos else {}
            out.append({
                "email": first.get("email", ""),
                "subject": m.get("subject", ""),
                "templateId": m.get("templateId"),
                "messageId": m.get("messageId"),
                "uuid": hashlib.md5(
                    (m.get("messageId") or "").encode("utf-8")
                ).hexdigest(),
                "date": m.get("date"),
                "from": (m.get("sender") or {}).get("email"),
                "tags": m.get("tags", []),
            })
        _record(s, "getTransacEmailsList", count=len(out),
                filters={"email": email, "templateId": templateId,
                         "messageId": messageId})
        _save_state(s)
        return {"transactionalEmails": out, "count": total}


@mcp.tool(name="getTransacEmailContent")
def get_transac_email_content(uuid: str) -> dict:
    """Brevo REST: GET /smtp/emails/{uuid} — retrieve full content
    of a sent transactional email by its uuid (md5 of messageId in this
    mock)."""
    with _lock():
        s = _load_state()
        match = None
        for m in s.get("transac_emails", []):
            mid = m.get("messageId") or ""
            if hashlib.md5(mid.encode("utf-8")).hexdigest() == uuid:
                match = m
                break
        if not match:
            _record(s, "getTransacEmailContent", uuid=uuid,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"No transactional email with uuid={uuid}")
        tos = match.get("to") or []
        first = tos[0] if tos else {}
        body = {
            "email": first.get("email", ""),
            "subject": match.get("subject", ""),
            "templateId": match.get("templateId"),
            "date": match.get("date"),
            "events": [
                {"name": "requests", "time": match.get("date")},
            ],
            "body": match.get("htmlContent", ""),
            "attachmentCount": len(match.get("attachment") or []),
        }
        _record(s, "getTransacEmailContent", uuid=uuid,
                messageId=match.get("messageId"))
        _save_state(s)
        return body


@mcp.tool(name="getTransacBlockedContacts")
def get_transac_blocked_contacts(startDate: str | None = None,
                                 endDate: str | None = None,
                                 senders: list | None = None,
                                 limit: int = 50,
                                 offset: int = 0,
                                 sort: str = "desc") -> dict:
    """Brevo REST: GET /smtp/blockedContacts — list contacts blocked
    from receiving transactional emails."""
    with _lock():
        s = _load_state()
        items = list(s.get("blocked_contacts", []))
        if startDate:
            items = [b for b in items
                     if (b.get("blockedAt") or "") >= startDate]
        if endDate:
            items = [b for b in items
                     if (b.get("blockedAt") or "") <= endDate]
        if senders:
            ss = set(senders)
            items = [b for b in items if b.get("senderEmail") in ss]
        items.sort(key=lambda b: b.get("blockedAt") or "",
                   reverse=(sort != "asc"))
        total = len(items)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 100))
        page = items[offset: offset + limit]
        _record(s, "getTransacBlockedContacts", count=len(page))
        _save_state(s)
        return {"contacts": page, "count": total}


@mcp.tool(name="getSmtpReport")
def get_smtp_report(limit: int = 10,
                    offset: int = 0,
                    startDate: str | None = None,
                    endDate: str | None = None,
                    days: int | None = None,
                    tag: str | None = None,
                    sort: str = "desc") -> dict:
    """Brevo REST: GET /smtp/statistics/reports — aggregated daily SMTP
    stats: requests, delivered, opens, clicks, etc. Returns
    `{reports: [{date, requests, delivered, hardBounces, ...}, ...]}`.
    """
    with _lock():
        s = _load_state()
        events = list(s.get("smtp_events", []))
        if tag:
            events = [e for e in events if e.get("tag") == tag]
        if startDate:
            events = [e for e in events
                      if (e.get("date") or "") >= startDate]
        if endDate:
            events = [e for e in events
                      if (e.get("date") or "") <= endDate]
        if days:
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=int(days))).isoformat()
            events = [e for e in events
                      if (e.get("date") or "") >= cutoff]
        # Bucket by date (YYYY-MM-DD)
        buckets: dict[str, dict] = {}
        for e in events:
            d = (e.get("date") or "")[:10]
            if not d:
                continue
            b = buckets.setdefault(d, {
                "date": d,
                "requests": 0, "delivered": 0, "hardBounces": 0,
                "softBounces": 0, "clicks": 0, "uniqueClicks": 0,
                "opens": 0, "uniqueOpens": 0, "spamReports": 0,
                "blocked": 0, "invalid": 0, "unsubscribed": 0,
            })
            evname = e.get("event") or "requests"
            if evname in b:
                b[evname] += 1
            else:
                b["requests"] += 1
        reports = list(buckets.values())
        reports.sort(key=lambda r: r["date"], reverse=(sort != "asc"))
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 10), 30))
        page = reports[offset: offset + limit]
        _record(s, "getSmtpReport", days=days, tag=tag,
                count=len(page))
        _save_state(s)
        return {"reports": page}


# ===========================================================================
# Templates
# ===========================================================================

@mcp.tool(name="getSmtpTemplates")
def get_smtp_templates(templateStatus: bool | None = None,
                       limit: int = 50,
                       offset: int = 0,
                       sort: str = "desc") -> dict:
    """Brevo REST: GET /smtp/templates — list email templates."""
    with _lock():
        s = _load_state()
        items = [t for t in s["templates"].values()]
        if templateStatus is not None:
            items = [t for t in items
                     if bool(t.get("isActive")) == bool(templateStatus)]
        items.sort(key=lambda t: t.get("createdAt") or "",
                   reverse=(sort != "asc"))
        total = len(items)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 1000))
        page = items[offset: offset + limit]
        _record(s, "getSmtpTemplates", count=len(page))
        _save_state(s)
        return {"templates": page, "count": total}


@mcp.tool(name="getSmtpTemplate")
def get_smtp_template(templateId: int) -> dict:
    """Brevo REST: GET /smtp/templates/{templateId} — retrieve one
    template by id."""
    with _lock():
        s = _load_state()
        t = _find_template(s, templateId)
        _record(s, "getSmtpTemplate", templateId=templateId,
                result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return _err("document_not_found",
                        f"Template ID {templateId} does not exist")
        return dict(t)


@mcp.tool(name="createSmtpTemplate")
def create_smtp_template(templateName: str,
                         subject: str,
                         sender: dict,
                         htmlContent: str | None = None,
                         htmlUrl: str | None = None,
                         replyTo: str | None = None,
                         toField: str | None = None,
                         tag: str | None = None,
                         isActive: bool = True,
                         attachmentUrl: str | None = None) -> dict:
    """Brevo REST: POST /smtp/templates — create a new transactional
    email template. Returns `{id}`."""
    with _lock():
        s = _load_state()
        if not htmlContent and not htmlUrl:
            _record(s, "createSmtpTemplate", result="bad_request",
                    reason="missing html")
            _save_state(s)
            return _err("missing_parameter",
                        "Either 'htmlContent' or 'htmlUrl' is required")
        if not sender or not isinstance(sender, dict):
            _record(s, "createSmtpTemplate", result="bad_request",
                    reason="missing sender")
            _save_state(s)
            return _err("missing_parameter", "'sender' is required")
        tid = _next_id(s, "template")
        now = _now_iso()
        t = {
            "id": tid,
            "name": templateName,
            "subject": subject,
            "isActive": bool(isActive),
            "testSent": False,
            "sender": sender,
            "replyTo": replyTo or "",
            "toField": toField or "",
            "tag": tag or "",
            "htmlContent": htmlContent or "",
            "htmlUrl": htmlUrl,
            "attachmentUrl": attachmentUrl,
            "createdAt": now,
            "modifiedAt": now,
            "doiTemplate": False,
        }
        s["templates"][tid] = t
        _record(s, "createSmtpTemplate", id=tid, name=templateName)
        _save_state(s)
        return {"id": tid}


@mcp.tool(name="updateSmtpTemplate")
def update_smtp_template(templateId: int,
                         templateName: str | None = None,
                         subject: str | None = None,
                         sender: dict | None = None,
                         htmlContent: str | None = None,
                         htmlUrl: str | None = None,
                         replyTo: str | None = None,
                         toField: str | None = None,
                         tag: str | None = None,
                         isActive: bool | None = None,
                         attachmentUrl: str | None = None) -> dict:
    """Brevo REST: PUT /smtp/templates/{templateId} — update an existing
    template. Returns empty body on success (HTTP 204 in the real API)."""
    with _lock():
        s = _load_state()
        t = _find_template(s, templateId)
        if not t:
            _record(s, "updateSmtpTemplate", templateId=templateId,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"Template ID {templateId} does not exist")
        if templateName is not None:
            t["name"] = templateName
        if subject is not None:
            t["subject"] = subject
        if sender is not None:
            t["sender"] = sender
        if htmlContent is not None:
            t["htmlContent"] = htmlContent
        if htmlUrl is not None:
            t["htmlUrl"] = htmlUrl
        if replyTo is not None:
            t["replyTo"] = replyTo
        if toField is not None:
            t["toField"] = toField
        if tag is not None:
            t["tag"] = tag
        if isActive is not None:
            t["isActive"] = bool(isActive)
        if attachmentUrl is not None:
            t["attachmentUrl"] = attachmentUrl
        t["modifiedAt"] = _now_iso()
        _record(s, "updateSmtpTemplate", templateId=templateId)
        _save_state(s)
        return {}


@mcp.tool(name="deleteSmtpTemplate")
def delete_smtp_template(templateId: int) -> dict:
    """Brevo REST: DELETE /smtp/templates/{templateId} — delete an
    email template."""
    with _lock():
        s = _load_state()
        t = _find_template(s, templateId)
        if not t:
            _record(s, "deleteSmtpTemplate", templateId=templateId,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"Template ID {templateId} does not exist")
        try:
            del s["templates"][int(templateId)]
        except KeyError:
            s["templates"].pop(str(templateId), None)
        _record(s, "deleteSmtpTemplate", templateId=templateId)
        _save_state(s)
        return {}


@mcp.tool(name="sendTestTemplate")
def send_test_template(templateId: int,
                       emailTo: list | None = None) -> dict:
    """Brevo REST: POST /smtp/templates/{templateId}/sendTest — send a
    test email of a template to a list of addresses."""
    with _lock():
        s = _load_state()
        t = _find_template(s, templateId)
        if not t:
            _record(s, "sendTestTemplate", templateId=templateId,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"Template ID {templateId} does not exist")
        recipients = emailTo or []
        if not recipients:
            _record(s, "sendTestTemplate", templateId=templateId,
                    result="bad_request")
            _save_state(s)
            return _err("missing_parameter",
                        "'emailTo' is required")
        mid = _new_message_id(s)
        s["transac_emails"].append({
            "messageId": mid,
            "to": [{"email": e} for e in recipients],
            "sender": t.get("sender"),
            "subject": "[Test] " + (t.get("subject") or ""),
            "htmlContent": t.get("htmlContent"),
            "templateId": int(templateId),
            "tags": ["test-template"],
            "date": _now_iso(),
            "status": "sent",
        })
        t["testSent"] = True
        _record(s, "sendTestTemplate", templateId=templateId,
                recipients=recipients, messageId=mid)
        _save_state(s)
        return {}


# ===========================================================================
# Contacts
# ===========================================================================

@mcp.tool(name="getContacts")
def get_contacts(limit: int = 50,
                 offset: int = 0,
                 modifiedSince: str | None = None,
                 createdSince: str | None = None,
                 segmentId: int | None = None,
                 listIds: list | None = None,
                 filter: str | None = None,
                 sort: str = "desc") -> dict:
    """Brevo REST: GET /contacts — list all contacts (optionally filter
    by modification date / segment / lists). Returns `{contacts, count}`."""
    with _lock():
        s = _load_state()
        items = [c for _, c in _contact_ids_as_int(s)]
        if modifiedSince:
            items = [c for c in items
                     if (c.get("modifiedAt") or "") >= modifiedSince]
        if createdSince:
            items = [c for c in items
                     if (c.get("createdAt") or "") >= createdSince]
        if listIds:
            wanted = {int(x) for x in listIds}
            items = [c for c in items
                     if wanted.intersection(c.get("listIds") or [])]
        items.sort(key=lambda c: c.get("modifiedAt") or "",
                   reverse=(sort != "asc"))
        total = len(items)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 1000))
        page = items[offset: offset + limit]
        _record(s, "getContacts", count=len(page),
                filters={"listIds": listIds, "modifiedSince": modifiedSince})
        _save_state(s)
        return {"contacts": page, "count": total}


@mcp.tool(name="getContactInfo")
def get_contact_info(identifier: str,
                     startDate: str | None = None,
                     endDate: str | None = None,
                     limit: int = 50,
                     offset: int = 0) -> dict:
    """Brevo REST: GET /contacts/{identifier} — retrieve a single
    contact by email (URL-encoded) or contact id."""
    with _lock():
        s = _load_state()
        c = _find_contact(s, identifier)
        _record(s, "getContactInfo", identifier=identifier,
                result="ok" if c else "not_found")
        _save_state(s)
        if not c:
            return _err("document_not_found",
                        f"Contact does not exist: {identifier}")
        return dict(c)


@mcp.tool(name="createContact")
def create_contact(email: str | None = None,
                   ext_id: str | None = None,
                   attributes: dict | None = None,
                   emailBlacklisted: bool = False,
                   smsBlacklisted: bool = False,
                   listIds: list | None = None,
                   updateEnabled: bool = False,
                   smtpBlacklistSender: list | None = None) -> dict:
    """Brevo REST: POST /contacts — create a new contact. If
    `updateEnabled=true`, an existing contact with the same email is
    updated instead. Returns `{id}`."""
    with _lock():
        s = _load_state()
        em = _norm_email(email)
        if not em and not ext_id:
            _record(s, "createContact", result="bad_request",
                    reason="missing email/ext_id")
            _save_state(s)
            return _err("missing_parameter",
                        "Either 'email' or 'ext_id' is required")
        existing = _find_contact(s, em) if em else None
        if existing:
            if not updateEnabled:
                _record(s, "createContact", email=em,
                        result="duplicate")
                _save_state(s)
                return _err("duplicate_parameter",
                            f"Contact already exists: {email}")
            # update path
            if attributes:
                existing.setdefault("attributes", {}).update(attributes)
            if listIds:
                merged = set(existing.get("listIds") or [])
                merged.update(int(x) for x in listIds)
                existing["listIds"] = sorted(merged)
            existing["emailBlacklisted"] = bool(emailBlacklisted)
            existing["smsBlacklisted"] = bool(smsBlacklisted)
            existing["modifiedAt"] = _now_iso()
            for lid in (listIds or []):
                _recalc_list_totals(s, int(lid))
            _record(s, "createContact", email=em, updated=True,
                    id=existing["id"])
            _save_state(s)
            return {"id": existing["id"]}
        # validate listIds
        unknown = []
        for lid in (listIds or []):
            if not _find_list(s, lid):
                unknown.append(lid)
        if unknown:
            _record(s, "createContact", email=em,
                    result="unknown_lists", lists=unknown)
            _save_state(s)
            return _err("invalid_parameter",
                        f"Unknown list ids: {unknown}")
        cid = _next_id(s, "contact")
        now = _now_iso()
        c = {
            "id": cid,
            "email": email or "",
            "emailBlacklisted": bool(emailBlacklisted),
            "smsBlacklisted": bool(smsBlacklisted),
            "createdAt": now,
            "modifiedAt": now,
            "listIds": sorted({int(x) for x in (listIds or [])}),
            "listUnsubscribed": [],
            "attributes": dict(attributes or {}),
        }
        s["contacts"][cid] = c
        if em:
            s["contacts_by_email"][em] = cid
        for lid in c["listIds"]:
            _recalc_list_totals(s, lid)
        _record(s, "createContact", email=em, id=cid,
                listIds=c["listIds"])
        _save_state(s)
        return {"id": cid}


@mcp.tool(name="updateContact")
def update_contact(identifier: str,
                   attributes: dict | None = None,
                   emailBlacklisted: bool | None = None,
                   smsBlacklisted: bool | None = None,
                   listIds: list | None = None,
                   unlinkListIds: list | None = None,
                   smtpBlacklistSender: list | None = None,
                   ext_id: str | None = None) -> dict:
    """Brevo REST: PUT /contacts/{identifier} — update an existing
    contact. Returns empty body on success."""
    with _lock():
        s = _load_state()
        c = _find_contact(s, identifier)
        if not c:
            _record(s, "updateContact", identifier=identifier,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"Contact does not exist: {identifier}")
        if attributes is not None:
            c.setdefault("attributes", {}).update(attributes)
        if emailBlacklisted is not None:
            c["emailBlacklisted"] = bool(emailBlacklisted)
        if smsBlacklisted is not None:
            c["smsBlacklisted"] = bool(smsBlacklisted)
        touched_lists = set()
        if listIds:
            current = set(c.get("listIds") or [])
            for lid in listIds:
                lid_i = int(lid)
                if not _find_list(s, lid_i):
                    _record(s, "updateContact", identifier=identifier,
                            result="unknown_list", list_id=lid_i)
                    _save_state(s)
                    return _err("invalid_parameter",
                                f"Unknown list id: {lid_i}")
                current.add(lid_i)
                touched_lists.add(lid_i)
            c["listIds"] = sorted(current)
        if unlinkListIds:
            current = set(c.get("listIds") or [])
            for lid in unlinkListIds:
                lid_i = int(lid)
                current.discard(lid_i)
                touched_lists.add(lid_i)
            c["listIds"] = sorted(current)
        c["modifiedAt"] = _now_iso()
        for lid in touched_lists:
            _recalc_list_totals(s, lid)
        _record(s, "updateContact", identifier=identifier,
                listIds=listIds, unlinkListIds=unlinkListIds)
        _save_state(s)
        return {}


@mcp.tool(name="deleteContact")
def delete_contact(identifier: str) -> dict:
    """Brevo REST: DELETE /contacts/{identifier} — delete a contact."""
    with _lock():
        s = _load_state()
        c = _find_contact(s, identifier)
        if not c:
            _record(s, "deleteContact", identifier=identifier,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"Contact does not exist: {identifier}")
        touched = list(c.get("listIds") or [])
        cid = c["id"]
        em = _norm_email(c.get("email"))
        s["contacts"].pop(cid, None)
        s["contacts"].pop(str(cid), None)
        if em:
            s["contacts_by_email"].pop(em, None)
        for lid in touched:
            _recalc_list_totals(s, lid)
        _record(s, "deleteContact", identifier=identifier, id=cid)
        _save_state(s)
        return {}


# ===========================================================================
# Lists
# ===========================================================================

@mcp.tool(name="getLists")
def get_lists(limit: int = 10,
              offset: int = 0,
              sort: str = "desc") -> dict:
    """Brevo REST: GET /contacts/lists — list all contact lists."""
    with _lock():
        s = _load_state()
        items = list(s["lists"].values())
        items.sort(key=lambda L: L.get("id", 0),
                   reverse=(sort != "asc"))
        total = len(items)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 10), 50))
        page = items[offset: offset + limit]
        _record(s, "getLists", count=len(page))
        _save_state(s)
        return {"lists": page, "count": total}


@mcp.tool(name="getList")
def get_list(listId: int) -> dict:
    """Brevo REST: GET /contacts/lists/{listId} — retrieve a list."""
    with _lock():
        s = _load_state()
        L = _find_list(s, listId)
        _record(s, "getList", listId=listId,
                result="ok" if L else "not_found")
        _save_state(s)
        if not L:
            return _err("document_not_found",
                        f"List ID {listId} does not exist")
        return dict(L)


@mcp.tool(name="createList")
def create_list(name: str, folderId: int = 1) -> dict:
    """Brevo REST: POST /contacts/lists — create a new contact list.
    Returns `{id}`."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "createList", result="bad_request")
            _save_state(s)
            return _err("missing_parameter", "'name' is required")
        # folder check
        fid = int(folderId)
        if fid not in s["folders"] and str(fid) not in s["folders"]:
            _record(s, "createList", result="folder_not_found",
                    folderId=fid)
            _save_state(s)
            return _err("document_not_found",
                        f"Folder ID {fid} does not exist")
        lid = _next_id(s, "list")
        now = _now_iso()
        L = {
            "id": lid,
            "name": name,
            "folderId": fid,
            "totalSubscribers": 0,
            "totalBlacklisted": 0,
            "uniqueSubscribers": 0,
            "createdAt": now,
            "campaignStats": [],
            "dynamicList": False,
        }
        s["lists"][lid] = L
        _record(s, "createList", id=lid, name=name, folderId=fid)
        _save_state(s)
        return {"id": lid}


@mcp.tool(name="updateList")
def update_list(listId: int,
                name: str | None = None,
                folderId: int | None = None) -> dict:
    """Brevo REST: PUT /contacts/lists/{listId} — rename a list or
    move it between folders."""
    with _lock():
        s = _load_state()
        L = _find_list(s, listId)
        if not L:
            _record(s, "updateList", listId=listId, result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"List ID {listId} does not exist")
        if name is not None:
            L["name"] = name
        if folderId is not None:
            fid = int(folderId)
            if fid not in s["folders"] and str(fid) not in s["folders"]:
                _record(s, "updateList", listId=listId,
                        result="folder_not_found", folderId=fid)
                _save_state(s)
                return _err("document_not_found",
                            f"Folder ID {fid} does not exist")
            L["folderId"] = fid
        _record(s, "updateList", listId=listId, name=name, folderId=folderId)
        _save_state(s)
        return {}


@mcp.tool(name="deleteList")
def delete_list(listId: int) -> dict:
    """Brevo REST: DELETE /contacts/lists/{listId} — delete a list."""
    with _lock():
        s = _load_state()
        L = _find_list(s, listId)
        if not L:
            _record(s, "deleteList", listId=listId, result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"List ID {listId} does not exist")
        lid_i = int(listId)
        try:
            del s["lists"][lid_i]
        except KeyError:
            s["lists"].pop(str(lid_i), None)
        # also strip from contact.listIds
        for _, c in _contact_ids_as_int(s):
            if lid_i in (c.get("listIds") or []):
                c["listIds"] = [x for x in c["listIds"] if x != lid_i]
                c["modifiedAt"] = _now_iso()
        _record(s, "deleteList", listId=lid_i)
        _save_state(s)
        return {}


@mcp.tool(name="getContactsFromList")
def get_contacts_from_list(listId: int,
                           modifiedSince: str | None = None,
                           limit: int = 50,
                           offset: int = 0,
                           sort: str = "desc") -> dict:
    """Brevo REST: GET /contacts/lists/{listId}/contacts — list
    contacts that belong to a list."""
    with _lock():
        s = _load_state()
        L = _find_list(s, listId)
        if not L:
            _record(s, "getContactsFromList", listId=listId,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"List ID {listId} does not exist")
        member_ids = _list_members(s, int(listId))
        contacts = [s["contacts"].get(i) or s["contacts"].get(str(i))
                    for i in member_ids]
        contacts = [c for c in contacts if c]
        if modifiedSince:
            contacts = [c for c in contacts
                        if (c.get("modifiedAt") or "") >= modifiedSince]
        contacts.sort(key=lambda c: c.get("modifiedAt") or "",
                      reverse=(sort != "asc"))
        total = len(contacts)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 500))
        page = contacts[offset: offset + limit]
        _record(s, "getContactsFromList", listId=listId, count=len(page))
        _save_state(s)
        return {"contacts": page, "count": total}


@mcp.tool(name="addContactToList")
def add_contact_to_list(listId: int,
                        emails: list | None = None,
                        ids: list | None = None,
                        all: bool = False) -> dict:
    """Brevo REST: POST /contacts/lists/{listId}/contacts/add — add
    contacts (by email or id) to a list. Returns
    `{contacts: {success: [...], failure: [...]}}`."""
    with _lock():
        s = _load_state()
        L = _find_list(s, listId)
        if not L:
            _record(s, "addContactToList", listId=listId,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"List ID {listId} does not exist")
        success: list[str] = []
        failure: list[str] = []
        targets: list = []
        if all:
            targets = [c.get("email")
                       for _, c in _contact_ids_as_int(s) if c.get("email")]
        else:
            targets = list(emails or []) + list(ids or [])
        lid_i = int(listId)
        for t in targets:
            c = _find_contact(s, t)
            if not c:
                failure.append(str(t))
                continue
            if lid_i in (c.get("listIds") or []):
                failure.append(str(t))
                continue
            c.setdefault("listIds", []).append(lid_i)
            c["listIds"] = sorted(set(c["listIds"]))
            c["modifiedAt"] = _now_iso()
            success.append(str(t))
        _recalc_list_totals(s, lid_i)
        _record(s, "addContactToList", listId=lid_i,
                success=success, failure=failure)
        _save_state(s)
        return {"contacts": {"success": success, "failure": failure}}


@mcp.tool(name="removeContactFromList")
def remove_contact_from_list(listId: int,
                             emails: list | None = None,
                             ids: list | None = None,
                             all: bool = False) -> dict:
    """Brevo REST: POST /contacts/lists/{listId}/contacts/remove —
    remove contacts (by email or id) from a list."""
    with _lock():
        s = _load_state()
        L = _find_list(s, listId)
        if not L:
            _record(s, "removeContactFromList", listId=listId,
                    result="not_found")
            _save_state(s)
            return _err("document_not_found",
                        f"List ID {listId} does not exist")
        success: list[str] = []
        failure: list[str] = []
        lid_i = int(listId)
        if all:
            targets = [c.get("email")
                       for _, c in _contact_ids_as_int(s)
                       if lid_i in (c.get("listIds") or [])
                       and c.get("email")]
        else:
            targets = list(emails or []) + list(ids or [])
        for t in targets:
            c = _find_contact(s, t)
            if not c:
                failure.append(str(t))
                continue
            if lid_i not in (c.get("listIds") or []):
                failure.append(str(t))
                continue
            c["listIds"] = [x for x in c["listIds"] if x != lid_i]
            c["modifiedAt"] = _now_iso()
            success.append(str(t))
        _recalc_list_totals(s, lid_i)
        _record(s, "removeContactFromList", listId=lid_i,
                success=success, failure=failure)
        _save_state(s)
        return {"contacts": {"success": success, "failure": failure}}


# ===========================================================================
# Senders
# ===========================================================================

@mcp.tool(name="getSenders")
def get_senders(ip: str | None = None,
                domain: str | None = None) -> dict:
    """Brevo REST: GET /senders — list verified sender identities."""
    with _lock():
        s = _load_state()
        items = list(s["senders"].values())
        if domain:
            items = [sd for sd in items
                     if (sd.get("email") or "").endswith("@" + domain)
                     or (sd.get("email") or "").endswith("." + domain)]
        if ip:
            items = [sd for sd in items
                     if ip in [i.get("ip") for i in sd.get("ips", [])]]
        _record(s, "getSenders", count=len(items))
        _save_state(s)
        return {"senders": items}


@mcp.tool(name="createSender")
def create_sender(name: str,
                  email: str,
                  ips: list | None = None) -> dict:
    """Brevo REST: POST /senders — register a new sender identity.
    Real API requires email verification; the mock marks it active
    immediately. Returns `{id}`."""
    with _lock():
        s = _load_state()
        if not name or not email:
            _record(s, "createSender", result="bad_request")
            _save_state(s)
            return _err("missing_parameter",
                        "'name' and 'email' are required")
        em = _norm_email(email)
        for sd in s["senders"].values():
            if _norm_email(sd.get("email")) == em:
                _record(s, "createSender", email=em, result="duplicate")
                _save_state(s)
                return _err("duplicate_parameter",
                            f"Sender already exists: {email}")
        sid = _next_id(s, "sender")
        sender = {
            "id": sid,
            "name": name,
            "email": email,
            "active": True,
            "ips": ips or [],
        }
        s["senders"][sid] = sender
        _record(s, "createSender", id=sid, email=em)
        _save_state(s)
        return {"id": sid}


# ===========================================================================
# SMS
# ===========================================================================

@mcp.tool(name="sendTransacSms")
def send_transac_sms(sender: str,
                     recipient: str,
                     content: str,
                     type: str = "transactional",
                     tag: str | None = None,
                     webUrl: str | None = None,
                     unicodeEnabled: bool = False,
                     organisationPrefix: str | None = None) -> dict:
    """Brevo REST: POST /transactionalSMS/sms — send a transactional
    SMS. Returns `{reference, messageId, smsCount, usedCredits,
    remainingCredits}`."""
    with _lock():
        s = _load_state()
        if not sender or not recipient or not content:
            _record(s, "sendTransacSms", result="bad_request")
            _save_state(s)
            return _err("missing_parameter",
                        "'sender', 'recipient', and 'content' are required")
        # 160 chars per SMS for GSM7; 70 for unicode.
        seg = 70 if unicodeEnabled else 160
        sms_count = max(1, -(-len(content) // seg))
        # int messageId for SMS in Brevo (different from email)
        mid = _next_id(s, "message_seq") + 10_000_000_000
        ref = f"sms-{mid}"
        rec = {
            "messageId": mid,
            "reference": ref,
            "sender": sender,
            "recipient": recipient,
            "content": content,
            "type": type,
            "tag": tag,
            "webUrl": webUrl,
            "unicodeEnabled": bool(unicodeEnabled),
            "smsCount": sms_count,
            "date": _now_iso(),
            "status": "sent",
        }
        s["sms_messages"].append(rec)
        plan = s.get("account", {}).get("plan", [{}])[0]
        used = plan.get("credits", 300)
        used_credits = sms_count * 0.045
        plan["credits"] = max(0, used - used_credits)
        _record(s, "sendTransacSms", recipient=recipient,
                messageId=mid, smsCount=sms_count)
        _save_state(s)
        return {
            "reference": ref,
            "messageId": mid,
            "smsCount": sms_count,
            "usedCredits": used_credits,
            "remainingCredits": plan["credits"],
        }


@mcp.tool(name="getTransacSmsActivity")
def get_transac_sms_activity(startDate: str | None = None,
                             endDate: str | None = None,
                             days: int | None = None,
                             phoneNumber: str | None = None,
                             event: str | None = None,
                             tags: str | None = None,
                             limit: int = 50,
                             offset: int = 0,
                             sort: str = "desc") -> dict:
    """Brevo REST: GET /transactionalSMS/statistics/events — list
    transactional SMS events."""
    with _lock():
        s = _load_state()
        items = list(s.get("sms_messages", []))
        if phoneNumber:
            items = [m for m in items
                     if m.get("recipient") == phoneNumber]
        if event:
            items = [m for m in items if m.get("status") == event]
        if tags:
            wanted = {t.strip() for t in tags.split(",")}
            items = [m for m in items if m.get("tag") in wanted]
        if startDate:
            items = [m for m in items
                     if (m.get("date") or "") >= startDate]
        if endDate:
            items = [m for m in items
                     if (m.get("date") or "") <= endDate]
        if days:
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=int(days))).isoformat()
            items = [m for m in items
                     if (m.get("date") or "") >= cutoff]
        items.sort(key=lambda m: m.get("date") or "",
                   reverse=(sort != "asc"))
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 5000))
        page = items[offset: offset + limit]
        events = [{
            "phoneNumber": m.get("recipient"),
            "date": m.get("date"),
            "messageId": m.get("messageId"),
            "event": m.get("status"),
            "reason": "",
            "reply": "",
            "tag": m.get("tag") or "",
        } for m in page]
        _record(s, "getTransacSmsActivity", count=len(page))
        _save_state(s)
        return {"events": events}


@mcp.tool(name="getSmsCampaigns")
def get_sms_campaigns(status: str | None = None,
                      startDate: str | None = None,
                      endDate: str | None = None,
                      limit: int = 500,
                      offset: int = 0,
                      sort: str = "desc") -> dict:
    """Brevo REST: GET /smsCampaigns — list SMS campaigns."""
    with _lock():
        s = _load_state()
        items = list(s["sms_campaigns"].values())
        if status:
            items = [c for c in items if c.get("status") == status]
        if startDate:
            items = [c for c in items
                     if (c.get("scheduledAt") or "") >= startDate]
        if endDate:
            items = [c for c in items
                     if (c.get("scheduledAt") or "") <= endDate]
        items.sort(key=lambda c: c.get("createdAt") or "",
                   reverse=(sort != "asc"))
        total = len(items)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 500), 1000))
        page = items[offset: offset + limit]
        _record(s, "getSmsCampaigns", count=len(page))
        _save_state(s)
        return {"campaigns": page, "count": total}


# ===========================================================================
# Mock-only helpers
# ===========================================================================

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state for verifier
    introspection. Not part of the real Brevo REST surface."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(account: dict | None = None,
                    contacts: list | None = None,
                    lists: list | None = None,
                    folders: list | None = None,
                    templates: list | None = None,
                    senders: list | None = None,
                    transac_emails: list | None = None,
                    blocked_contacts: list | None = None,
                    smtp_events: list | None = None,
                    sms_messages: list | None = None,
                    sms_campaigns: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed state. Each input is a list of Brevo-shaped
    dicts. If `replace=True`, state is reset before seeding.

    - `contacts`: [{id?, email, attributes?, listIds?, emailBlacklisted?,
                    smsBlacklisted?}]
    - `lists`: [{id?, name, folderId?}]
    - `folders`: [{id?, name}]
    - `templates`: [{id?, name, subject, sender, htmlContent?,
                     isActive?, tag?}]
    - `senders`: [{id?, name, email, active?}]
    - `transac_emails`: [{messageId?, to, subject, ...}]
    - `blocked_contacts`: [{email, reason, senderEmail?, blockedAt?}]
    - `smtp_events`: [{date, email, event, messageId?, tag?, templateId?}]
    - `sms_messages`: [{messageId?, recipient, content, status?, tag?}]
    - `sms_campaigns`: [{id?, name, status?, content?, scheduledAt?}]
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        if account:
            s["account"].update(account)
        for f in folders or []:
            fid = int(f.get("id") or _next_id(s, "folder"))
            s["folders"][fid] = {
                "id": fid,
                "name": f.get("name", f"Folder {fid}"),
                "totalSubscribers": f.get("totalSubscribers", 0),
                "totalBlacklisted": f.get("totalBlacklisted", 0),
                "uniqueSubscribers": f.get("uniqueSubscribers", 0),
            }
        for L in lists or []:
            lid = int(L.get("id") or _next_id(s, "list"))
            s["lists"][lid] = {
                "id": lid,
                "name": L.get("name", f"List {lid}"),
                "folderId": int(L.get("folderId", 1)),
                "totalSubscribers": int(L.get("totalSubscribers", 0)),
                "totalBlacklisted": int(L.get("totalBlacklisted", 0)),
                "uniqueSubscribers": int(L.get("uniqueSubscribers", 0)),
                "createdAt": L.get("createdAt") or _now_iso(),
                "campaignStats": L.get("campaignStats", []),
                "dynamicList": bool(L.get("dynamicList", False)),
            }
        for c in contacts or []:
            cid = int(c.get("id") or _next_id(s, "contact"))
            em = _norm_email(c.get("email"))
            now = _now_iso()
            s["contacts"][cid] = {
                "id": cid,
                "email": c.get("email", ""),
                "emailBlacklisted": bool(c.get("emailBlacklisted", False)),
                "smsBlacklisted": bool(c.get("smsBlacklisted", False)),
                "createdAt": c.get("createdAt") or now,
                "modifiedAt": c.get("modifiedAt") or now,
                "listIds": sorted({int(x) for x in c.get("listIds") or []}),
                "listUnsubscribed": c.get("listUnsubscribed") or [],
                "attributes": dict(c.get("attributes") or {}),
            }
            if em:
                s["contacts_by_email"][em] = cid
        for lid in list(s["lists"].keys()):
            _recalc_list_totals(s, int(lid))
        for t in templates or []:
            tid = int(t.get("id") or _next_id(s, "template"))
            now = _now_iso()
            s["templates"][tid] = {
                "id": tid,
                "name": t.get("name", f"Template {tid}"),
                "subject": t.get("subject", ""),
                "isActive": bool(t.get("isActive", True)),
                "testSent": bool(t.get("testSent", False)),
                "sender": t.get("sender", {}),
                "replyTo": t.get("replyTo", ""),
                "toField": t.get("toField", ""),
                "tag": t.get("tag", ""),
                "htmlContent": t.get("htmlContent", ""),
                "htmlUrl": t.get("htmlUrl"),
                "createdAt": t.get("createdAt") or now,
                "modifiedAt": t.get("modifiedAt") or now,
                "doiTemplate": bool(t.get("doiTemplate", False)),
            }
        for sd in senders or []:
            sid = int(sd.get("id") or _next_id(s, "sender"))
            s["senders"][sid] = {
                "id": sid,
                "name": sd.get("name", ""),
                "email": sd.get("email", ""),
                "active": bool(sd.get("active", True)),
                "ips": sd.get("ips", []),
            }
        for m in transac_emails or []:
            mid = m.get("messageId") or _new_message_id(s)
            rec = dict(m)
            rec["messageId"] = mid
            rec.setdefault("date", _now_iso())
            rec.setdefault("status", "sent")
            s["transac_emails"].append(rec)
        for b in blocked_contacts or []:
            s["blocked_contacts"].append({
                "email": b.get("email", ""),
                "reason": b.get("reason", {"code": "manual_blocked",
                                            "message": "Manually blocked"}),
                "senderEmail": b.get("senderEmail"),
                "blockedAt": b.get("blockedAt") or _now_iso(),
            })
        for e in smtp_events or []:
            ev = dict(e)
            ev.setdefault("date", _now_iso())
            s["smtp_events"].append(ev)
        for m in sms_messages or []:
            mid = m.get("messageId") or (
                _next_id(s, "message_seq") + 10_000_000_000)
            rec = dict(m)
            rec["messageId"] = mid
            rec.setdefault("date", _now_iso())
            rec.setdefault("status", "sent")
            s["sms_messages"].append(rec)
        for c in sms_campaigns or []:
            cid = int(c.get("id") or _next_id(s, "sms_campaign"))
            s["sms_campaigns"][cid] = {
                "id": cid,
                "name": c.get("name", f"SMS Campaign {cid}"),
                "status": c.get("status", "draft"),
                "content": c.get("content", ""),
                "scheduledAt": c.get("scheduledAt"),
                "sender": c.get("sender", ""),
                "createdAt": c.get("createdAt") or _now_iso(),
                "recipients": c.get("recipients", {}),
                "stats": c.get("stats", {}),
            }
        _record(s, "debug_seed",
                counts={"contacts": len(contacts or []),
                        "lists": len(lists or []),
                        "templates": len(templates or []),
                        "senders": len(senders or []),
                        "transac_emails": len(transac_emails or []),
                        "blocked_contacts": len(blocked_contacts or []),
                        "smtp_events": len(smtp_events or []),
                        "sms_messages": len(sms_messages or []),
                        "sms_campaigns": len(sms_campaigns or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "contact_ids": sorted(int(k) for k in s["contacts"].keys()
                                  if str(k).isdigit() or isinstance(k, int)),
            "list_ids": sorted(int(k) for k in s["lists"].keys()
                               if str(k).isdigit() or isinstance(k, int)),
            "template_ids": sorted(int(k) for k in s["templates"].keys()
                                   if str(k).isdigit() or isinstance(k, int)),
            "sender_ids": sorted(int(k) for k in s["senders"].keys()
                                 if str(k).isdigit() or isinstance(k, int)),
        }


if __name__ == "__main__":
    mcp.run()
