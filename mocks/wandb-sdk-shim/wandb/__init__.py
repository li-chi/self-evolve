"""`wandb` client shim backed by the wandb-mock state.

Graders use `wandb.Api()` to read a project's runs and their logged
history. The real client speaks GraphQL to api.wandb.ai and needs a login;
this shim reads the same state.json the wandb mock MCP server serves to the
agent, so both see one workspace.

Only the read surface Toolathlon's graders use is implemented — Api.runs,
Api.run, run.history/scan_history/summary/config — everything else raises
so a gap is loud.
"""

from __future__ import annotations

import json
import os

__version__ = "0.0.0-mock"


def _state_path() -> str:
    d = os.environ.get("WANDB_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/wandb_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _load() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("WANDB_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, encoding="utf-8") as f:
                return json.load(f)
        return {"viewer": {}, "projects": {}, "runs": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Run:
    def __init__(self, record: dict):
        self._r = record
        self.id = record.get("id") or record.get("name")
        self.name = record.get("display_name") or record.get("name") or self.id
        self.entity = record.get("entity")
        self.project = record.get("project")
        self.state = record.get("state", "finished")
        self.config = record.get("config", {}) or {}
        self.summary = record.get("summary", {}) or {}
        self.tags = record.get("tags", []) or []
        self.created_at = record.get("created_at")
        self.path = [self.entity, self.project, self.id]
        self.url = (f"https://wandb.ai/{self.entity}/{self.project}/runs/"
                    f"{self.id}")

    def history(self, samples: int = 500, keys=None, pandas: bool = True,
                **_kw):
        rows = list(self._r.get("history", []))
        if keys:
            rows = [{k: r.get(k) for k in list(keys) + ["_step"] if k in r}
                    for r in rows]
        if samples and len(rows) > samples:
            step = max(1, len(rows) // samples)
            rows = rows[::step]
        if pandas:
            try:
                import pandas as pd
                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def scan_history(self, keys=None, page_size: int = 1000, **_kw):
        for row in self._r.get("history", []):
            yield ({k: row.get(k) for k in keys if k in row}
                   if keys else dict(row))

    def file(self, name):
        raise NotImplementedError("wandb shim: Run.file is not implemented")

    def __repr__(self):
        return f"<Run {self.entity}/{self.project}/{self.id}>"


class Runs(list):
    """List of runs; the real object is lazy but iterates the same way."""


class Api:
    def __init__(self, *args, **kwargs):
        self._state = _load()

    @property
    def viewer(self):
        return self._state.get("viewer", {})

    def runs(self, path: str = "", filters=None, order=None, per_page=50,
             **_kw):
        entity, _, project = path.partition("/")
        out = []
        for record in self._state.get("runs", {}).values():
            if entity and record.get("entity") != entity:
                continue
            if project and record.get("project") != project:
                continue
            if filters and not _match(record, filters):
                continue
            out.append(Run(record))
        return Runs(out)

    def run(self, path: str):
        parts = path.split("/")
        entity, project, run_id = (parts + [None, None, None])[:3]
        for record in self._state.get("runs", {}).values():
            if record.get("id") != run_id and record.get("name") != run_id:
                continue
            if entity and record.get("entity") != entity:
                continue
            if project and record.get("project") != project:
                continue
            return Run(record)
        raise ValueError(f"Could not find run {path}")

    def project(self, name: str, entity: str | None = None):
        for p in self._state.get("projects", {}).values():
            if p.get("name") == name and (not entity
                                          or p.get("entity") == entity):
                return p
        raise ValueError(f"Could not find project {name}")

    def projects(self, entity: str | None = None, **_kw):
        return [p for p in self._state.get("projects", {}).values()
                if not entity or p.get("entity") == entity]


def _match(record: dict, filters: dict) -> bool:
    for key, want in filters.items():
        got = record
        for part in key.split("."):
            got = (got or {}).get(part) if isinstance(got, dict) else None
        if isinstance(want, dict):          # {"$gt": x} style
            for op, value in want.items():
                if op in ("$eq",) and got != value:
                    return False
                if op == "$gt" and not (got is not None and got > value):
                    return False
                if op == "$lt" and not (got is not None and got < value):
                    return False
                if op == "$in" and got not in value:
                    return False
        elif got != want:
            return False
    return True


def login(*_args, **_kwargs):
    return True


def init(*_args, **_kwargs):
    raise NotImplementedError(
        "wandb shim: run creation is not implemented (graders only read)")
