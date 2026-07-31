"""MongoDB mock MCP server.

Mirrors the tool surface of `mongodb-mcp-server@0.2.0` (the upstream
server registered by mcp-atlas, source:
github.com/mongodb-js/mongodb-mcp-server). That server speaks the
real MongoDB Node driver to a real `mongod`; this mock keeps the
exact same MCP tool names + arg shapes but is backed by a tiny
in-process Mongo-like layer that persists each database/collection
as a JSON array on disk.

Atlas's normalization pass renames tools by replacing dashes with
underscores and prefixing with the server name, so e.g. upstream
`collection-schema` becomes `mongodb_collection-schema`. The names
registered here use the upstream form (`collection-schema`, `find`,
`aggregate`, ...) so the registry-side mapping doesn't change.

Backend semantics:
  - Filters support $eq $ne $gt $gte $lt $lte $in $nin $and $or $not
    $nor $regex $exists. Implicit equality (`{field: value}`) and
    nested-dot-path predicates (`{"a.b.c": ...}`) both work.
  - Aggregation supports $match $group $sort $limit $skip $project
    $count $unwind $addFields/$set. Anything else is best-effort or
    flagged in the call log.
  - Update supports $set $unset $inc $push $pull $addToSet $rename
    plus replacement docs (no operators -> whole-doc replace).
  - `_id` auto-generated as 24-hex ObjectId-like strings if missing.

Return shapes match the real MongoDB driver as exposed via
the upstream server's `provider.*` calls:
  find       -> {"content":[{"type":"text","text":"Found N docs..."},
                 {"type":"text","text":"<EJSON doc>"}, ...]}
  count      -> {"content":[{"type":"text","text":"Found N documents"}]}
  aggregate  -> same shape as find
  insertMany -> {"content":[..., "Inserted IDs: a, b, c"]}
  updateMany -> {"content":[..., "Matched/Modified/Upserted ..."]}
  deleteMany -> {"content":[..., "Deleted N document(s) ..."]}
  list-collections / list-databases / collection-* -> text lines

Errors surface as MCP CallToolResult-shaped error text, matching the
upstream server's behaviour of returning a single text block with
the error message instead of raising.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import fcntl
import json
import os
import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "MONGO_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/mongo_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _new_oid() -> str:
    """Return a 24-hex-char ObjectId-like string."""
    return secrets.token_hex(12)


def _empty_state() -> dict:
    return {
        "databases": {},  # name -> {"collections": {name: {documents, indexes}}}
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed_path = os.environ.get("MONGO_MOCK_SEED_PATH")
        if seed_path and os.path.exists(seed_path):
            return _load_seed(seed_path)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=_json_default)
    os.replace(tmp, path)


def _json_default(o: Any) -> Any:
    """JSON encoder fallback for datetime / bytes / sets."""
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", errors="replace")
    if isinstance(o, set):
        return list(o)
    return str(o)


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


def _get_coll(state: dict, database: str, collection: str,
              create: bool = False) -> dict | None:
    db = state["databases"].get(database)
    if db is None:
        if not create:
            return None
        db = {"collections": {}}
        state["databases"][database] = db
    coll = db["collections"].get(collection)
    if coll is None:
        if not create:
            return None
        coll = {"documents": [], "indexes": [_default_id_index()]}
        db["collections"][collection] = coll
    coll.setdefault("indexes", [_default_id_index()])
    coll.setdefault("documents", [])
    return coll


def _default_id_index() -> dict:
    return {"name": "_id_", "key": {"_id": 1}, "unique": True}


# ---------------------------------------------------------------------------
# Seed loader (BSON dump or JSON arrays)
# ---------------------------------------------------------------------------

def _load_seed(seed_path: str) -> dict:
    """Build initial state from a seed path. Accepts:

      - a JSON file matching our state schema (top-level "databases" key)
      - a JSON file mapping {db: {coll: [docs, ...]}}
      - a directory laid out like a `mongodump` output:
            <root>/<db>/<coll>.bson
            <root>/<db>/<coll>.json   (json array)
            <root>/<db>/<coll>.metadata.json   (ignored for content)
        URL-encoded collection names ("Purchase+History.bson" or
        "Purchase%20History.bson") are decoded.
    """
    state = _empty_state()
    if os.path.isfile(seed_path):
        with open(seed_path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        if isinstance(blob, dict) and "databases" in blob:
            state.update(blob)
            return state
        # nested map form
        for db_name, colls in (blob or {}).items():
            for coll_name, docs in (colls or {}).items():
                _ingest_docs(state, db_name, coll_name, docs)
        return state
    if not os.path.isdir(seed_path):
        return state
    # mongodump-style directory: <root>/<db>/<coll>.{bson,json}
    for db_name in sorted(os.listdir(seed_path)):
        if db_name.startswith(".") or db_name == "__MACOSX":
            continue
        db_dir = os.path.join(seed_path, db_name)
        if not os.path.isdir(db_dir):
            continue
        for fn in sorted(os.listdir(db_dir)):
            if fn.startswith(".") or fn.endswith(".metadata.json"):
                continue
            full = os.path.join(db_dir, fn)
            stem, ext = os.path.splitext(fn)
            coll_name = _decode_coll_name(stem)
            docs: list[dict] = []
            try:
                if ext.lower() == ".bson":
                    docs = _read_bson(full)
                elif ext.lower() == ".json":
                    with open(full, "r", encoding="utf-8") as f:
                        docs = json.load(f)
                    if isinstance(docs, dict):
                        docs = [docs]
                else:
                    continue
            except Exception as e:  # pragma: no cover - seed-time fallback
                state["calls"].append(
                    {"op": "seed_error", "ts": _now(),
                     "file": full, "error": str(e)})
                continue
            _ingest_docs(state, db_name, coll_name, docs)
    return state


def _decode_coll_name(stem: str) -> str:
    import urllib.parse as up
    # mongodump replaces spaces with '+' OR URL-encodes them
    return up.unquote(stem.replace("+", " "))


def _read_bson(path: str) -> list[dict]:
    """Decode a .bson file (sequence of BSON documents) to a list of
    JSON-safe dicts. Relies on pymongo's `bson` module."""
    try:
        import bson  # type: ignore
        from bson.json_util import loads as _json_util_loads
        from bson.json_util import dumps as _json_util_dumps
    except ImportError:  # pragma: no cover
        raise RuntimeError(
            "pymongo (bson module) is required to read .bson seed files. "
            "Install with `pip install pymongo`.")
    with open(path, "rb") as f:
        raw = f.read()
    docs = list(bson.decode_all(raw))
    # round-trip through bson.json_util so ObjectId, datetime, Decimal128
    # etc. become JSON-serialisable Extended-JSON dicts -- then strip the
    # $oid wrappers down to plain strings so our query layer can compare
    # them with normal == semantics.
    norm = json.loads(_json_util_dumps(docs))
    return [_normalize_ejson(d) for d in norm]


