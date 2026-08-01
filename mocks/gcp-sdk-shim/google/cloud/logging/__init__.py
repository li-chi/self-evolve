"""`google.cloud.logging` shim backed by the google-cloud-mock state.

Entries are the exact dicts the MCP mock's `logging_write_log` appends to
state.json["logs"][<log_name>], so an entry written by the agent through
its MCP tool is visible to a grader calling `client.list_entries(...)`.

`list_entries` supports the filter subset Toolathlon graders use:
  logName="projects/<p>/logs/<name>"      (also logName = "..." / bare name)
  timestamp >= "<rfc3339>"  /  <=  /  >  /  <
  severity  = "INFO" (and >=)
  resource.type="global"
  AND-joined terms; a leading `NOT <term>` is treated as an exclusion.
Anything it cannot parse is ignored rather than silently dropping entries —
the graders then apply their own belt-and-braces filtering in Python.
"""

from __future__ import annotations

import datetime
import re

from .. import _mockstate as ms

__all__ = ["Client", "Logger", "ASCENDING", "DESCENDING", "entries"]

ASCENDING = "timestamp asc"
DESCENDING = "timestamp desc"

_SEVERITY_ORDER = {
    "DEFAULT": 0, "DEBUG": 100, "INFO": 200, "NOTICE": 300,
    "WARNING": 400, "ERROR": 500, "CRITICAL": 600, "ALERT": 700,
    "EMERGENCY": 800,
}


class LogEntry:
    def __init__(self, entry: dict):
        self._entry = entry
        self.log_name = entry.get("log_name")
        self.severity = entry.get("severity", "DEFAULT")
        self.timestamp = ms.parse_ts(entry.get("timestamp"))
        self.resource = entry.get("resource")
        self.labels = entry.get("labels") or {}
        self.insert_id = entry.get("insert_id")
        if entry.get("json_payload") is not None:
            self.payload = entry["json_payload"]
        else:
            self.payload = entry.get("text_payload")

    @property
    def json_payload(self):
        return self._entry.get("json_payload")

    @property
    def text_payload(self):
        return self._entry.get("text_payload")

    def to_api_repr(self):
        return dict(self._entry)

    def __repr__(self):
        return f"<LogEntry {self.log_name} {self.severity} {self.payload!r}>"


class Logger:
    def __init__(self, name, client, labels=None):
        self.name = name
        self._client = client
        self.labels = labels or {}
        self.full_name = f"projects/{client.project}/logs/{name}"

    def _write(self, payload, severity, is_json, **kw):
        entry = {
            "log_name": self.full_name,
            "severity": (severity or "DEFAULT").upper(),
            "timestamp": ms.now(),
            "text_payload": None if is_json else payload,
            "json_payload": payload if is_json else None,
            "resource": kw.get("resource")
            or {"type": "global",
                "labels": {"project_id": self._client.project}},
        }
        if kw.get("labels"):
            entry["labels"] = kw["labels"]
        with ms.mutate() as s:
            s.setdefault("logs", {}).setdefault(self.name, []).append(entry)
            ms.record(s, "logging_write_log", log_name=self.name,
                      severity=entry["severity"])

    def log_text(self, text, severity="DEFAULT", **kw):
        self._write(text, severity, False, **kw)

    def log_struct(self, info, severity="DEFAULT", **kw):
        self._write(info, severity, True, **kw)

    def log(self, payload, severity="DEFAULT", **kw):
        self._write(payload, severity, not isinstance(payload, str), **kw)

    def list_entries(self, **kw):
        kw.setdefault("filter_", f'logName="{self.full_name}"')
        return self._client.list_entries(**kw)

    def delete(self):
        with ms.mutate() as s:
            s.get("logs", {}).pop(self.name, None)
            ms.record(s, "logging_delete_log", log_name=self.name)


# --------------------------------------------------------------------------
# filter handling
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'(?P<lparen>\()|(?P<rparen>\))|'
    r'(?P<op>\bAND\b|\bOR\b|\bNOT\b)|'
    r'(?P<cmp>(?P<field>[A-Za-z0-9_.\\/]+)\s*'
    r'(?P<operator>>=|<=|!=|=~|=|>|<|:)\s*'
    r"(?P<value>\"(?:[^\"\\\\]|\\\\.)*\"|'[^']*'|[^\\s()]+))"
    r'|(?P<bare>[A-Za-z0-9_.\\/]+)'      # bare field = existence check
    r')'
)


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _tokenize(expr: str) -> list:
    tokens, pos = [], 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            pos += 1  # skip anything we do not model rather than failing
            continue
        pos = m.end()
        if m.group("lparen"):
            tokens.append(("(", None))
        elif m.group("rparen"):
            tokens.append((")", None))
        elif m.group("op"):
            tokens.append((m.group("op").upper(), None))
        elif m.group("cmp") is None and m.group("bare"):
            # `NOT jsonPayload.foo` — a field with no operator asks whether
            # the field is present at all.
            tokens.append(("EXISTS",
                           {"field": m.group("bare").replace("\\", "")}))
        else:
            tokens.append(("CMP", {
                "field": m.group("field").replace("\\", ""),
                "op": m.group("operator"),
                "value": _unquote(m.group("value")),
            }))
    return tokens


