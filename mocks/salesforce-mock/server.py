"""Salesforce REST API mock MCP server.

Mirrors the Salesforce REST API v59.0
(https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/).
Tool names mirror the canonical REST endpoints — `query` for SOQL,
`search` for SOSL, `describe_object` / `describe_global` for metadata,
and `get_record` / `create_record` / `update_record` / `delete_record` /
`upsert_record` for the per-sObject CRUD surface.

Response envelopes match what the real REST API returns:

  Records carry an `attributes` block:
    {"attributes": {"type": "Account",
                    "url": "/services/data/v59.0/sobjects/Account/001..."},
     "Id": "001...", "Name": "...", ...}

  Query/queryAll response:
    {"totalSize": <int>, "done": <bool>, "records": [<record>, ...],
     "nextRecordsUrl"?: "/services/data/v59.0/query/<locator>"}

  Search response (parameterizedSearch shape):
    {"searchRecords": [<record with attributes>, ...]}

  Create/update/upsert response (sObject Row resource):
    {"id": "001...", "success": true, "errors": []}
    Upsert adds {"created": <bool>}.

  Error envelope (REST returns a JSON array of error objects):
    [{"message": "...", "errorCode": "NOT_FOUND"}]

State — one JSON file at $SF_MOCK_STATE_DIR/state.json:

  state = {
    "api_version": "v59.0",
    "objects": {
      "<ObjectType>": {
        "<recordId>": {"attributes": {...},
                       "Id": "...", "Name": "...", ...,
                       "CreatedDate": "...+0000", ...}
      }
    },
    "next_id_seq": {"case_number": 1000, ...},
    "users": {"<userId>": {...}},
    "admin_user_id": "<userId>",
    "calls": [...]
  }

Tool surface (12 REST-shaped tools):

  query, query_all, search,
  describe_object, describe_global,
  get_record, create_record, update_record, delete_record,
  upsert_record, list_record_attachments, get_limits

Plus mock-only helpers:
  mock_debug_state, mock_debug_seed_account, mock_debug_seed_contact,
  mock_debug_seed_lead, mock_debug_seed_opportunity, mock_debug_seed_case.

Salesforce-isms mocked:
  - 15/18-char object-prefixed Ids (Account=001, Contact=003, Lead=00Q,
    Opportunity=006, Case=500, User=005, Task=00T) with a stub `AAA`
    18-char checksum suffix (real SF SDKs accept both 15 and 18 char
    forms).
  - System fields: CreatedDate / LastModifiedDate / SystemModstamp in
    ISO 8601 with `+0000` suffix (the format SOQL returns, not `Z`).
    CreatedById / LastModifiedById / OwnerId default to the seeded
    admin User Id.
  - Case.CaseNumber is auto-assigned, monotonically increasing, zero-
    padded to 8 chars starting at 00001000.
  - Contact.Name is the computed `FirstName + " " + LastName` (Salesforce
    treats Name as a read-only compound field on Person-like sObjects).
  - SOQL parser supports SELECT/FROM/WHERE/ORDER BY/LIMIT/OFFSET, the
    comparison + logical + LIKE/IN operators, COUNT/MAX/MIN/AVG/SUM
    aggregates, GROUP BY, parent-traversal (`Account.Name`) and child
    subqueries (`(SELECT Id FROM Contacts)`), and date literals
    (TODAY, YESTERDAY, LAST_N_DAYS:N, THIS_MONTH, LAST_MONTH).
  - SOSL parser supports `FIND {term} [IN <scope>] RETURNING
    Object1(fields), Object2(fields)`.

Deliberately unsupported (out of scope for the mock):
  - Composite/Tree API, Bulk API, SObject Collections.
  - Apex REST, Tooling API, Metadata API.
  - HAVING, FOR UPDATE/FOR VIEW/FOR REFERENCE, TYPEOF polymorphism,
    GROUPING, ROLLUP / CUBE, geolocation (DISTANCE), date functions
    (CALENDAR_YEAR, FISCAL_QUARTER, …), WITH SECURITY_ENFORCED, USING
    SCOPE, ALL ROWS.
  - SOSL `WITH` clauses, snippet/highlighting, division.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_VERSION = "v59.0"

# Per-object Id prefix. Real Salesforce reserves these specific 3-char
# prefixes for each standard sObject — SDKs key off the prefix to
# determine the object type. Mock keeps the same mapping.
ID_PREFIXES: dict[str, str] = {
    "Account":     "001",
    "Contact":     "003",
    "Lead":        "00Q",
    "Opportunity": "006",
    "Case":        "500",
    "User":        "005",
    "Task":        "00T",
}

# Reverse lookup for `attributes.url` parsing + debug helpers.
PREFIX_TO_TYPE: dict[str, str] = {v: k for k, v in ID_PREFIXES.items()}

# Per-sObject canonical field list — used by describe_object and to
# initialise unset fields on create_record so query results return the
# full envelope the real API would.
OBJECT_FIELDS: dict[str, list[str]] = {
    "Account": [
        "Id", "Name", "Type", "Industry", "Phone", "Website",
        "BillingStreet", "BillingCity", "BillingState",
        "BillingPostalCode", "BillingCountry",
        "AnnualRevenue", "NumberOfEmployees",
        "OwnerId", "CreatedById", "LastModifiedById",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
    "Contact": [
        "Id", "FirstName", "LastName", "Name",
        "Email", "Phone", "MobilePhone", "Title",
        "AccountId", "OwnerId", "CreatedById", "LastModifiedById",
        "MailingStreet", "MailingCity", "MailingCountry",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
    "Lead": [
        "Id", "FirstName", "LastName", "Name", "Company",
        "Email", "Phone", "Status", "Industry", "Title",
        "LeadSource", "IsConverted",
        "ConvertedAccountId", "ConvertedContactId",
        "OwnerId", "CreatedById", "LastModifiedById",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
    "Opportunity": [
        "Id", "Name", "AccountId", "StageName", "Amount",
        "CloseDate", "Probability", "ForecastCategory",
        "OwnerId", "Type", "LeadSource", "Description",
        "CreatedById", "LastModifiedById",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
    "Case": [
        "Id", "CaseNumber", "Subject", "Description",
        "Status", "Priority", "Origin",
        "AccountId", "ContactId", "OwnerId",
        "CreatedById", "LastModifiedById",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
    "User": [
        "Id", "Username", "Name", "FirstName", "LastName",
        "Email", "IsActive", "ProfileId",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
    "Task": [
        "Id", "Subject", "Status", "Priority", "ActivityDate",
        "WhoId", "WhatId", "OwnerId", "Description",
        "CreatedById", "LastModifiedById",
        "CreatedDate", "LastModifiedDate", "SystemModstamp",
    ],
}

# Parent-relationship fields: relationship-name → (foreign-key field on
# this sObject, parent sObject type). Used by SOQL parent-traversal
# (`SELECT Account.Name FROM Contact`).
PARENT_RELATIONSHIPS: dict[str, dict[str, tuple[str, str]]] = {
    "Contact":     {"Account": ("AccountId", "Account"),
                    "Owner":   ("OwnerId",   "User")},
    "Opportunity": {"Account": ("AccountId", "Account"),
                    "Owner":   ("OwnerId",   "User")},
    "Case":        {"Account": ("AccountId", "Account"),
                    "Contact": ("ContactId", "Contact"),
                    "Owner":   ("OwnerId",   "User")},
    "Task":        {"Owner":   ("OwnerId",   "User")},
    "Lead":        {"Owner":   ("OwnerId",   "User")},
    "Account":     {"Owner":   ("OwnerId",   "User")},
}

# Child-relationship fields: relationship-plural on parent → (child
# sObject, foreign-key on child). Used by SOQL child subqueries
# (`SELECT (SELECT Id FROM Contacts) FROM Account`).
CHILD_RELATIONSHIPS: dict[str, dict[str, tuple[str, str]]] = {
    "Account": {
        "Contacts":      ("Contact",     "AccountId"),
        "Opportunities": ("Opportunity", "AccountId"),
        "Cases":         ("Case",        "AccountId"),
    },
    "Contact": {
        "Cases": ("Case", "ContactId"),
    },
}

# Numeric fields — used by aggregates and comparisons to coerce
# values out of the always-string property store on the wire.
NUMERIC_FIELDS: set[tuple[str, str]] = {
    ("Account",     "AnnualRevenue"),
    ("Account",     "NumberOfEmployees"),
    ("Opportunity", "Amount"),
    ("Opportunity", "Probability"),
}


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "SF_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/salesforce_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    """ISO 8601 with the `+0000` suffix Salesforce SOQL responses use
    (not the `Z` suffix). Millisecond precision."""
    return (datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000+0000"))


def _gen_admin_user_id() -> str:
    return _gen_id("User")


def _empty_state() -> dict:
    admin_id = _gen_admin_user_id()
    now = _now()
    admin = {
        "attributes": {
            "type": "User",
            "url": f"/services/data/{API_VERSION}/sobjects/User/{admin_id}",
        },
        "Id": admin_id,
        "Username": "admin@mock.salesforce.com",
        "Name": "Mock Admin",
        "FirstName": "Mock",
        "LastName": "Admin",
        "Email": "admin@mock.salesforce.com",
        "IsActive": True,
        "ProfileId": "00e000000000000AAA",
        "CreatedDate": now,
        "LastModifiedDate": now,
        "SystemModstamp": now,
    }
    return {
        "api_version": API_VERSION,
        "objects": {
            "Account":     {},
            "Contact":     {},
            "Lead":        {},
            "Opportunity": {},
            "Case":        {},
            "Task":        {},
            "User":        {admin_id: admin},
        },
        "next_id_seq": {"case_number": 1000},
        "admin_user_id": admin_id,
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("SF_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


# ---------------------------------------------------------------------------
# Salesforce-shaped helpers (Ids, envelopes, errors)
# ---------------------------------------------------------------------------

# Salesforce alphabet for the random Id body: base62 (0-9A-Za-z).
_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _gen_id(sobject_type: str) -> str:
    """Generate a Salesforce-shaped 18-char Id: 3-char object prefix +
    12 random base62 chars + 3-char checksum. We mock the checksum as
    a fixed `AAA` — real Salesforce computes it from the first 15 chars,
    but SDKs and the REST API accept both 15-char and 18-char Ids for
    every input.
    """
    prefix = ID_PREFIXES.get(sobject_type)
    if not prefix:
        # Unknown sObject — use a generic 'a0' custom-object prefix so
        # callers still get a 3-char-prefixed Id back.
        prefix = "a00"
    body = "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))
    return f"{prefix}{body}AAA"


def _id_to_type(record_id: str) -> str | None:
    if not record_id or len(record_id) < 3:
        return None
    return PREFIX_TO_TYPE.get(record_id[:3])


def _attrs(sobject_type: str, record_id: str) -> dict:
    return {
        "type": sobject_type,
        "url": f"/services/data/{API_VERSION}/sobjects/{sobject_type}/{record_id}",
    }


def _err(error_code: str, message: str, fields: list | None = None) -> list:
    """Salesforce REST error envelope is a JSON array of
    {message, errorCode, [fields]}."""
    body: dict = {"message": message, "errorCode": error_code}
    if fields:
        body["fields"] = list(fields)
    return [body]


def _strip_attrs(record: dict) -> dict:
    """Strip `attributes` so we can re-assemble it deterministically per
    response (the url's api_version may differ from when the record was
    stored)."""
    return {k: v for k, v in record.items() if k != "attributes"}


def _coerce(sobject_type: str, fields: dict) -> dict:
    """Apply per-sObject value normalisation. Salesforce stores most
    things in a structured form (numbers as numbers, booleans as
    booleans) — coerce known numeric / boolean fields here. Unknown
    fields pass through untouched."""
    out: dict = {}
    for k, v in (fields or {}).items():
        if v is None:
            out[k] = None
            continue
        if (sobject_type, k) in NUMERIC_FIELDS:
            try:
                out[k] = float(v) if "." in str(v) else int(float(v))
            except (TypeError, ValueError):
                out[k] = v
        elif k in ("IsActive", "IsConverted"):
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, str):
                out[k] = v.strip().lower() in ("true", "1", "yes")
            else:
                out[k] = bool(v)
        else:
            out[k] = v
    return out


def _compute_name(sobject_type: str, record: dict) -> None:
    """Salesforce auto-computes Name for Person-like sObjects from
    FirstName + LastName. Mirror that for Contact and Lead. Account /
    Opportunity / Case have a directly-settable Name field already."""
    if sobject_type in ("Contact", "Lead"):
        first = (record.get("FirstName") or "").strip()
        last = (record.get("LastName") or "").strip()
        record["Name"] = f"{first} {last}".strip() if (first or last) else None


def _next_case_number(state: dict) -> str:
    seq = state["next_id_seq"]
    n = seq.get("case_number", 1000)
    seq["case_number"] = n + 1
    # CaseNumber is zero-padded to 8 digits (`00001000`, `00001001`, …).
    return f"{n:08d}"


def _new_record(state: dict, sobject_type: str, fields: dict,
                rid: str | None = None) -> dict:
    rid = rid or _gen_id(sobject_type)
    now = _now()
    admin = state.get("admin_user_id") or ""
    rec: dict = {
        "attributes": _attrs(sobject_type, rid),
        "Id": rid,
    }
    rec.update(_coerce(sobject_type, fields))
    # System fields
    rec.setdefault("OwnerId", admin)
    rec.setdefault("CreatedById", admin)
    rec.setdefault("LastModifiedById", admin)
    rec["CreatedDate"] = rec.get("CreatedDate") or now
    rec["LastModifiedDate"] = now
    rec["SystemModstamp"] = now
    if sobject_type == "Case":
        rec.setdefault("CaseNumber", _next_case_number(state))
        rec.setdefault("Status", "New")
        rec.setdefault("Origin", "Web")
    if sobject_type == "Lead":
        rec.setdefault("Status", "Open - Not Contacted")
        rec.setdefault("IsConverted", False)
    if sobject_type == "Opportunity":
        rec.setdefault("Probability", 10)
        rec.setdefault("ForecastCategory", "Pipeline")
    _compute_name(sobject_type, rec)
    return rec


def _touch(record: dict, state: dict) -> None:
    record["LastModifiedDate"] = _now()
    record["SystemModstamp"] = record["LastModifiedDate"]
    record["LastModifiedById"] = state.get("admin_user_id") or ""


# ===========================================================================
# SOQL parser + executor
# ---------------------------------------------------------------------------
# This is a small purpose-built parser for the subset of SOQL the mock
# supports. It is NOT a general SQL parser. The grammar handled:
#
#   query        := SELECT field-list FROM ObjectName
#                   [WHERE expr] [GROUP BY field-list]
#                   [ORDER BY order-list] [LIMIT N] [OFFSET N]
#   field-list   := field-item ("," field-item)*
#   field-item   := alias.field
#                 | function "(" [field|"*"] ")" [alias]
#                 | "(" subquery ")"
#                 | field
#   expr         := and-expr ("OR" and-expr)*
#   and-expr     := not-expr ("AND" not-expr)*
#   not-expr     := ["NOT"] primary
#   primary      := "(" expr ")" | comparison
#   comparison   := field op value
#   op           := = | != | <> | < | <= | > | >= | LIKE | IN | NOT IN
#
# The lexer is a regex tokeniser; the parser is recursive-descent.
# ===========================================================================

# Tokens
_T_KW = "KW"
_T_ID = "ID"          # bare identifier or dotted (Account.Name)
_T_NUM = "NUM"
_T_STR = "STR"
_T_OP = "OP"          # =, !=, <>, <, <=, >, >=
_T_LP = "LP"
_T_RP = "RP"
_T_COMMA = "COMMA"
_T_STAR = "STAR"
_T_COLON = "COLON"

# Keywords (case-insensitive)
_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "ORDER", "BY", "ASC", "DESC",
    "LIMIT", "OFFSET", "GROUP", "AND", "OR", "NOT", "LIKE",
    "IN", "NULL", "TRUE", "FALSE",
    # Date literals (we treat them as keyword identifiers and resolve
    # in _value_to_python)
    "TODAY", "YESTERDAY", "TOMORROW", "THIS_MONTH", "LAST_MONTH",
    "THIS_WEEK", "LAST_WEEK", "THIS_YEAR", "LAST_YEAR",
}

# Aggregate functions
_AGG_FUNCS = {"COUNT", "MIN", "MAX", "AVG", "SUM"}


def _tokenize_soql(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
            continue
        if c == ",":
            tokens.append((_T_COMMA, ","))
            i += 1
            continue
        if c == "(":
            tokens.append((_T_LP, "("))
            i += 1
            continue
        if c == ")":
            tokens.append((_T_RP, ")"))
            i += 1
            continue
        if c == ":":
            tokens.append((_T_COLON, ":"))
            i += 1
            continue
        if c == "*":
            tokens.append((_T_STAR, "*"))
            i += 1
            continue
        if c in "<>!=":
            # 2-char op (<=, >=, !=, <>)
            if i + 1 < n and query[i+1] == "=":
                tokens.append((_T_OP, query[i:i+2]))
                i += 2
                continue
            if c == "<" and i + 1 < n and query[i+1] == ">":
                tokens.append((_T_OP, "<>"))
                i += 2
                continue
            if c == "=" or c == "<" or c == ">":
                tokens.append((_T_OP, c))
                i += 1
                continue
            raise ValueError(f"unexpected character {c!r} in SOQL")
        if c in ("'", '"'):
            quote = c
            j = i + 1
            buf: list[str] = []
            while j < n and query[j] != quote:
                if query[j] == "\\" and j + 1 < n:
                    buf.append(query[j+1])
                    j += 2
                else:
                    buf.append(query[j])
                    j += 1
            if j >= n:
                raise ValueError("unterminated string literal in SOQL")
            tokens.append((_T_STR, "".join(buf)))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and query[i+1].isdigit()):
            j = i + 1
            while j < n and (query[j].isdigit() or query[j] == "."):
                j += 1
            tokens.append((_T_NUM, query[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (query[j].isalnum() or query[j] in "._"):
                j += 1
            word = query[i:j]
            up = word.upper()
            if up in _KEYWORDS:
                tokens.append((_T_KW, up))
            else:
                tokens.append((_T_ID, word))
            i = j
            continue
        raise ValueError(f"unexpected character {c!r} in SOQL at position {i}")
    return tokens


class _SOQL:
    """Recursive-descent SOQL parser. Produces a dict-shaped AST that
    the executor walks."""

    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self, off: int = 0) -> tuple[str, str] | None:
        p = self.pos + off
        return self.tokens[p] if p < len(self.tokens) else None

    def _eat(self, kind: str, value: str | None = None) -> tuple[str, str]:
        tok = self._peek()
        if not tok:
            raise ValueError(f"unexpected EOF in SOQL (expected {kind})")
        if tok[0] != kind or (value is not None and tok[1].upper() != value.upper()):
            raise ValueError(f"expected {kind}/{value} in SOQL, got {tok}")
        self.pos += 1
        return tok

    def _accept(self, kind: str, value: str | None = None) -> tuple[str, str] | None:
        tok = self._peek()
        if not tok:
            return None
        if tok[0] != kind:
            return None
        if value is not None and tok[1].upper() != value.upper():
            return None
        self.pos += 1
        return tok

    def parse(self) -> dict:
        self._eat(_T_KW, "SELECT")
        fields = self._parse_field_list()
        self._eat(_T_KW, "FROM")
        # FROM target can be an id (Object) or sometimes Object.Relationship;
        # we only need the leading Object name here.
        obj_tok = self._eat(_T_ID)
        ast: dict = {
            "type": "select",
            "fields": fields,
            "object": obj_tok[1],
            "where": None,
            "group_by": [],
            "order_by": [],
            "limit": None,
            "offset": None,
        }
        if self._accept(_T_KW, "WHERE"):
            ast["where"] = self._parse_or()
        if self._accept(_T_KW, "GROUP"):
            self._eat(_T_KW, "BY")
            ast["group_by"] = self._parse_id_list()
        if self._accept(_T_KW, "ORDER"):
            self._eat(_T_KW, "BY")
            ast["order_by"] = self._parse_order_list()
        if self._accept(_T_KW, "LIMIT"):
            tok = self._eat(_T_NUM)
            ast["limit"] = int(tok[1])
        if self._accept(_T_KW, "OFFSET"):
            tok = self._eat(_T_NUM)
            ast["offset"] = int(tok[1])
        return ast

    def _parse_field_list(self) -> list[dict]:
        items = [self._parse_field_item()]
        while self._accept(_T_COMMA):
            items.append(self._parse_field_item())
        return items

    def _parse_field_item(self) -> dict:
        tok = self._peek()
        if tok and tok[0] == _T_LP:
            # child subquery: ( SELECT ... FROM Children )
            self._eat(_T_LP)
            inner = _SOQL(self._take_until_matching_rp()).parse()
            return {"kind": "subquery", "subquery": inner}
        if tok and tok[0] == _T_ID:
            # function call OR plain field
            name = tok[1]
            if (self._peek(1) and self._peek(1)[0] == _T_LP
                    and name.upper() in _AGG_FUNCS):
                self.pos += 1
                self._eat(_T_LP)
                arg_tok = self._peek()
                arg: str | None
                if arg_tok and arg_tok[0] == _T_STAR:
                    self._eat(_T_STAR)
                    arg = None
                elif arg_tok and arg_tok[0] == _T_RP:
                    arg = None
                else:
                    arg = self._eat(_T_ID)[1]
                self._eat(_T_RP)
                alias = None
                if self._peek() and self._peek()[0] == _T_ID:
                    alias = self._eat(_T_ID)[1]
                return {"kind": "agg", "func": name.upper(),
                        "arg": arg, "alias": alias}
            # plain field (possibly dotted: Account.Name)
            self._eat(_T_ID)
            alias = None
            nxt = self._peek()
            if nxt and nxt[0] == _T_ID:
                # `FIELD alias` shorthand — Salesforce only allows this on
                # aggregates, but be permissive.
                alias = self._eat(_T_ID)[1]
            return {"kind": "field", "name": name, "alias": alias}
        raise ValueError(f"unexpected token in field list: {tok}")

    def _take_until_matching_rp(self) -> list[tuple[str, str]]:
        depth = 1
        out: list[tuple[str, str]] = []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            if tok[0] == _T_LP:
                depth += 1
            elif tok[0] == _T_RP:
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    return out
            out.append(tok)
            self.pos += 1
        raise ValueError("unmatched paren in SOQL subquery")

    def _parse_id_list(self) -> list[str]:
        ids = [self._eat(_T_ID)[1]]
        while self._accept(_T_COMMA):
            ids.append(self._eat(_T_ID)[1])
        return ids

    def _parse_order_list(self) -> list[dict]:
        items: list[dict] = []
        while True:
            f = self._eat(_T_ID)[1]
            direction = "ASC"
            if self._accept(_T_KW, "DESC"):
                direction = "DESC"
            elif self._accept(_T_KW, "ASC"):
                direction = "ASC"
            # ignore NULLS FIRST/LAST modifier (out of scope)
            items.append({"field": f, "direction": direction})
            if not self._accept(_T_COMMA):
                break
        return items

    def _parse_or(self) -> dict:
        left = self._parse_and()
        while self._accept(_T_KW, "OR"):
            right = self._parse_and()
            left = {"kind": "or", "left": left, "right": right}
        return left

    def _parse_and(self) -> dict:
        left = self._parse_not()
        while self._accept(_T_KW, "AND"):
            right = self._parse_not()
            left = {"kind": "and", "left": left, "right": right}
        return left

    def _parse_not(self) -> dict:
        if self._accept(_T_KW, "NOT"):
            return {"kind": "not", "expr": self._parse_primary()}
        return self._parse_primary()

    def _parse_primary(self) -> dict:
        if self._accept(_T_LP):
            inner = self._parse_or()
            self._eat(_T_RP)
            return inner
        return self._parse_comparison()

    def _parse_comparison(self) -> dict:
        f_tok = self._eat(_T_ID)
        field = f_tok[1]
        # IN / NOT IN
        if self._accept(_T_KW, "IN"):
            self._eat(_T_LP)
            values = self._parse_value_list()
            self._eat(_T_RP)
            return {"kind": "cmp", "op": "IN", "field": field, "value": values}
        if self._accept(_T_KW, "NOT"):
            self._eat(_T_KW, "IN")
            self._eat(_T_LP)
            values = self._parse_value_list()
            self._eat(_T_RP)
            return {"kind": "cmp", "op": "NOT_IN",
                    "field": field, "value": values}
        if self._accept(_T_KW, "LIKE"):
            tok = self._eat(_T_STR)
            return {"kind": "cmp", "op": "LIKE",
                    "field": field, "value": tok[1]}
        # comparison op
        op_tok = self._eat(_T_OP)
        value = self._parse_value()
        op = op_tok[1]
        if op == "<>":
            op = "!="
        return {"kind": "cmp", "op": op, "field": field, "value": value}

    def _parse_value(self) -> Any:
        tok = self._peek()
        if not tok:
            raise ValueError("expected value")
        if tok[0] == _T_STR:
            self.pos += 1
            return {"kind": "str", "value": tok[1]}
        if tok[0] == _T_NUM:
            self.pos += 1
            v = tok[1]
            return {"kind": "num",
                    "value": float(v) if "." in v else int(v)}
        if tok[0] == _T_KW and tok[1] in ("TRUE", "FALSE"):
            self.pos += 1
            return {"kind": "bool", "value": tok[1] == "TRUE"}
        if tok[0] == _T_KW and tok[1] == "NULL":
            self.pos += 1
            return {"kind": "null", "value": None}
        if tok[0] == _T_KW:
            # date literal: TODAY, YESTERDAY, LAST_N_DAYS:N, etc.
            self.pos += 1
            name = tok[1]
            if name == "LAST_N_DAYS" or self._peek_is_colon():
                # We don't see LAST_N_DAYS here because it's an identifier;
                # this branch handles bare-keyword date literals only.
                pass
            return {"kind": "date_literal", "value": name}
        if tok[0] == _T_ID:
            self.pos += 1
            # LAST_N_DAYS:N pattern arrives as ID 'LAST_N_DAYS' + ':' + NUM
            if (self._peek() and self._peek()[0] == _T_COLON
                    and tok[1].upper() in (
                        "LAST_N_DAYS", "NEXT_N_DAYS",
                        "LAST_N_WEEKS", "NEXT_N_WEEKS",
                        "LAST_N_MONTHS", "NEXT_N_MONTHS")):
                self._eat(_T_COLON)
                num = self._eat(_T_NUM)
                return {"kind": "date_literal",
                        "value": f"{tok[1].upper()}:{num[1]}"}
            return {"kind": "id", "value": tok[1]}
        raise ValueError(f"unexpected token in value: {tok}")

    def _peek_is_colon(self) -> bool:
        nxt = self._peek()
        return bool(nxt and nxt[0] == _T_COLON)

    def _parse_value_list(self) -> list:
        items = [self._parse_value()]
        while self._accept(_T_COMMA):
            items.append(self._parse_value())
        return items


def _value_to_python(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    kind = v.get("kind")
    if kind in ("str", "num", "bool", "null", "id"):
        return v["value"]
    if kind == "date_literal":
        return _resolve_date_literal(v["value"])
    return v.get("value")


def _resolve_date_literal(name: str) -> tuple[str, str]:
    """Resolve a SOQL date literal to a (start, end) ISO-date range. We
    compare against the date part of CreatedDate / LastModifiedDate /
    SystemModstamp as a substring."""
    today = datetime.date.today()
    if name.startswith("LAST_N_DAYS:"):
        n = int(name.split(":", 1)[1])
        start = today - datetime.timedelta(days=n)
        return (start.isoformat(), today.isoformat())
    if name.startswith("NEXT_N_DAYS:"):
        n = int(name.split(":", 1)[1])
        end = today + datetime.timedelta(days=n)
        return (today.isoformat(), end.isoformat())
    if name == "TODAY":
        return (today.isoformat(), today.isoformat())
    if name == "YESTERDAY":
        y = today - datetime.timedelta(days=1)
        return (y.isoformat(), y.isoformat())
    if name == "TOMORROW":
        t = today + datetime.timedelta(days=1)
        return (t.isoformat(), t.isoformat())
    if name == "THIS_MONTH":
        start = today.replace(day=1)
        # Approximate end-of-month
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1) - datetime.timedelta(days=1)
        else:
            end = start.replace(month=start.month + 1) - datetime.timedelta(days=1)
        return (start.isoformat(), end.isoformat())
    if name == "LAST_MONTH":
        first_this = today.replace(day=1)
        last_prev = first_this - datetime.timedelta(days=1)
        start = last_prev.replace(day=1)
        return (start.isoformat(), last_prev.isoformat())
    if name == "THIS_WEEK":
        start = today - datetime.timedelta(days=today.weekday())
        end = start + datetime.timedelta(days=6)
        return (start.isoformat(), end.isoformat())
    if name == "LAST_WEEK":
        start_this = today - datetime.timedelta(days=today.weekday())
        end = start_this - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=6)
        return (start.isoformat(), end.isoformat())
    if name == "THIS_YEAR":
        return (datetime.date(today.year, 1, 1).isoformat(),
                datetime.date(today.year, 12, 31).isoformat())
    if name == "LAST_YEAR":
        return (datetime.date(today.year - 1, 1, 1).isoformat(),
                datetime.date(today.year - 1, 12, 31).isoformat())
    # Unrecognised date literal — fall back to a sentinel.
    return ("", "")


def _like_to_re(pattern: str) -> re.Pattern:
    """Convert a SOQL LIKE pattern to a regex. SOQL only supports `%`
    (any-chars) and `_` (single char) wildcards. Case-insensitive."""
    out: list[str] = []
    for c in pattern:
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        else:
            out.append(re.escape(c))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def _field_value(state: dict, record: dict, sobject_type: str,
                 field: str) -> Any:
    """Resolve a possibly-dotted field reference (`Account.Name`) on a
    record. Parent traversal looks up the foreign-key record."""
    if "." not in field:
        return record.get(field)
    head, _, rest = field.partition(".")
    rels = PARENT_RELATIONSHIPS.get(sobject_type, {})
    rel = rels.get(head)
    if not rel:
        return None
    fk_field, parent_type = rel
    parent_id = record.get(fk_field)
    if not parent_id:
        return None
    parent = state["objects"].get(parent_type, {}).get(parent_id)
    if not parent:
        return None
    return _field_value(state, parent, parent_type, rest)


def _cmp(left: Any, op: str, right: Any) -> bool:
    if op == "LIKE":
        if left is None:
            return False
        return bool(_like_to_re(str(right)).match(str(left)))
    if op == "IN":
        rights = {_normalise(_value_to_python(r)) for r in right}
        return _normalise(left) in rights
    if op == "NOT_IN":
        rights = {_normalise(_value_to_python(r)) for r in right}
        return _normalise(left) not in rights
    rhs = _value_to_python(right)
    if op == "=":
        if rhs is None:
            return left in (None, "")
        if isinstance(rhs, tuple) and len(rhs) == 2:
            # date_literal range — `=` means "within"
            return _within_date_range(left, rhs)
        return _normalise(left) == _normalise(rhs)
    if op == "!=":
        if rhs is None:
            return left not in (None, "")
        if isinstance(rhs, tuple) and len(rhs) == 2:
            return not _within_date_range(left, rhs)
        return _normalise(left) != _normalise(rhs)
    # ordered comparisons
    try:
        ln = float(left)
        rn = float(rhs) if not isinstance(rhs, tuple) else float(rhs[0])
    except (TypeError, ValueError):
        ls = "" if left is None else str(left)
        if isinstance(rhs, tuple):
            rs = rhs[0]
        elif rhs is None:
            rs = ""
        else:
            rs = str(rhs)
        if op == "<":
            return ls < rs
        if op == "<=":
            return ls <= rs
        if op == ">":
            return ls > rs
        if op == ">=":
            return ls >= rs
        return False
    if op == "<":
        return ln < rn
    if op == "<=":
        return ln <= rn
    if op == ">":
        return ln > rn
    if op == ">=":
        return ln >= rn
    return False


def _normalise(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    return str(v)


def _within_date_range(left: Any, rng: tuple[str, str]) -> bool:
    if left is None:
        return False
    s = str(left)[:10]
    return rng[0] <= s <= rng[1]


def _eval_where(state: dict, record: dict, sobject_type: str,
                node: dict | None) -> bool:
    if node is None:
        return True
    kind = node["kind"]
    if kind == "and":
        return (_eval_where(state, record, sobject_type, node["left"])
                and _eval_where(state, record, sobject_type, node["right"]))
    if kind == "or":
        return (_eval_where(state, record, sobject_type, node["left"])
                or _eval_where(state, record, sobject_type, node["right"]))
    if kind == "not":
        return not _eval_where(state, record, sobject_type, node["expr"])
    if kind == "cmp":
        left = _field_value(state, record, sobject_type, node["field"])
        return _cmp(left, node["op"], node["value"])
    return False


def _select_record_fields(state: dict, record: dict, sobject_type: str,
                          fields: list[dict]) -> dict:
    """Build the response record for a single source record per the
    SELECT field list. Handles parent traversal (Account.Name nests
    under an `Account` sub-envelope with its own `attributes`) and
    child subqueries."""
    out: dict = {"attributes": dict(_attrs(sobject_type, record["Id"]))}
    for item in fields:
        kind = item["kind"]
        if kind == "field":
            name = item["name"]
            if "." in name:
                _materialise_parent_path(state, record, sobject_type, name, out)
            else:
                out[name] = record.get(name)
        elif kind == "agg":
            # Aggregates are handled in the outer executor (group-level).
            pass
        elif kind == "subquery":
            inner = item["subquery"]
            child_obj = inner["object"]
            rels = CHILD_RELATIONSHIPS.get(sobject_type, {})
            if child_obj not in rels:
                out[child_obj] = None
                continue
            actual_child, fk_field = rels[child_obj]
            children = [c for c in state["objects"].get(actual_child, {}).values()
                        if c.get(fk_field) == record["Id"]]
            children = _apply_order_limit(inner, children)
            child_records = [_select_record_fields(state, c, actual_child,
                                                    inner["fields"])
                             for c in children]
            out[child_obj] = {
                "totalSize": len(child_records),
                "done": True,
                "records": child_records,
            }
    return out


def _materialise_parent_path(state: dict, record: dict, sobject_type: str,
                              path: str, out: dict) -> None:
    """`Account.Owner.Name` → nested {"Account": {"attributes":...,
    "Owner": {"attributes":..., "Name":"..."}}} on `out`."""
    head, _, rest = path.partition(".")
    rels = PARENT_RELATIONSHIPS.get(sobject_type, {})
    rel = rels.get(head)
    if not rel:
        return
    fk_field, parent_type = rel
    parent_id = record.get(fk_field)
    parent = (state["objects"].get(parent_type, {}).get(parent_id)
              if parent_id else None)
    if not parent:
        out[head] = None
        return
    block = out.setdefault(head, {"attributes": dict(_attrs(parent_type,
                                                             parent["Id"]))})
    if "." in rest:
        _materialise_parent_path(state, parent, parent_type, rest, block)
    else:
        block[rest] = parent.get(rest)


def _apply_order_limit(ast: dict, records: list[dict]) -> list[dict]:
    """Apply ORDER BY / LIMIT / OFFSET to a record list (used by both
    top-level and subquery executors)."""
    order_by = ast.get("order_by") or []
    if order_by:
        for spec in reversed(order_by):
            field = spec["field"]
            reverse = spec["direction"] == "DESC"
            records = sorted(records,
                              key=lambda r: _sort_key(r.get(field)),
                              reverse=reverse)
    offset = ast.get("offset") or 0
    limit = ast.get("limit")
    if offset:
        records = records[offset:]
    if limit is not None:
        records = records[:limit]
    return records


def _sort_key(v: Any) -> tuple:
    """Return a sort key tuple that puts None first and orders mixed
    numeric/string values stably."""
    if v is None:
        return (0, "")
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (1, v)
    return (2, str(v))


def _execute_soql(state: dict, ast: dict, include_deleted: bool) -> dict:
    sobject_type = ast["object"]
    bin_ = state["objects"].get(sobject_type, {})
    records = [r for r in bin_.values()
                if include_deleted or not r.get("IsDeleted")]
    # WHERE
    where = ast.get("where")
    records = [r for r in records
                if _eval_where(state, r, sobject_type, where)]

    fields = ast["fields"]
    has_agg = any(f["kind"] == "agg" for f in fields)
    group_by = ast.get("group_by") or []

    if has_agg or group_by:
        return _execute_aggregate(state, sobject_type, records, fields,
                                   group_by, ast)

    # Non-aggregate path
    records = _apply_order_limit(ast, records)
    result_records = [_select_record_fields(state, r, sobject_type, fields)
                       for r in records]
    return {
        "totalSize": len(result_records),
        "done": True,
        "records": result_records,
    }


def _execute_aggregate(state: dict, sobject_type: str,
                        records: list[dict],
                        fields: list[dict], group_by: list[str],
                        ast: dict) -> dict:
    """Aggregate executor. Salesforce returns AggregateResult records
    with `expr0`, `expr1`, … aliases for unaliased aggregates."""
    # Group rows
    if group_by:
        groups: dict[tuple, list[dict]] = {}
        for r in records:
            key = tuple(_field_value(state, r, sobject_type, g)
                          for g in group_by)
            groups.setdefault(key, []).append(r)
        group_keys = list(groups.keys())
    else:
        groups = {(): records}
        group_keys = [()]

    out_records: list[dict] = []
    auto_alias = 0
    for key in group_keys:
        rows = groups[key]
        rec: dict = {"attributes": {"type": "AggregateResult"}}
        # Replace group-by field values
        for i, g in enumerate(group_by):
            rec[g] = key[i]
        # Aggregates
        for f in fields:
            if f["kind"] == "field":
                # GROUP BY non-aggregate fields land in rec directly.
                continue
            if f["kind"] != "agg":
                continue
            alias = f["alias"] or f"expr{auto_alias}"
            if not f["alias"]:
                auto_alias += 1
            func = f["func"]
            arg = f["arg"]
            if func == "COUNT":
                if arg is None:
                    rec[alias] = len(rows)
                else:
                    rec[alias] = sum(1 for r in rows if r.get(arg) is not None)
            else:
                vals = []
                for r in rows:
                    v = r.get(arg) if arg else None
                    if v is None:
                        continue
                    try:
                        vals.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if not vals:
                    rec[alias] = None
                elif func == "SUM":
                    rec[alias] = sum(vals)
                elif func == "AVG":
                    rec[alias] = sum(vals) / len(vals)
                elif func == "MIN":
                    rec[alias] = min(vals)
                elif func == "MAX":
                    rec[alias] = max(vals)
        out_records.append(rec)

    out_records = _apply_order_limit(ast, out_records)
    return {
        "totalSize": len(out_records),
        "done": True,
        "records": out_records,
    }


# ===========================================================================
# SOSL parser + executor
# ---------------------------------------------------------------------------
# Grammar:
#   sosl       := FIND { search-term } [ IN search-scope ]
#                  RETURNING ret-obj ("," ret-obj)*
#   ret-obj    := ObjectName [ "(" field-list [WHERE expr] ")" ]
#
# search-scope is ignored (we always search all string fields).
# ===========================================================================

def _parse_sosl(query: str) -> dict:
    # Pull the FIND term.
    m = re.match(r"\s*FIND\s*[{\"']([^}\"']*)[}\"']", query, re.IGNORECASE)
    if not m:
        raise ValueError("SOSL query must start with FIND {term}")
    term = m.group(1)
    rest = query[m.end():]
    # Optional IN <scope>
    in_m = re.match(r"\s*IN\s+([A-Z_ ]+?)\s+RETURNING", rest, re.IGNORECASE)
    scope = "ALL FIELDS"
    if in_m:
        scope = in_m.group(1).strip().upper()
        rest = rest[in_m.end() - len("RETURNING"):]
    # RETURNING
    rm = re.match(r"\s*RETURNING\s+(.*)", rest, re.IGNORECASE | re.DOTALL)
    if not rm:
        # No RETURNING — search all standard sObjects with just Id
        return {"term": term, "scope": scope, "returning": [
            {"object": t, "fields": ["Id"], "where": None}
            for t in OBJECT_FIELDS.keys()
        ]}
    body = rm.group(1).strip()
    returning: list[dict] = []
    # Split on top-level commas (paren-aware)
    for chunk in _split_top_level(body, ","):
        chunk = chunk.strip()
        if not chunk:
            continue
        paren_m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?$",
                            chunk, re.DOTALL)
        if not paren_m:
            continue
        obj = paren_m.group(1)
        inside = (paren_m.group(2) or "").strip()
        fields = ["Id"]
        where = None
        if inside:
            # split fields portion vs WHERE clause
            up = inside.upper()
            wpos = up.find(" WHERE ")
            if wpos < 0 and up.startswith("WHERE "):
                wpos = 0
            if wpos >= 0:
                fields_part = inside[:wpos].strip()
                where_part = inside[wpos:].strip()
                if where_part.upper().startswith("WHERE"):
                    where_part = where_part[5:].strip()
                # Parse where via SOQL parser by injecting it into a
                # synthetic SELECT.
                where_ast = None
                try:
                    toks = _tokenize_soql(f"SELECT Id FROM {obj} WHERE {where_part}")
                    where_ast = _SOQL(toks).parse()["where"]
                except ValueError:
                    where_ast = None
                fields = [f.strip() for f in fields_part.split(",") if f.strip()]
                where = where_ast
            else:
                fields = [f.strip() for f in inside.split(",") if f.strip()]
        returning.append({"object": obj, "fields": fields, "where": where})
    return {"term": term, "scope": scope, "returning": returning}


def _split_top_level(s: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for c in s:
        if c == "(":
            depth += 1
            buf.append(c)
        elif c == ")":
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf))
    return parts


def _execute_sosl(state: dict, ast: dict) -> dict:
    term = (ast.get("term") or "").lower()
    out: list[dict] = []
    for ret in ast["returning"]:
        obj = ret["object"]
        bin_ = state["objects"].get(obj, {})
        for r in bin_.values():
            if r.get("IsDeleted"):
                continue
            if not _sosl_match(r, term):
                continue
            if ret.get("where") and not _eval_where(state, r, obj,
                                                     ret["where"]):
                continue
            view: dict = {"attributes": dict(_attrs(obj, r["Id"]))}
            for f in ret["fields"]:
                view[f] = r.get(f)
            out.append(view)
    return {"searchRecords": out}


def _sosl_match(record: dict, term: str) -> bool:
    if not term:
        return True
    for k, v in record.items():
        if k == "attributes":
            continue
        if v is None:
            continue
        if term in str(v).lower():
            return True
    return False


# ===========================================================================
# FastMCP server
# ===========================================================================

mcp = FastMCP("salesforce-mock")


# ---------------------------------------------------------------------------
# Query / search / describe
# ---------------------------------------------------------------------------

@mcp.tool(name="query")
def query(q: str) -> dict:
    """Salesforce REST: GET /services/data/v59.0/query?q=<SOQL>.

    Executes a SOQL query. Returns
    `{"totalSize", "done", "records": [<record>...]}`. Soft-deleted
    records (IsDeleted=true) are excluded; use `query_all` to include
    them. Each record carries an `attributes` envelope of
    `{type, url}` and the requested fields.

    Supported SOQL features: SELECT field projection, WHERE with
    =/!=/<>/</<=/>/>= /LIKE/IN/NOT IN/AND/OR/NOT, parent traversal
    (`Account.Name`), child subqueries (`(SELECT Id FROM Contacts)`),
    aggregates (COUNT/MIN/MAX/AVG/SUM), GROUP BY, ORDER BY [ASC|DESC],
    LIMIT, OFFSET, date literals (TODAY, YESTERDAY, LAST_N_DAYS:N,
    THIS_MONTH, LAST_MONTH, THIS_WEEK, LAST_WEEK, THIS_YEAR, LAST_YEAR).
    """
    with _lock():
        s = _load_state()
        try:
            ast = _SOQL(_tokenize_soql(q)).parse()
        except ValueError as exc:
            _record(s, "query", q=q, result="malformed", error=str(exc))
            _save_state(s)
            return _err("MALFORMED_QUERY", str(exc))
        if ast["object"] not in s["objects"]:
            _record(s, "query", q=q, result="unknown_sobject",
                    sobject=ast["object"])
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{ast['object']}' is not supported.")
        body = _execute_soql(s, ast, include_deleted=False)
        _record(s, "query", q=q, sobject=ast["object"],
                count=body["totalSize"])
        _save_state(s)
        return body


@mcp.tool(name="query_all")
def query_all(q: str) -> dict:
    """Salesforce REST: GET /services/data/v59.0/queryAll?q=<SOQL>.

    Identical to `query` but includes soft-deleted records
    (rows where `IsDeleted=true`). Use this for archived/deleted
    record audits. Same response shape as `query`."""
    with _lock():
        s = _load_state()
        try:
            ast = _SOQL(_tokenize_soql(q)).parse()
        except ValueError as exc:
            _record(s, "query_all", q=q, result="malformed", error=str(exc))
            _save_state(s)
            return _err("MALFORMED_QUERY", str(exc))
        if ast["object"] not in s["objects"]:
            _record(s, "query_all", q=q, result="unknown_sobject",
                    sobject=ast["object"])
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{ast['object']}' is not supported.")
        body = _execute_soql(s, ast, include_deleted=True)
        _record(s, "query_all", q=q, sobject=ast["object"],
                count=body["totalSize"])
        _save_state(s)
        return body


@mcp.tool(name="search")
def search(q: str) -> dict:
    """Salesforce REST: GET /services/data/v59.0/search?q=<SOSL>
    (also exposed via /parameterizedSearch).

    Executes a SOSL search. Returns `{"searchRecords": [<record>...]}`
    with each record carrying `attributes` of `{type, url}` and the
    requested fields. SOSL grammar:

      FIND {term} [IN <scope>]
        RETURNING Object1(field1, field2 [WHERE ...]),
                  Object2(field1)

    `<scope>` is currently parsed but ignored; the mock searches all
    fields on the named RETURNING objects.
    """
    with _lock():
        s = _load_state()
        try:
            ast = _parse_sosl(q)
        except ValueError as exc:
            _record(s, "search", q=q, result="malformed", error=str(exc))
            _save_state(s)
            return _err("MALFORMED_SEARCH", str(exc))
        body = _execute_sosl(s, ast)
        _record(s, "search", q=q,
                returning=[r["object"] for r in ast["returning"]],
                count=len(body["searchRecords"]))
        _save_state(s)
        return body


@mcp.tool(name="describe_object")
def describe_object(sObjectType: str) -> dict:
    """Salesforce REST: GET /services/data/v59.0/sobjects/{type}/describe.

    Returns the per-sObject metadata block: object name, label, key
    prefix, and a list of `fields` with their types. The mock returns
    a simplified subset of the real metadata (no picklist values, no
    layout info)."""
    with _lock():
        s = _load_state()
        if sObjectType not in OBJECT_FIELDS:
            _record(s, "describe_object", sobject=sObjectType,
                    result="unknown")
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{sObjectType}' is not supported.")
        fields = [{
            "name": f,
            "label": f,
            "type": _field_type(sObjectType, f),
            "updateable": f not in {"Id", "Name", "CreatedDate",
                                      "LastModifiedDate", "SystemModstamp",
                                      "CreatedById", "LastModifiedById",
                                      "CaseNumber", "IsDeleted"},
            "nillable": f not in {"Id", "LastName", "Email"},
            "custom": False,
        } for f in OBJECT_FIELDS[sObjectType]]
        body = {
            "name": sObjectType,
            "label": sObjectType,
            "labelPlural": f"{sObjectType}s",
            "keyPrefix": ID_PREFIXES.get(sObjectType, "a00"),
            "custom": False,
            "createable": True,
            "updateable": True,
            "deletable": True,
            "queryable": True,
            "fields": fields,
            "childRelationships": [
                {"childSObject": child, "field": fk,
                 "relationshipName": name}
                for name, (child, fk) in CHILD_RELATIONSHIPS.get(
                    sObjectType, {}).items()
            ],
            "urls": {
                "sobject":
                    f"/services/data/{API_VERSION}/sobjects/{sObjectType}",
                "describe":
                    f"/services/data/{API_VERSION}/sobjects/{sObjectType}/describe",
                "rowTemplate":
                    f"/services/data/{API_VERSION}/sobjects/{sObjectType}/{{ID}}",
            },
        }
        _record(s, "describe_object", sobject=sObjectType)
        _save_state(s)
        return body


def _field_type(sobject_type: str, field: str) -> str:
    """Heuristic field-type classification for describe_object."""
    if field == "Id" or field.endswith("Id"):
        return "id" if field == "Id" else "reference"
    if (sobject_type, field) in NUMERIC_FIELDS:
        return "double" if field in ("AnnualRevenue", "Amount") else "int"
    if field in ("IsActive", "IsConverted", "IsDeleted"):
        return "boolean"
    if field in ("CloseDate", "ActivityDate"):
        return "date"
    if field in ("CreatedDate", "LastModifiedDate", "SystemModstamp"):
        return "datetime"
    if field == "Email":
        return "email"
    if field == "Phone" or field == "MobilePhone":
        return "phone"
    if field == "Website":
        return "url"
    if field == "Description":
        return "textarea"
    return "string"


@mcp.tool(name="describe_global")
def describe_global() -> dict:
    """Salesforce REST: GET /services/data/v59.0/sobjects/.

    Returns the list of sObjects in the org. Mock returns one entry per
    known standard object with key metadata (keyPrefix, queryable,
    createable, …)."""
    with _lock():
        s = _load_state()
        sobjects = [{
            "name": name,
            "label": name,
            "labelPlural": f"{name}s",
            "keyPrefix": ID_PREFIXES.get(name, "a00"),
            "custom": False,
            "createable": True,
            "updateable": True,
            "deletable": True,
            "queryable": True,
            "urls": {
                "sobject":
                    f"/services/data/{API_VERSION}/sobjects/{name}",
                "describe":
                    f"/services/data/{API_VERSION}/sobjects/{name}/describe",
                "rowTemplate":
                    f"/services/data/{API_VERSION}/sobjects/{name}/{{ID}}",
            },
        } for name in OBJECT_FIELDS.keys()]
        _record(s, "describe_global", count=len(sobjects))
        _save_state(s)
        return {
            "encoding": "UTF-8",
            "maxBatchSize": 200,
            "sobjects": sobjects,
        }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@mcp.tool(name="get_record")
def get_record(sObjectType: str, recordId: str,
               fields: str | None = None) -> dict:
    """Salesforce REST: GET /services/data/v59.0/sobjects/{type}/{id}
    [?fields=...].

    Retrieve a single record by Id. `fields` is an optional
    comma-separated list — when omitted, all canonical fields for the
    sObject type are returned."""
    with _lock():
        s = _load_state()
        if sObjectType not in s["objects"]:
            _record(s, "get_record", sobject=sObjectType,
                    result="unknown_sobject")
            _save_state(s)
            return _err("NOT_FOUND",
                         f"sObject type '{sObjectType}' is not supported.")
        rec = s["objects"][sObjectType].get(recordId)
        if not rec:
            _record(s, "get_record", sobject=sObjectType, id=recordId,
                    result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                         f"Provided external ID field does not exist or is "
                         f"not accessible: {recordId}")
        view = dict(rec)
        view["attributes"] = dict(_attrs(sObjectType, rec["Id"]))
        if fields:
            wanted = {f.strip() for f in fields.split(",") if f.strip()}
            view = {k: view[k] for k in ("attributes", *wanted) if k in view}
            view["attributes"] = dict(_attrs(sObjectType, rec["Id"]))
        _record(s, "get_record", sobject=sObjectType, id=recordId,
                result="ok")
        _save_state(s)
        return view


@mcp.tool(name="create_record")
def create_record(sObjectType: str, record: dict) -> dict:
    """Salesforce REST: POST /services/data/v59.0/sobjects/{type}.

    Creates a new record. Returns the standard REST create response:
      `{"id": "<recordId>", "success": true, "errors": []}`.

    Validation errors (unknown sObject, missing required field like
    LastName on Contact / Lead) return the Salesforce error-array
    envelope. CaseNumber is auto-generated for Case records and
    cannot be supplied by the caller."""
    with _lock():
        s = _load_state()
        if sObjectType not in s["objects"]:
            _record(s, "create_record", sobject=sObjectType,
                    result="unknown_sobject")
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{sObjectType}' is not supported.")
        # Drop caller-supplied CaseNumber — Case auto-numbers it.
        props = dict(record or {})
        if sObjectType == "Case":
            props.pop("CaseNumber", None)
        # Required-field check
        missing = _required_field_check(sObjectType, props)
        if missing:
            _record(s, "create_record", sobject=sObjectType,
                    result="required_field_missing", missing=missing)
            _save_state(s)
            return _err("REQUIRED_FIELD_MISSING",
                         f"Required fields are missing: {missing}",
                         fields=missing)
        new = _new_record(s, sObjectType, props)
        s["objects"][sObjectType][new["Id"]] = new
        _record(s, "create_record", sobject=sObjectType, id=new["Id"])
        _save_state(s)
        return {"id": new["Id"], "success": True, "errors": []}


def _required_field_check(sobject_type: str, props: dict) -> list[str]:
    """Per-sObject required-field set. Salesforce returns
    REQUIRED_FIELD_MISSING for these on create."""
    required: dict[str, list[str]] = {
        "Contact":     ["LastName"],
        "Lead":        ["LastName", "Company"],
        "Account":     ["Name"],
        "Opportunity": ["Name", "StageName", "CloseDate"],
        "Case":        [],
        "User":        ["Username", "LastName", "Email"],
        "Task":        [],
    }
    needed = required.get(sobject_type, [])
    return [f for f in needed if not props.get(f)]


@mcp.tool(name="update_record")
def update_record(sObjectType: str, recordId: str,
                  record: dict) -> dict:
    """Salesforce REST: PATCH /services/data/v59.0/sobjects/{type}/{id}.

    Updates the supplied fields on an existing record. The real API
    returns 204 No Content on success; the mock returns
    `{"id", "success": true, "errors": []}` so callers can still
    consume a structured body."""
    with _lock():
        s = _load_state()
        if sObjectType not in s["objects"]:
            _record(s, "update_record", sobject=sObjectType,
                    result="unknown_sobject")
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{sObjectType}' is not supported.")
        rec = s["objects"][sObjectType].get(recordId)
        if not rec:
            _record(s, "update_record", sobject=sObjectType, id=recordId,
                    result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                         f"Provided external ID field does not exist or is "
                         f"not accessible: {recordId}")
        # Strip uneditable system fields from the patch.
        props = {k: v for k, v in (record or {}).items()
                  if k not in {"Id", "CreatedDate", "CreatedById",
                                "CaseNumber", "attributes"}}
        rec.update(_coerce(sObjectType, props))
        _compute_name(sObjectType, rec)
        _touch(rec, s)
        _record(s, "update_record", sobject=sObjectType, id=recordId,
                keys=list(props.keys()))
        _save_state(s)
        return {"id": recordId, "success": True, "errors": []}


@mcp.tool(name="delete_record")
def delete_record(sObjectType: str, recordId: str) -> dict:
    """Salesforce REST: DELETE /services/data/v59.0/sobjects/{type}/{id}.

    Soft-deletes the record (sets `IsDeleted=true` so it disappears
    from `query` but stays visible to `query_all`). The real API
    returns 204 No Content; the mock returns
    `{"id", "success": true, "errors": []}`."""
    with _lock():
        s = _load_state()
        if sObjectType not in s["objects"]:
            _record(s, "delete_record", sobject=sObjectType,
                    result="unknown_sobject")
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{sObjectType}' is not supported.")
        rec = s["objects"][sObjectType].get(recordId)
        if not rec:
            _record(s, "delete_record", sobject=sObjectType, id=recordId,
                    result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                         f"Provided external ID field does not exist or is "
                         f"not accessible: {recordId}")
        rec["IsDeleted"] = True
        _touch(rec, s)
        _record(s, "delete_record", sobject=sObjectType, id=recordId)
        _save_state(s)
        return {"id": recordId, "success": True, "errors": []}


@mcp.tool(name="upsert_record")
def upsert_record(sObjectType: str, externalIdField: str,
                   externalId: str, record: dict) -> dict:
    """Salesforce REST: PATCH /services/data/v59.0/sobjects/{type}/
    {externalIdField}/{externalId}.

    Upsert via external Id: if a record exists with
    `record[externalIdField] == externalId`, update it; otherwise
    create a new record with that external id baked in.

    Returns `{"id", "created", "success": true, "errors": []}`."""
    with _lock():
        s = _load_state()
        if sObjectType not in s["objects"]:
            _record(s, "upsert_record", sobject=sObjectType,
                    result="unknown_sobject")
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{sObjectType}' is not supported.")
        # Look up an existing record by external id
        bin_ = s["objects"][sObjectType]
        existing = next(
            (r for r in bin_.values()
             if str(r.get(externalIdField, "")).lower() == str(externalId).lower()),
            None,
        )
        if existing:
            props = {k: v for k, v in (record or {}).items()
                      if k not in {"Id", "CreatedDate", "CreatedById",
                                    "CaseNumber", "attributes"}}
            existing.update(_coerce(sObjectType, props))
            _compute_name(sObjectType, existing)
            _touch(existing, s)
            _record(s, "upsert_record", sobject=sObjectType,
                    id=existing["Id"], created=False)
            _save_state(s)
            return {"id": existing["Id"], "created": False,
                    "success": True, "errors": []}
        props = dict(record or {})
        props[externalIdField] = externalId
        if sObjectType == "Case":
            props.pop("CaseNumber", None)
        missing = _required_field_check(sObjectType, props)
        if missing:
            _record(s, "upsert_record", sobject=sObjectType,
                    result="required_field_missing", missing=missing)
            _save_state(s)
            return _err("REQUIRED_FIELD_MISSING",
                         f"Required fields are missing: {missing}",
                         fields=missing)
        new = _new_record(s, sObjectType, props)
        bin_[new["Id"]] = new
        _record(s, "upsert_record", sobject=sObjectType, id=new["Id"],
                created=True)
        _save_state(s)
        return {"id": new["Id"], "created": True,
                "success": True, "errors": []}


@mcp.tool(name="list_record_attachments")
def list_record_attachments(sObjectType: str, recordId: str,
                              relationship: str) -> dict:
    """Salesforce REST: GET /services/data/v59.0/sobjects/{type}/{id}/
    {relationship} — generic related-list fetch.

    `relationship` is a child relationship plural name from
    `describe_object().childRelationships[].relationshipName`
    (e.g. `Contacts` on Account, `Cases` on Account). Returns
    `{"totalSize", "done", "records": [<record>...]}` for the related
    child records. The name `list_record_attachments` follows the
    spec: it's a generic related-list endpoint, despite the name."""
    with _lock():
        s = _load_state()
        if sObjectType not in s["objects"]:
            _record(s, "list_record_attachments", sobject=sObjectType,
                    result="unknown_sobject")
            _save_state(s)
            return _err("INVALID_TYPE",
                         f"sObject type '{sObjectType}' is not supported.")
        rec = s["objects"][sObjectType].get(recordId)
        if not rec:
            _record(s, "list_record_attachments", sobject=sObjectType,
                    id=recordId, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                         f"resource not found: {recordId}")
        rels = CHILD_RELATIONSHIPS.get(sObjectType, {})
        if relationship not in rels:
            _record(s, "list_record_attachments", sobject=sObjectType,
                    relationship=relationship,
                    result="unknown_relationship")
            _save_state(s)
            return _err("INVALID_FIELD",
                         f"unknown relationship '{relationship}' on "
                         f"{sObjectType}")
        child_type, fk = rels[relationship]
        children = [c for c in s["objects"].get(child_type, {}).values()
                     if c.get(fk) == recordId and not c.get("IsDeleted")]
        out = []
        for c in children:
            view = dict(c)
            view["attributes"] = dict(_attrs(child_type, c["Id"]))
            out.append(view)
        _record(s, "list_record_attachments", sobject=sObjectType,
                id=recordId, relationship=relationship, count=len(out))
        _save_state(s)
        return {"totalSize": len(out), "done": True, "records": out}