def _normalize_ejson(o: Any) -> Any:
    """Collapse Extended-JSON wrappers ($oid, $date, $numberLong, ...) into
    plain Python scalars so the filter engine can compare them naturally."""
    if isinstance(o, dict):
        # single-key extended-json wrappers
        if len(o) == 1:
            (k, v), = o.items()
            if k == "$oid" and isinstance(v, str):
                return v
            if k == "$date":
                if isinstance(v, str):
                    return v
                if isinstance(v, dict) and "$numberLong" in v:
                    try:
                        ms = int(v["$numberLong"])
                        return datetime.datetime.fromtimestamp(
                            ms / 1000, tz=datetime.timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                    except Exception:
                        return v
                return v
            if k in ("$numberLong", "$numberInt"):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
            if k in ("$numberDouble", "$numberDecimal"):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return v
        return {k: _normalize_ejson(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_normalize_ejson(x) for x in o]
    return o


def _ingest_docs(state: dict, db_name: str, coll_name: str,
                 docs: list[dict]) -> None:
    coll = _get_coll(state, db_name, coll_name, create=True)
    assert coll is not None
    for d in docs or []:
        if not isinstance(d, dict):
            continue
        if "_id" not in d:
            d["_id"] = _new_oid()
        coll["documents"].append(d)


# ---------------------------------------------------------------------------
# Query / filter engine ($eq $ne $gt $gte $lt $lte $in $nin $and $or $not
# $nor $regex $exists $size $all $elemMatch)
# ---------------------------------------------------------------------------

_MISSING = object()


def _get_path(doc: Any, path: str) -> Any:
    """Resolve a dotted field path on a doc, returning _MISSING if any
    segment is absent. Supports numeric indices into arrays."""
    cur: Any = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return _MISSING
        elif isinstance(cur, list):
            if part.isdigit():
                idx = int(part)
                if 0 <= idx < len(cur):
                    cur = cur[idx]
                else:
                    return _MISSING
            else:
                # apply path elementwise -> first match style
                vals = []
                for item in cur:
                    v = _get_path(item, part)
                    if v is not _MISSING:
                        vals.append(v)
                cur = vals if vals else _MISSING
                if cur is _MISSING:
                    return _MISSING
        else:
            return _MISSING
    return cur


def _set_path(doc: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur: Any = doc
    for p in parts[:-1]:
        nxt = cur.get(p) if isinstance(cur, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    if isinstance(cur, dict):
        cur[parts[-1]] = value


def _unset_path(doc: dict, path: str) -> None:
    parts = path.split(".")
    cur: Any = doc
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _values_for_match(doc: Any, path: str) -> list[Any]:
    """Return *all* candidate values at `path` (arrays expand) for matching.
    Mongo semantics: a predicate on `a.b` matches if the doc has a value at
    that path OR any element of an array at any prefix has the value."""
    val = _get_path(doc, path)
    if val is _MISSING:
        return []
    return [val]


def _match(doc: Any, query: Any) -> bool:
    if not isinstance(query, dict) or not query:
        return True
    for key, cond in query.items():
        if key.startswith("$"):
            if not _match_logical(doc, key, cond):
                return False
            continue
        # field-level predicate
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            val = _get_path(doc, key)
            if not _match_operators(val, cond):
                return False
        else:
            val = _get_path(doc, key)
            if not _eq_match(val, cond):
                return False
    return True


def _match_logical(doc: Any, op: str, cond: Any) -> bool:
    if op == "$and":
        return all(_match(doc, c) for c in (cond or []))
    if op == "$or":
        return any(_match(doc, c) for c in (cond or []))
    if op == "$nor":
        return not any(_match(doc, c) for c in (cond or []))
    if op == "$not":
        return not _match(doc, cond)
    if op == "$expr":
        # very small subset: allow {"$eq":[<a>,<b>]} etc with literals
        return _eval_expr(doc, cond)
    if op == "$where":
        return False  # unsupported in mock
    return True


def _match_operators(val: Any, cond: dict) -> bool:
    for op, arg in cond.items():
        if op == "$eq":
            if not _eq_match(val, arg):
                return False
        elif op == "$ne":
            if _eq_match(val, arg):
                return False
        elif op == "$gt":
            if not _cmp(val, arg, lambda a, b: a > b):
                return False
        elif op == "$gte":
            if not _cmp(val, arg, lambda a, b: a >= b):
                return False
        elif op == "$lt":
            if not _cmp(val, arg, lambda a, b: a < b):
                return False
        elif op == "$lte":
            if not _cmp(val, arg, lambda a, b: a <= b):
                return False
        elif op == "$in":
            if not any(_eq_match(val, x) for x in (arg or [])):
                return False
        elif op == "$nin":
            if any(_eq_match(val, x) for x in (arg or [])):
                return False
        elif op == "$exists":
            present = val is not _MISSING
            if bool(arg) != present:
                return False
        elif op == "$regex":
            pat = arg
            opts = cond.get("$options", "")
            flags = 0
            if "i" in opts:
                flags |= re.IGNORECASE
            if "s" in opts:
                flags |= re.DOTALL
            if "m" in opts:
                flags |= re.MULTILINE
            try:
                rx = re.compile(pat, flags)
            except re.error:
                return False
            candidates = val if isinstance(val, list) else [val]
            if not any(isinstance(c, str) and rx.search(c)
                       for c in candidates):
                return False
        elif op == "$options":
            continue  # consumed by $regex
        elif op == "$size":
            if not (isinstance(val, list) and len(val) == int(arg)):
                return False
        elif op == "$all":
            if not isinstance(val, list):
                return False
            for needle in (arg or []):
                if not any(_eq_match(v, needle) for v in val):
                    return False
        elif op == "$elemMatch":
            if not isinstance(val, list):
                return False
            if not any(_match(v, arg) for v in val):
                return False
        elif op == "$not":
            if _match_operators(val, arg):
                return False
        elif op == "$type":
            if not _check_type(val, arg):
                return False
        elif op == "$mod":
            try:
                divisor, remainder = arg
                if not isinstance(val, (int, float)):
                    return False
                if int(val) % int(divisor) != int(remainder):
                    return False
            except Exception:
                return False
        else:
            # unknown operator -> fail closed
            return False
    return True


def _eq_match(val: Any, target: Any) -> bool:
    """Mongo `$eq` semantics: equal directly, OR (if val is a list) any
    element equals target, OR if target is a list, structural equality."""
    if val is _MISSING:
        return target is None  # missing matches `None` if user asked
    if val == target:
        return True
    if isinstance(val, list) and not isinstance(target, list):
        return any(v == target for v in val)
    return False


def _cmp(val: Any, target: Any, op) -> bool:
    if val is _MISSING:
        return False
    candidates = val if isinstance(val, list) else [val]
    for c in candidates:
        try:
            if op(c, target):
                return True
        except TypeError:
            continue
    return False


def _check_type(val: Any, t: Any) -> bool:
    if val is _MISSING:
        return False
    name_map = {
        1: float, "double": float,
        2: str, "string": str,
        3: dict, "object": dict,
        4: list, "array": list,
        8: bool, "bool": bool,
        10: type(None), "null": type(None),
        16: int, "int": int,
        18: int, "long": int,
    }
    if isinstance(t, list):
        return any(_check_type(val, x) for x in t)
    cls = name_map.get(t)
    return cls is not None and isinstance(val, cls)


def _eval_expr(doc: Any, expr: Any) -> Any:
    """Tiny $expr evaluator -- supports literals, field refs ('$foo.bar'),
    and {$op: [...]} for $eq $ne $gt $gte $lt $lte $and $or $not $in."""
    if isinstance(expr, str) and expr.startswith("$"):
        v = _get_path(doc, expr[1:])
        return None if v is _MISSING else v
    if isinstance(expr, dict) and len(expr) == 1:
        (op, args), = expr.items()
        if op.startswith("$"):
            if op == "$literal":
                return args
            evaled = [_eval_expr(doc, a) for a in (args or [])] \
                if isinstance(args, list) else _eval_expr(doc, args)
            if op == "$eq":
                return evaled[0] == evaled[1]
            if op == "$ne":
                return evaled[0] != evaled[1]
            if op == "$gt":
                return _safe_cmp(evaled[0], evaled[1], lambda a, b: a > b)
            if op == "$gte":
                return _safe_cmp(evaled[0], evaled[1], lambda a, b: a >= b)
            if op == "$lt":
                return _safe_cmp(evaled[0], evaled[1], lambda a, b: a < b)
            if op == "$lte":
                return _safe_cmp(evaled[0], evaled[1], lambda a, b: a <= b)
            if op == "$and":
                return all(bool(x) for x in evaled)
            if op == "$or":
                return any(bool(x) for x in evaled)
            if op == "$not":
                return not bool(evaled if not isinstance(evaled, list)
                                else evaled[0])
            if op == "$in":
                return evaled[0] in (evaled[1] or [])
            if op == "$add":
                return sum(evaled)
            if op == "$subtract":
                return evaled[0] - evaled[1]
            if op == "$multiply":
                out = 1
                for v in evaled:
                    out *= v
                return out
            if op == "$divide":
                return evaled[0] / evaled[1]
            if op == "$concat":
                return "".join(str(x) for x in evaled if x is not None)
            if op == "$toLower":
                return str(evaled).lower() if not isinstance(evaled, list) \
                    else str(evaled[0]).lower()
            if op == "$toUpper":
                return str(evaled).upper() if not isinstance(evaled, list) \
                    else str(evaled[0]).upper()
        return expr
    if isinstance(expr, list):
        return [_eval_expr(doc, x) for x in expr]
    return expr


def _safe_cmp(a, b, op) -> bool:
    try:
        return op(a, b)
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Sort / project helpers
# ---------------------------------------------------------------------------

def _sort_docs(docs: list[dict], sort: dict | None) -> list[dict]:
    if not sort:
        return docs
    items = list(sort.items())

    def key_fn(d: dict):
        keys = []
        for field, _ in items:
            v = _get_path(d, field)
            if v is _MISSING:
                v = None
            # group by type to avoid TypeError comparing heterogeneous values
            type_rank = 0 if v is None else 1
            keys.append((type_rank, _SortableWrapper(v)))
        return keys

    out = list(docs)
    # apply each sort key in reverse for stable multi-key sort
    for field, direction in reversed(items):
        rev = int(direction) < 0
        out.sort(
            key=lambda d, f=field: (
                0 if _get_path(d, f) is _MISSING
                  or _get_path(d, f) is None else 1,
                _SortableWrapper(
                    None if _get_path(d, f) is _MISSING
                    else _get_path(d, f)
                ),
            ),
            reverse=rev,
        )
    return out


class _SortableWrapper:
    """Allow heterogeneous values to be compared without TypeError."""
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def _key(self):
        v = self.v
        if v is None:
            return (0, 0)
        if isinstance(v, bool):
            return (1, int(v))
        if isinstance(v, (int, float)):
            return (2, float(v))
        if isinstance(v, str):
            return (3, v)
        return (4, str(v))

    def __lt__(self, other):
        return self._key() < other._key()

    def __eq__(self, other):
        return self._key() == other._key()


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return copy.deepcopy(doc)
    # exclusion vs inclusion mode (per Mongo: _id is allowed to differ)
    non_id = {k: v for k, v in projection.items() if k != "_id"}
    include_mode = any(bool(v) and not isinstance(v, dict) for v in non_id.values())
    exclude_mode = any(v == 0 or v is False for v in non_id.values())
    if include_mode and exclude_mode:
        # invalid in real mongo; fall back to inclusion
        exclude_mode = False
    if include_mode:
        out: dict = {}
        for k, v in projection.items():
            if not v or isinstance(v, dict):
                continue
            val = _get_path(doc, k)
            if val is not _MISSING:
                _set_path(out, k, val)
        # _id behaviour
        if projection.get("_id", 1) and "_id" in doc:
            out["_id"] = doc["_id"]
        elif projection.get("_id") in (0, False) and "_id" in out:
            out.pop("_id", None)
        return out
    if exclude_mode:
        out = copy.deepcopy(doc)
        for k, v in projection.items():
            if v == 0 or v is False:
                _unset_path(out, k)
        return out
    return copy.deepcopy(doc)


# ---------------------------------------------------------------------------
# Update operators
# ---------------------------------------------------------------------------

def _apply_update(doc: dict, update: dict) -> dict:
    """Apply a Mongo-style update to `doc` (in place). If `update` has no
    `$`-prefixed keys, treat as a full replacement (preserving _id)."""
    has_ops = any(k.startswith("$") for k in update.keys())
    if not has_ops:
        # replacement document
        new_doc = copy.deepcopy(update)
        new_doc["_id"] = doc.get("_id")
        doc.clear()
        doc.update(new_doc)
        return doc
    for op, payload in update.items():
        if not isinstance(payload, dict):
            continue
        if op == "$set":
            for k, v in payload.items():
                _set_path(doc, k, v)
        elif op == "$unset":
            for k in payload.keys():
                _unset_path(doc, k)
        elif op == "$inc":
            for k, delta in payload.items():
                cur = _get_path(doc, k)
                if cur is _MISSING or cur is None:
                    cur = 0
                try:
                    _set_path(doc, k, cur + delta)
                except TypeError:
                    pass
        elif op == "$mul":
            for k, factor in payload.items():
                cur = _get_path(doc, k)
                if cur is _MISSING or cur is None:
                    cur = 0
                try:
                    _set_path(doc, k, cur * factor)
                except TypeError:
                    pass
        elif op == "$min":
            for k, v in payload.items():
                cur = _get_path(doc, k)
                if cur is _MISSING or (
                        cur is not None and v is not None and v < cur):
                    _set_path(doc, k, v)
        elif op == "$max":
            for k, v in payload.items():
                cur = _get_path(doc, k)
                if cur is _MISSING or (
                        cur is not None and v is not None and v > cur):
                    _set_path(doc, k, v)
        elif op == "$rename":
            for old, new in payload.items():
                v = _get_path(doc, old)
                if v is _MISSING:
                    continue
                _unset_path(doc, old)
                _set_path(doc, new, v)
        elif op == "$push":
            for k, v in payload.items():
                arr = _get_path(doc, k)
                if arr is _MISSING or arr is None:
                    arr = []
                    _set_path(doc, k, arr)
                if not isinstance(arr, list):
                    continue
                if isinstance(v, dict) and "$each" in v:
                    for item in v["$each"]:
                        arr.append(item)
                else:
                    arr.append(v)
        elif op == "$addToSet":
            for k, v in payload.items():
                arr = _get_path(doc, k)
                if arr is _MISSING or arr is None:
                    arr = []
                    _set_path(doc, k, arr)
                if not isinstance(arr, list):
                    continue
                items = v["$each"] if isinstance(v, dict) and "$each" in v \
                    else [v]
                for item in items:
                    if item not in arr:
                        arr.append(item)
        elif op == "$pull":
            for k, cond in payload.items():
                arr = _get_path(doc, k)
                if not isinstance(arr, list):
                    continue
                if isinstance(cond, dict) and any(
                        kk.startswith("$") for kk in cond):
                    arr[:] = [x for x in arr
                              if not _match_operators(x, cond)]
                elif isinstance(cond, dict):
                    arr[:] = [x for x in arr if not _match(x, cond)]
                else:
                    arr[:] = [x for x in arr if x != cond]
        elif op == "$pop":
            for k, direction in payload.items():
                arr = _get_path(doc, k)
                if isinstance(arr, list) and arr:
                    if int(direction) == -1:
                        arr.pop(0)
                    else:
                        arr.pop()
        elif op == "$currentDate":
            for k, v in payload.items():
                _set_path(doc, k, _now())
    return doc


# ---------------------------------------------------------------------------
# Aggregation pipeline
# ---------------------------------------------------------------------------

def _aggregate(docs: list[dict], pipeline: list[dict]) -> list[dict]:
    cur = [copy.deepcopy(d) for d in docs]
    for stage in pipeline or []:
        if not isinstance(stage, dict) or len(stage) != 1:
            continue
        (op, arg), = stage.items()
        if op == "$match":
            cur = [d for d in cur if _match(d, arg or {})]
        elif op == "$limit":
            cur = cur[: int(arg)]
        elif op == "$skip":
            cur = cur[int(arg):]
        elif op == "$sort":
            cur = _sort_docs(cur, arg or {})
        elif op == "$count":
            cur = [{arg: len(cur)}]
        elif op == "$project":
            cur = [_project(d, arg or {}) for d in cur]
        elif op in ("$addFields", "$set"):
            for d in cur:
                for k, v in (arg or {}).items():
                    _set_path(d, k, _eval_expr(d, v))
        elif op == "$unset":
            keys = arg if isinstance(arg, list) else [arg]
            for d in cur:
                for k in keys:
                    _unset_path(d, k)
        elif op == "$unwind":
            cur = _stage_unwind(cur, arg)
        elif op == "$group":
            cur = _stage_group(cur, arg or {})
        elif op == "$replaceRoot":
            new_cur = []
            for d in cur:
                root = _eval_expr(
                    d, (arg or {}).get("newRoot"))
                if isinstance(root, dict):
                    new_cur.append(root)
            cur = new_cur
        elif op == "$lookup":
            # not supported -- requires cross-collection access; skip
            continue
        else:
            # unknown stage -- skip (best-effort)
            continue
    return cur


def _stage_unwind(docs: list[dict], arg: Any) -> list[dict]:
    if isinstance(arg, str):
        path = arg.lstrip("$")
        preserve = False
    else:
        path = str(arg.get("path", "")).lstrip("$")
        preserve = bool(arg.get("preserveNullAndEmptyArrays"))
    out = []
    for d in docs:
        v = _get_path(d, path)
        if v is _MISSING or v is None:
            if preserve:
                out.append(d)
            continue
        if not isinstance(v, list):
            out.append(d)
            continue
        if not v and preserve:
            out.append(d)
            continue
        for item in v:
            nd = copy.deepcopy(d)
            _set_path(nd, path, item)
            out.append(nd)
    return out


def _stage_group(docs: list[dict], spec: dict) -> list[dict]:
    id_expr = spec.get("_id")
    accs = {k: v for k, v in spec.items() if k != "_id"}
    groups: dict[str, dict] = {}
    order: list[str] = []
    for d in docs:
        gid = _eval_expr(d, id_expr) if id_expr is not None else None
        gkey = json.dumps(gid, sort_keys=True, default=_json_default)
        if gkey not in groups:
            groups[gkey] = {"_id": gid, "_members": []}
            order.append(gkey)
        groups[gkey]["_members"].append(d)
    out = []
    for gkey in order:
        bucket = groups[gkey]
        rec = {"_id": bucket["_id"]}
        for field, expr in accs.items():
            rec[field] = _apply_acc(expr, bucket["_members"])
        out.append(rec)
    return out


def _apply_acc(expr: Any, members: list[dict]) -> Any:
    if not isinstance(expr, dict) or len(expr) != 1:
        return None
    (op, arg), = expr.items()
    vals = [_eval_expr(m, arg) for m in members]
    if op == "$sum":
        if isinstance(arg, (int, float)):
            return arg * len(members)
        return sum(v for v in vals if isinstance(v, (int, float)))
    if op == "$avg":
        nums = [v for v in vals if isinstance(v, (int, float))]
        return (sum(nums) / len(nums)) if nums else None
    if op == "$min":
        nums = [v for v in vals if v is not None]
        return min(nums) if nums else None
    if op == "$max":
        nums = [v for v in vals if v is not None]
        return max(nums) if nums else None
    if op == "$first":
        return vals[0] if vals else None
    if op == "$last":
        return vals[-1] if vals else None
    if op == "$push":
        return vals
    if op == "$addToSet":
        seen = []
        for v in vals:
            if v not in seen:
                seen.append(v)
        return seen
    if op == "$count":
        return len(members)
    return None


# ---------------------------------------------------------------------------
# Result helpers (match upstream server's CallToolResult shapes)
# ---------------------------------------------------------------------------

def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _result(*texts: str) -> dict:
    return {"content": [_text(t) for t in texts]}


def _err(msg: str) -> dict:
    return {"isError": True, "content": [_text(msg)]}


def _ejson(doc: Any) -> str:
    """Best-effort Extended-JSON for output. Datetimes -> ISO strings.
    ObjectId-like strings stay as plain strings."""
    return json.dumps(doc, default=_json_default, ensure_ascii=False)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("mongodb-mock")


@mcp.tool(name="connect")
def tool_connect(connectionString: str | None = None) -> dict:
    """Connect to a MongoDB instance.

    Upstream `connect` tool: in the mock there is no real network
    connection; every subsequent call uses the in-process state. We
    record the call and return success."""
    with _lock():
        s = _load_state()
        _record(s, "connect", connectionString=connectionString)
        _save_state(s)
    return _result("Successfully connected to MongoDB.")


@mcp.tool(name="switch-connection")
def tool_switch_connection(connectionString: str | None = None) -> dict:
    """Switch to a different MongoDB connection. Mock no-op."""
    with _lock():
        s = _load_state()
        _record(s, "switch_connection", connectionString=connectionString)
        _save_state(s)
    return _result("Successfully connected to MongoDB.")


@mcp.tool(name="list-databases")
def tool_list_databases() -> dict:
    """List all databases for a MongoDB connection."""
    with _lock():
        s = _load_state()
        names = sorted(s["databases"].keys())
        _record(s, "list_databases", count=len(names))
        _save_state(s)
    if not names:
        return _result("Name: admin, Size: 0 bytes")
    return {"content": [_text(f"Name: {n}, Size: {_db_size(s, n)} bytes")
                        for n in names]}


def _db_size(state: dict, name: str) -> int:
    db = state["databases"].get(name) or {}
    total = 0
    for coll in (db.get("collections") or {}).values():
        total += sum(len(_ejson(d).encode("utf-8"))
                     for d in coll.get("documents", []))
    return total


@mcp.tool(name="list-collections")
def tool_list_collections(database: str) -> dict:
    """List all collections for a given database."""
    with _lock():
        s = _load_state()
        db = s["databases"].get(database)
        names = sorted((db or {"collections": {}})["collections"].keys())
        _record(s, "list_collections", database=database, count=len(names))
        _save_state(s)
    if not names:
        return _result(
            f'No collections found for database "{database}". To create a '
            f'collection, use the "create-collection" tool.')
    return {"content": [_text(f'Name: "{n}"') for n in names]}


@mcp.tool(name="collection-schema")
def tool_collection_schema(database: str, collection: str) -> dict:
    """Describe the schema for a collection (inferred from up to 5 docs)."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        _record(s, "collection_schema", database=database,
                collection=collection,
                exists=coll is not None)
        _save_state(s)
    if coll is None or not coll["documents"]:
        return _result(
            f'Could not deduce the schema for "{database}.{collection}". '
            f"This may be because it doesn't exist or is empty.")
    sample = coll["documents"][:5]
    schema = _infer_schema(sample)
    return _result(
        f'Found {len(schema)} fields in the schema for '
        f'"{database}.{collection}"',
        json.dumps(schema, default=_json_default))


def _infer_schema(docs: list[dict]) -> dict:
    """Mongo-schema-style flat field map: {field: {types: [<type>, ...]}}."""
    out: dict[str, dict] = {}

    def walk(prefix: str, val: Any):
        t = _bson_type(val)
        node = out.setdefault(prefix, {"types": []})
        if t not in node["types"]:
            node["types"].append(t)
        if isinstance(val, dict):
            for k, v in val.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(val, list):
            for v in val[:3]:
                walk(prefix, v)

    for d in docs:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            walk(k, v)
    return out


def _bson_type(v: Any) -> str:
    if v is None:
        return "Null"
    if isinstance(v, bool):
        return "Boolean"
    if isinstance(v, int):
        return "Int32"
    if isinstance(v, float):
        return "Double"
    if isinstance(v, str):
        return "String"
    if isinstance(v, list):
        return "Array"
    if isinstance(v, dict):
        return "Document"
    return type(v).__name__


@mcp.tool(name="collection-indexes")
def tool_collection_indexes(database: str, collection: str) -> dict:
    """Describe the indexes for a collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        _record(s, "collection_indexes", database=database,
                collection=collection,
                exists=coll is not None)
        _save_state(s)
    if coll is None:
        return _result(
            f'The indexes for "{database}.{collection}" cannot be determined '
            f"because the collection does not exist.")
    indexes = coll.get("indexes", [_default_id_index()])
    head = _text(f'Found {len(indexes)} indexes in the collection '
                 f'"{collection}":')
    rest = [_text(
        f'Name "{ix["name"]}", definition: {json.dumps(ix["key"])}')
        for ix in indexes]
    return {"content": [head, *rest]}


@mcp.tool(name="collection-storage-size")
def tool_collection_storage_size(database: str, collection: str) -> dict:
    """Gets the size of the collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        _record(s, "collection_storage_size", database=database,
                collection=collection,
                exists=coll is not None)
        _save_state(s)
    if coll is None:
        return _result(
            f'The size of "{database}.{collection}" cannot be determined '
            f"because the collection does not exist.")
    size = sum(len(_ejson(d).encode("utf-8"))
               for d in coll.get("documents", []))
    units, scaled = _scale(size)
    return _result(
        f'The size of "{database}.{collection}" is `{scaled:.2f} {units}`')


def _scale(size: int) -> tuple[str, float]:
    kb, mb, gb = 1024, 1024 ** 2, 1024 ** 3
    if size > gb:
        return "GB", size / gb
    if size > mb:
        return "MB", size / mb
    if size > kb:
        return "KB", size / kb
    return "bytes", float(size)


@mcp.tool(name="db-stats")
def tool_db_stats(database: str) -> dict:
    """Returns statistics that reflect the use state of a single database."""
    with _lock():
        s = _load_state()
        db = s["databases"].get(database) or {"collections": {}}
        colls = db.get("collections") or {}
        total_docs = sum(len(c.get("documents", [])) for c in colls.values())
        size = _db_size(s, database)
        _record(s, "db_stats", database=database)
        _save_state(s)
    stats = {
        "db": database,
        "collections": len(colls),
        "objects": total_docs,
        "avgObjSize": (size / total_docs) if total_docs else 0,
        "dataSize": size,
        "storageSize": size,
        "indexes": sum(len(c.get("indexes", []))
                       for c in colls.values()),
        "indexSize": 0,
        "ok": 1,
    }
    return _result(
        f"Statistics for database {database}",
        json.dumps(stats, default=_json_default))


@mcp.tool(name="find")
def tool_find(database: str, collection: str,
              filter: dict | None = None,
              projection: dict | None = None,
              limit: int = 10,
              sort: dict | None = None) -> dict:
    """Run a find query against a MongoDB collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        docs = list(coll["documents"]) if coll else []
        matched = [d for d in docs if _match(d, filter or {})]
        matched = _sort_docs(matched, sort)
        if limit is not None and int(limit) > 0:
            matched = matched[: int(limit)]
        matched = [_project(d, projection) for d in matched]
        _record(s, "find", database=database, collection=collection,
                filter=filter, count=len(matched))
        _save_state(s)
    head = _text(f'Found {len(matched)} documents in the collection '
                 f'"{collection}":')
    return {"content": [head, *[_text(_ejson(d)) for d in matched]]}


@mcp.tool(name="count")
def tool_count(database: str, collection: str,
               query: dict | None = None) -> dict:
    """Gets the number of documents in a MongoDB collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        docs = coll["documents"] if coll else []
        n = sum(1 for d in docs if _match(d, query or {}))
        _record(s, "count", database=database, collection=collection,
                query=query, count=n)
        _save_state(s)
    return _result(f'Found {n} documents in the collection "{collection}"')


@mcp.tool(name="aggregate")
def tool_aggregate(database: str, collection: str,
                   pipeline: list[dict]) -> dict:
    """Run an aggregation against a MongoDB collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        docs = coll["documents"] if coll else []
        out = _aggregate(docs, pipeline or [])
        _record(s, "aggregate", database=database, collection=collection,
                stages=[next(iter(st)) for st in (pipeline or [])
                        if isinstance(st, dict) and st],
                count=len(out))
        _save_state(s)
    head = _text(f'Found {len(out)} documents in the collection '
                 f'"{collection}":')
    return {"content": [head, *[_text(_ejson(d)) for d in out]]}


@mcp.tool(name="insert-many")
def tool_insert_many(database: str, collection: str,
                     documents: list[dict]) -> dict:
    """Insert an array of documents into a MongoDB collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection, create=True)
        assert coll is not None
        ids: list[str] = []
        for d in documents or []:
            if not isinstance(d, dict):
                continue
            nd = copy.deepcopy(d)
            if "_id" not in nd:
                nd["_id"] = _new_oid()
            coll["documents"].append(nd)
            ids.append(str(nd["_id"]))
        _record(s, "insert_many", database=database, collection=collection,
                inserted=len(ids))
        _save_state(s)
    return _result(
        f'Inserted `{len(ids)}` document(s) into collection "{collection}"',
        f'Inserted IDs: {", ".join(ids)}',
    )


@mcp.tool(name="update-many")
def tool_update_many(database: str, collection: str,
                     update: dict,
                     filter: dict | None = None,
                     upsert: bool | None = None) -> dict:
    """Updates all documents that match the specified filter for a
    collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection, create=bool(upsert))
        matched = 0
        modified = 0
        upserted = 0
        upserted_id: str | None = None
        if coll is not None:
            for d in coll["documents"]:
                if _match(d, filter or {}):
                    matched += 1
                    before = json.dumps(d, sort_keys=True,
                                        default=_json_default)
                    _apply_update(d, update or {})
                    after = json.dumps(d, sort_keys=True,
                                       default=_json_default)
                    if before != after:
                        modified += 1
            if matched == 0 and upsert:
                new_doc: dict = {}
                # seed from $set / $setOnInsert and from equality predicates
                for k, v in (filter or {}).items():
                    if not k.startswith("$") and not isinstance(v, dict):
                        _set_path(new_doc, k, v)
                _apply_update(new_doc, update or {})
                if "_id" not in new_doc:
                    new_doc["_id"] = _new_oid()
                coll["documents"].append(new_doc)
                upserted = 1
                upserted_id = str(new_doc["_id"])
        _record(s, "update_many", database=database, collection=collection,
                filter=filter, matched=matched, modified=modified,
                upserted=upserted)
        _save_state(s)
    if matched == 0 and modified == 0 and upserted == 0:
        return _result("No documents matched the filter.")
    msg = f"Matched {matched} document(s)."
    if modified > 0:
        msg += f" Modified {modified} document(s)."
    if upserted > 0:
        msg += f" Upserted {upserted} document with id: {upserted_id}."
    return _result(msg)


@mcp.tool(name="delete-many")
def tool_delete_many(database: str, collection: str,
                     filter: dict | None = None) -> dict:
    """Removes all documents that match the filter from a MongoDB
    collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        n = 0
        if coll is not None:
            keep = []
            for d in coll["documents"]:
                if _match(d, filter or {}):
                    n += 1
                else:
                    keep.append(d)
            coll["documents"] = keep
        _record(s, "delete_many", database=database, collection=collection,
                filter=filter, deleted=n)
        _save_state(s)
    return _result(f'Deleted `{n}` document(s) from collection "{collection}"')


@mcp.tool(name="create-collection")
def tool_create_collection(database: str, collection: str) -> dict:
    """Creates a new collection in a database. If the database doesn't
    exist, it will be created automatically."""
    with _lock():
        s = _load_state()
        _get_coll(s, database, collection, create=True)
        _record(s, "create_collection", database=database,
                collection=collection)
        _save_state(s)
    return _result(
        f'Collection "{collection}" created in database "{database}".')


@mcp.tool(name="create-index")
def tool_create_index(database: str, collection: str,
                      keys: dict, name: str | None = None) -> dict:
    """Create an index for a collection."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection, create=True)
        assert coll is not None
        idx_name = name or "_".join(f"{k}_{v}" for k, v in keys.items())
        existing = next((ix for ix in coll["indexes"]
                         if ix["name"] == idx_name), None)
        if existing is None:
            coll["indexes"].append({"name": idx_name, "key": dict(keys),
                                    "unique": False})
        _record(s, "create_index", database=database, collection=collection,
                index=idx_name)
        _save_state(s)
    return _result(
        f'Created the index "{idx_name}" on collection "{collection}" '
        f'in database "{database}"')


@mcp.tool(name="drop-collection")
def tool_drop_collection(database: str, collection: str) -> dict:
    """Removes a collection or view from the database."""
    with _lock():
        s = _load_state()
        db = s["databases"].get(database)
        existed = bool(db and collection in (db.get("collections") or {}))
        if existed:
            del db["collections"][collection]
        _record(s, "drop_collection", database=database,
                collection=collection, dropped=existed)
        _save_state(s)
    return _result(
        f'{"Successfully dropped" if existed else "Failed to drop"} '
        f'collection "{collection}" from database "{database}"')


@mcp.tool(name="drop-database")
def tool_drop_database(database: str) -> dict:
    """Removes the specified database, deleting the associated data
    files."""
    with _lock():
        s = _load_state()
        existed = database in s["databases"]
        if existed:
            del s["databases"][database]
        _record(s, "drop_database", database=database, dropped=existed)
        _save_state(s)
    return _result(
        f'{"Successfully dropped" if existed else "Failed to drop"} '
        f'database "{database}"')


@mcp.tool(name="rename-collection")
def tool_rename_collection(database: str, collection: str, newName: str,
                           dropTarget: bool = False) -> dict:
    """Renames a collection in a MongoDB database."""
    with _lock():
        s = _load_state()
        db = s["databases"].get(database) or {"collections": {}}
        colls = db.setdefault("collections", {})
        if collection not in colls:
            _record(s, "rename_collection", database=database,
                    collection=collection, error="NamespaceNotFound")
            _save_state(s)
            return _result(
                f'Cannot rename "{database}.{collection}" because it '
                f"doesn't exist.")
        if newName in colls:
            if not dropTarget:
                _record(s, "rename_collection", database=database,
                        collection=collection,
                        error="NamespaceExists")
                _save_state(s)
                return _result(
                    f'Cannot rename "{database}.{collection}" to '
                    f'"{newName}" because the target collection already '
                    f'exists. If you want to overwrite it, set the '
                    f'"dropTarget" argument to true.')
            del colls[newName]
        colls[newName] = colls.pop(collection)
        s["databases"][database] = db
        _record(s, "rename_collection", database=database,
                collection=collection, newName=newName)
        _save_state(s)
    return _result(
        f'Collection "{collection}" renamed to "{newName}" in '
        f'database "{database}".')


@mcp.tool(name="explain")
def tool_explain(database: str, collection: str, method: list[dict]) -> dict:
    """Returns statistics describing the execution of the winning plan
    chosen by the query optimizer. The mock returns a synthetic
    queryPlanner-style stub identifying which index (if any) would be
    used based on the filter keys."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection)
        if not method:
            return _err("No method provided. Expected one of `aggregate`, "
                        "`find`, or `count`.")
        m = method[0]
        m_name = m.get("name")
        args = m.get("arguments", {}) or {}
        filt = args.get("filter") or args.get("query") or {}
        idx_name = "COLLSCAN"
        if coll:
            for ix in coll.get("indexes", []):
                if any(k in (filt or {}) for k in ix["key"].keys()):
                    idx_name = ix["name"]
                    break
        plan = {
            "queryPlanner": {
                "namespace": f"{database}.{collection}",
                "winningPlan": {
                    "stage": "COLLSCAN" if idx_name == "COLLSCAN"
                    else "FETCH",
                    "inputStage": {
                        "stage": "IXSCAN",
                        "indexName": idx_name,
                    } if idx_name != "COLLSCAN" else None,
                },
            },
            "ok": 1,
        }
        _record(s, "explain", database=database, collection=collection,
                method=m_name)
        _save_state(s)
    return _result(
        f'Here is some information about the winning plan chosen by the '
        f'query optimizer for running the given `{m_name}` operation in '
        f'"{database}.{collection}". This information can be used to '
        f'understand how the query was executed and to optimize the query '
        f'performance.',
        json.dumps(plan, default=_json_default))


@mcp.tool(name="mongodb-logs")
def tool_mongodb_logs(type: str = "global", limit: int = 50) -> dict:
    """Returns the most recent logged mongod events. The mock returns
    entries from its own `calls` log so verifiers can still inspect
    activity."""
    with _lock():
        s = _load_state()
        entries = s["calls"][-int(limit):]
        _record(s, "mongodb_logs", type=type, limit=limit)
        _save_state(s)
    head = _text(f"Found: {len(entries)} messages")
    return {"content": [head, *[_text(json.dumps(e, default=_json_default))
                                for e in entries]]}


# ---------------------------------------------------------------------------
# Debug helpers (not part of the upstream surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not exposed by the real
    MongoDB server; use for inspection/verification."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(database: str, collection: str,
                    documents: list[dict]) -> dict:
    """Mock-only: bulk-insert documents into a collection bypassing
    every check. Auto-creates the database/collection and auto-assigns
    `_id` when missing. Used by per-task preprocessing."""
    with _lock():
        s = _load_state()
        coll = _get_coll(s, database, collection, create=True)
        assert coll is not None
        ids = []
        for d in documents or []:
            if not isinstance(d, dict):
                continue
            nd = copy.deepcopy(d)
            if "_id" not in nd:
                nd["_id"] = _new_oid()
            coll["documents"].append(nd)
            ids.append(str(nd["_id"]))
        _record(s, "debug_seed", database=database, collection=collection,
                inserted=len(ids))
        _save_state(s)
    return {"inserted": len(ids), "insertedIds": ids}


if __name__ == "__main__":
    mcp.run()
