"""`ConfigServiceV2Client` shim — log buckets/sinks from the mock state.

Graders use it as:
    client = ConfigServiceV2Client(credentials=creds)
    for bucket in client.list_buckets(parent=f"projects/{p}/locations/global"):
        bucket.name / .description / .retention_days / .create_time / .locked
"""

from __future__ import annotations

from google.cloud import _mockstate as ms

__all__ = ["ConfigServiceV2Client", "LogBucket", "LogSink"]


def _unpack(request, *fields):
    """Read fields from a request dict/object, falling back to kwargs.

    The real client accepts either `client.op(request={...})` or
    `client.op(field=...)`; Toolathlon's code uses both forms.
    """
    out = []
    for name, kwarg_value in fields:
        value = kwarg_value
        if value is None and request is not None:
            value = request.get(name) if isinstance(request, dict) \
                else getattr(request, name, None)
        out.append(value)
    return out


class _LifecycleState:
    """Stands in for the protobuf enum: callers read `.name`."""

    def __init__(self, name: str):
        self.name = name
        self.value = {"ACTIVE": 1, "DELETE_REQUESTED": 2}.get(name, 0)

    def __eq__(self, other):
        return self.name == (other.name if isinstance(other, _LifecycleState)
                             else other)

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.name


class LogBucket:
    def __init__(self, bucket_id: str, entry: dict, project: str,
                 location: str = "global"):
        self.bucket_id = bucket_id
        self.name = entry.get(
            "name",
            f"projects/{project}/locations/{location}/buckets/{bucket_id}")
        self.description = entry.get("description", "")
        self.retention_days = entry.get("retention_days", 30)
        self.locked = entry.get("locked", False)
        self.lifecycle_state = _LifecycleState(
            entry.get("lifecycle_state", "ACTIVE"))
        self.create_time = ms.parse_ts(entry.get("created"))
        self.update_time = ms.parse_ts(entry.get("updated")
                                       or entry.get("created"))
        self.location = entry.get("location", location)

    def __repr__(self):
        return f"<LogBucket {self.name}>"


class LogSink:
    def __init__(self, sink_id: str, entry: dict, project: str):
        self.sink_id = sink_id
        self.name = entry.get("name", f"projects/{project}/sinks/{sink_id}")
        self.destination = entry.get("destination", "")
        self.filter = entry.get("filter", "")
        self.description = entry.get("description", "")
        self.create_time = ms.parse_ts(entry.get("created"))


class ConfigServiceV2Client:
    def __init__(self, credentials=None, project=None, **_kw):
        self._credentials = credentials
        self.project = project or ms.PROJECT_ID

    @staticmethod
    def _project_of(parent: str, default: str) -> str:
        parts = (parent or "").split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "projects" \
            else default

    def list_buckets(self, request=None, *, parent=None, **_kw):
        (parent,) = _unpack(request, ("parent", parent))
        project = self._project_of(parent, self.project)
        s = ms.load_state()
        return [LogBucket(bid, entry, project)
                for bid, entry in sorted(s.get("log_buckets", {}).items())]

    def get_bucket(self, request=None, *, name=None, **_kw):
        (name,) = _unpack(request, ("name", name))
        bucket_id = (name or "").split("/")[-1]
        s = ms.load_state()
        entry = s.get("log_buckets", {}).get(bucket_id)
        if entry is None:
            from google.api_core.exceptions import NotFound
            raise NotFound(f"404 Log bucket {bucket_id} not found")
        return LogBucket(bucket_id, entry, self.project)

    def create_bucket(self, request=None, *, parent=None, bucket_id=None,
                      bucket=None, **_kw):
        parent, bucket_id, bucket = _unpack(
            request, ("parent", parent), ("bucket_id", bucket_id),
            ("bucket", bucket))
        project = self._project_of(parent, self.project)
        location = (parent or "").split("/")[-1] or "global"
        with ms.mutate() as s:
            if bucket_id in s.setdefault("log_buckets", {}):
                from google.api_core.exceptions import AlreadyExists
                raise AlreadyExists(f"409 Log bucket {bucket_id} exists")
            s["log_buckets"][bucket_id] = {
                "name": (f"projects/{project}/locations/{location}/buckets/"
                         f"{bucket_id}"),
                "location": location,
                "retention_days": getattr(bucket, "retention_days", 30) or 30,
                "description": getattr(bucket, "description", "") or "",
                "created": ms.now(),
                "lifecycle_state": "ACTIVE",
            }
            ms.record(s, "logging_create_log_bucket", bucket_id=bucket_id,
                      location=location)
        return self.get_bucket(name=f"projects/{project}/locations/"
                                    f"{location}/buckets/{bucket_id}")

    def delete_bucket(self, request=None, *, name=None, **_kw):
        (name,) = _unpack(request, ("name", name))
        bucket_id = (name or "").split("/")[-1]
        with ms.mutate() as s:
            if bucket_id not in s.get("log_buckets", {}):
                from google.api_core.exceptions import NotFound
                raise NotFound(f"404 Log bucket {bucket_id} not found")
            del s["log_buckets"][bucket_id]
            ms.record(s, "logging_delete_log_bucket", bucket_id=bucket_id)

    def list_sinks(self, request=None, *, parent=None, **_kw):
        (parent,) = _unpack(request, ("parent", parent))
        project = self._project_of(parent, self.project)
        s = ms.load_state()
        return [LogSink(sid, entry, project)
                for sid, entry in sorted(s.get("log_sinks", {}).items())]