@mcp.tool(name="get_limits")
def get_limits() -> dict:
    """Salesforce REST: GET /services/data/v59.0/limits.

    Returns org-level API limit usage. Mock returns fixed remaining
    counts large enough to never trip a verifier check."""
    with _lock():
        s = _load_state()
        _record(s, "get_limits")
        _save_state(s)
        return {
            "DailyApiRequests":          {"Max": 100000, "Remaining": 99500},
            "DailyBulkApiBatches":       {"Max": 15000,  "Remaining": 15000},
            "DailyAsyncApexExecutions":  {"Max": 250000, "Remaining": 250000},
            "DataStorageMB":             {"Max": 1024,   "Remaining": 1020},
            "FileStorageMB":             {"Max": 1024,   "Remaining": 1024},
            "ConcurrentAsyncGetReportInstances":
                                          {"Max": 200,    "Remaining": 200},
        }


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state."""
    with _lock():
        return _load_state()


def _seed(sobject_type: str, fields: dict,
          record_id: str | None = None) -> dict:
    """Shared body for the per-object debug seeders."""
    with _lock():
        s = _load_state()
        if record_id:
            new = _new_record(s, sobject_type, fields, rid=record_id)
        else:
            new = _new_record(s, sobject_type, fields)
        s["objects"][sobject_type][new["Id"]] = new
        _record(s, f"debug_seed_{sobject_type.lower()}",
                sobject=sobject_type, id=new["Id"])
        _save_state(s)
        return new


@mcp.tool(name="mock_debug_seed_account")
def mock_debug_seed_account(Name: str,
                              Industry: str | None = None,
                              Type: str | None = None,
                              Phone: str | None = None,
                              Website: str | None = None,
                              AnnualRevenue: float | None = None,
                              NumberOfEmployees: int | None = None,
                              BillingCity: str | None = None,
                              BillingCountry: str | None = None,
                              Id: str | None = None) -> dict:
    """Mock-only: seed an Account record with common fields."""
    fields = {k: v for k, v in {
        "Name": Name, "Industry": Industry, "Type": Type,
        "Phone": Phone, "Website": Website,
        "AnnualRevenue": AnnualRevenue,
        "NumberOfEmployees": NumberOfEmployees,
        "BillingCity": BillingCity, "BillingCountry": BillingCountry,
    }.items() if v is not None}
    return _seed("Account", fields, record_id=Id)


@mcp.tool(name="mock_debug_seed_contact")
def mock_debug_seed_contact(LastName: str,
                              FirstName: str | None = None,
                              Email: str | None = None,
                              Phone: str | None = None,
                              Title: str | None = None,
                              AccountId: str | None = None,
                              Id: str | None = None) -> dict:
    """Mock-only: seed a Contact record."""
    fields = {k: v for k, v in {
        "LastName": LastName, "FirstName": FirstName,
        "Email": Email, "Phone": Phone, "Title": Title,
        "AccountId": AccountId,
    }.items() if v is not None}
    return _seed("Contact", fields, record_id=Id)


@mcp.tool(name="mock_debug_seed_lead")
def mock_debug_seed_lead(LastName: str, Company: str,
                           FirstName: str | None = None,
                           Email: str | None = None,
                           Status: str | None = None,
                           Industry: str | None = None,
                           Title: str | None = None,
                           LeadSource: str | None = None,
                           Id: str | None = None) -> dict:
    """Mock-only: seed a Lead record."""
    fields = {k: v for k, v in {
        "LastName": LastName, "Company": Company,
        "FirstName": FirstName, "Email": Email,
        "Status": Status, "Industry": Industry,
        "Title": Title, "LeadSource": LeadSource,
    }.items() if v is not None}
    return _seed("Lead", fields, record_id=Id)


@mcp.tool(name="mock_debug_seed_opportunity")
def mock_debug_seed_opportunity(Name: str, StageName: str, CloseDate: str,
                                  AccountId: str | None = None,
                                  Amount: float | None = None,
                                  Probability: float | None = None,
                                  Type: str | None = None,
                                  LeadSource: str | None = None,
                                  Description: str | None = None,
                                  Id: str | None = None) -> dict:
    """Mock-only: seed an Opportunity record."""
    fields = {k: v for k, v in {
        "Name": Name, "StageName": StageName, "CloseDate": CloseDate,
        "AccountId": AccountId, "Amount": Amount,
        "Probability": Probability, "Type": Type,
        "LeadSource": LeadSource, "Description": Description,
    }.items() if v is not None}
    return _seed("Opportunity", fields, record_id=Id)


@mcp.tool(name="mock_debug_seed_case")
def mock_debug_seed_case(Subject: str,
                           Status: str | None = None,
                           Priority: str | None = None,
                           Origin: str | None = None,
                           AccountId: str | None = None,
                           ContactId: str | None = None,
                           Description: str | None = None,
                           Id: str | None = None) -> dict:
    """Mock-only: seed a Case record (CaseNumber is auto-assigned)."""
    fields = {k: v for k, v in {
        "Subject": Subject, "Status": Status, "Priority": Priority,
        "Origin": Origin, "AccountId": AccountId,
        "ContactId": ContactId, "Description": Description,
    }.items() if v is not None}
    return _seed("Case", fields, record_id=Id)


if __name__ == "__main__":
    mcp.run()