class _Node:
    """Parsed filter node: AND/OR/NOT tree over comparisons."""

    def __init__(self, kind, value=None, children=()):
        self.kind = kind
        self.value = value
        self.children = list(children)


def _parse(tokens: list) -> _Node:
    """expr := term ((AND|OR|<implicit AND>) term)*   term := NOT? (cmp|group)"""
    pos = 0

    def parse_expr():
        nonlocal pos
        node = parse_term()
        while pos < len(tokens):
            kind = tokens[pos][0]
            if kind == ")":
                break
            if kind in ("AND", "OR"):
                op = kind
                pos += 1
            elif kind in ("CMP", "EXISTS", "(", "NOT"):
                op = "AND"  # juxtaposition means AND in the logging language
            else:
                pos += 1
                continue
            right = parse_term()
            if right is None:
                break
            if node is None:
                node = right
            elif node.kind == op:
                node.children.append(right)
            else:
                node = _Node(op, children=[node, right])
        return node

    def parse_term():
        nonlocal pos
        while pos < len(tokens) and tokens[pos][0] in ("AND", "OR"):
            pos += 1
        if pos >= len(tokens):
            return None
        kind, payload = tokens[pos]
        if kind == "NOT":
            pos += 1
            inner = parse_term()
            return _Node("NOT", children=[inner]) if inner else None
        if kind == "(":
            pos += 1
            inner = parse_expr()
            if pos < len(tokens) and tokens[pos][0] == ")":
                pos += 1
            return inner
        if kind in ("CMP", "EXISTS"):
            pos += 1
            return _Node(kind, value=payload)
        pos += 1
        return None

    return parse_expr()


def _parse_filter(expr: str):
    if not expr:
        return None
    return _parse(_tokenize(expr))


def _entry_field(entry, field: str):
    if field == "logName":
        return entry.log_name
    if field == "severity":
        return entry.severity
    if field == "timestamp":
        return entry.timestamp
    if field == "resource.type":
        return (entry.resource or {}).get("type")
    if field.startswith("resource.labels."):
        return (entry.resource or {}).get("labels", {}).get(
            field.split(".", 2)[2])
    if field.startswith("labels."):
        return entry.labels.get(field.split(".", 1)[1])
    if field.startswith("jsonPayload."):
        payload = entry.json_payload
        if not isinstance(payload, dict):
            return None
        cur = payload
        for part in field.split(".")[1:]:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur
    if field == "jsonPayload":
        return entry.json_payload
    if field == "textPayload":
        return entry.text_payload
    return _MISSING


_MISSING = object()


def _cmp(actual, op, expected) -> bool:
    if isinstance(actual, datetime.datetime):
        expected_dt = ms.parse_ts(expected)
        return {
            "=": actual == expected_dt, "!=": actual != expected_dt,
            ">": actual > expected_dt, "<": actual < expected_dt,
            ">=": actual >= expected_dt, "<=": actual <= expected_dt,
            ":": False,
        }[op]
    if op in (">", "<", ">=", "<=") and \
            str(actual).upper() in _SEVERITY_ORDER and \
            str(expected).upper() in _SEVERITY_ORDER:
        a = _SEVERITY_ORDER[str(actual).upper()]
        b = _SEVERITY_ORDER[str(expected).upper()]
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
    a, b = str(actual), str(expected)
    if op == ":":            # Cloud Logging's "has / contains" operator
        return b in a
    if op == "=~":
        return re.search(b, a) is not None
    if op == "=":
        # logName may be written short or fully qualified
        return a == b or a.endswith("/" + b)
    if op == "!=":
        return not (a == b or a.endswith("/" + b))
    return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def _eval(entry, node) -> bool:
    if node is None:
        return True
    if node.kind == "AND":
        return all(_eval(entry, c) for c in node.children)
    if node.kind == "OR":
        return any(_eval(entry, c) for c in node.children)
    if node.kind == "NOT":
        return not all(_eval(entry, c) for c in node.children)
    term = node.value
    actual = _entry_field(entry, term["field"])
    if node.kind == "EXISTS":
        return actual is not _MISSING and actual is not None
    if actual is _MISSING:
        return True  # unsupported field: never drop the entry
    if actual is None:
        return False
    try:
        return _cmp(actual, term["op"], term["value"])
    except (KeyError, TypeError, ValueError):
        return True


def _matches(entry, node) -> bool:
    return _eval(entry, node)


class Client:
    def __init__(self, project=None, credentials=None, **_kw):
        self.project = project or ms.PROJECT_ID
        self._credentials = credentials

    def logger(self, name, labels=None, **_kw):
        return Logger(name, self, labels=labels)

    def list_entries(self, resource_names=None, filter_=None, order_by=None,
                     page_size=None, max_results=None, projects=None, **_kw):
        s = ms.load_state()
        entries = [LogEntry(e) for log in s.get("logs", {}).values()
                   for e in log]
        tree = _parse_filter(filter_ or "")
        entries = [e for e in entries if _matches(e, tree)]
        entries.sort(key=lambda e: e.timestamp,
                     reverse=(order_by == DESCENDING))
        limit = max_results or page_size
        return entries[:limit] if limit else entries

    def setup_logging(self, **_kw):
        pass

    def close(self):
        pass


def entries(*args, **kwargs):  # pragma: no cover - parity placeholder
    raise NotImplementedError("google.cloud.logging.entries is not shimmed")
