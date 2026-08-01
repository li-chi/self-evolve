"""`google.cloud.storage` shim backed by the google-cloud-mock state.

Objects live in state.json under buckets/<name>/objects/<path> with the
same shape the MCP mock writes (content stored as text or base64), so a
file uploaded by the agent through `storage_upload_file` is readable here
and vice versa.
"""

from __future__ import annotations

import base64
import os

from .. import _mockstate as ms
from ..exceptions import Conflict, NotFound

__all__ = ["Client", "Bucket", "Blob"]


def _decode(obj: dict):
    if obj is None:
        return None
    if obj.get("content_b64") is not None:
        return base64.b64decode(obj["content_b64"])
    return (obj.get("content") or "").encode("utf-8")


class Blob:
    def __init__(self, name, bucket):
        self.name = name
        self.bucket = bucket
        self._client = bucket._client

    # -- metadata ----------------------------------------------------------

    @property
    def _entry(self):
        s = ms.load_state()
        return (s["buckets"].get(self.bucket.name, {})
                .get("objects", {}).get(self.name))

    @property
    def size(self):
        e = self._entry
        return e.get("size") if e else None

    @property
    def updated(self):
        e = self._entry
        return ms.parse_ts(e.get("updated") or e.get("created")) if e else None

    @property
    def time_created(self):
        e = self._entry
        return ms.parse_ts(e.get("created")) if e else None

    @property
    def content_type(self):
        e = self._entry
        return e.get("content_type") if e else None

    def exists(self, client=None):
        return self._entry is not None

    # -- io ----------------------------------------------------------------

    def download_as_bytes(self, client=None, **_kw):
        e = self._entry
        if e is None:
            raise NotFound(f"404 GET object {self.bucket.name}/{self.name}")
        return _decode(e)

    def download_as_string(self, client=None, **_kw):
        return self.download_as_bytes()

    def download_as_text(self, client=None, encoding="utf-8", **_kw):
        return self.download_as_bytes().decode(encoding)

    def download_to_filename(self, filename, client=None, **_kw):
        data = self.download_as_bytes()
        with open(filename, "wb") as f:
            f.write(data)

    def download_to_file(self, file_obj, client=None, **_kw):
        file_obj.write(self.download_as_bytes())

    def upload_from_string(self, data, content_type=None, client=None, **_kw):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._write(data, content_type)

    def upload_from_filename(self, filename, content_type=None, client=None,
                             **_kw):
        with open(filename, "rb") as f:
            self._write(f.read(), content_type)

    def upload_from_file(self, file_obj, content_type=None, client=None,
                         rewind=False, **_kw):
        if rewind:
            file_obj.seek(0)
        data = file_obj.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._write(data, content_type)

    def _write(self, data: bytes, content_type=None):
        try:
            text = data.decode("utf-8")
            payload = {"content": text, "content_b64": None}
        except UnicodeDecodeError:
            payload = {"content": None,
                       "content_b64": base64.b64encode(data).decode("ascii")}
        with ms.mutate() as s:
            b = s["buckets"].get(self.bucket.name)
            if b is None:
                raise NotFound(f"404 Bucket {self.bucket.name} not found")
            entry = b.setdefault("objects", {}).get(self.name, {})
            entry.update(payload)
            entry.update({
                "name": self.name,
                "size": len(data),
                "content_type": content_type or entry.get("content_type")
                or "application/octet-stream",
                "created": entry.get("created") or ms.now(),
                "updated": ms.now(),
            })
            b["objects"][self.name] = entry
            ms.record(s, "storage_upload_file", bucket_name=self.bucket.name,
                      object_name=self.name, size=len(data))

    def delete(self, client=None, **_kw):
        with ms.mutate() as s:
            b = s["buckets"].get(self.bucket.name)
            if b is None or self.name not in b.get("objects", {}):
                raise NotFound(
                    f"404 DELETE object {self.bucket.name}/{self.name}")
            del b["objects"][self.name]
            ms.record(s, "storage_delete_object",
                      bucket_name=self.bucket.name, object_name=self.name)

    @classmethod
    def _from_entry(cls, name, bucket):
        return cls(name, bucket)


class Bucket:
    def __init__(self, client, name):
        self._client = client
        self.name = name

    @property
    def _entry(self):
        return ms.load_state()["buckets"].get(self.name)

    @property
    def location(self):
        e = self._entry
        return (e or {}).get("location", "US")

    @property
    def storage_class(self):
        e = self._entry
        return (e or {}).get("storage_class", "STANDARD")

    @property
    def time_created(self):
        e = self._entry
        return ms.parse_ts(e.get("created")) if e else None

    @property
    def versioning_enabled(self):
        e = self._entry
        return (e or {}).get("versioning_enabled", False)

    @property
    def labels(self):
        e = self._entry
        return (e or {}).get("labels", {})

    def exists(self, client=None):
        return self._entry is not None

    def blob(self, name, **_kw):
        return Blob(name, self)

    def get_blob(self, name, client=None):
        blob = Blob(name, self)
        return blob if blob.exists() else None

    def list_blobs(self, prefix=None, max_results=None, **_kw):
        e = self._entry
        if e is None:
            raise NotFound(f"404 Bucket {self.name} not found")
        names = sorted(e.get("objects", {}))
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        if max_results:
            names = names[:max_results]
        return [Blob(n, self) for n in names]

    def delete(self, force=False, client=None):
        with ms.mutate() as s:
            e = s["buckets"].get(self.name)
            if e is None:
                raise NotFound(f"404 Bucket {self.name} not found")
            if e.get("objects") and not force:
                raise Conflict(f"409 Bucket {self.name} is not empty")
            del s["buckets"][self.name]
            ms.record(s, "storage_delete_bucket", bucket_name=self.name)

    def copy_blob(self, blob, destination_bucket, new_name=None, **_kw):
        data = blob.download_as_bytes()
        target = destination_bucket.blob(new_name or blob.name)
        target._write(data)
        return target

    def __repr__(self):
        return f"<Bucket: {self.name}>"


class Client:
    def __init__(self, project=None, credentials=None, **_kw):
        self.project = project or ms.PROJECT_ID
        self._credentials = credentials

    def bucket(self, name, user_project=None):
        return Bucket(self, name)

    def get_bucket(self, name, **_kw):
        name = name.name if isinstance(name, Bucket) else name
        b = Bucket(self, name)
        if not b.exists():
            raise NotFound(f"404 GET bucket {name}")
        return b

    def lookup_bucket(self, name):
        b = Bucket(self, name)
        return b if b.exists() else None

    def list_buckets(self, max_results=None, prefix=None, **_kw):
        s = ms.load_state()
        names = sorted(s["buckets"])
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        if max_results:
            names = names[:max_results]
        return [Bucket(self, n) for n in names]

    def create_bucket(self, bucket_or_name, project=None, location=None,
                      **_kw):
        name = bucket_or_name.name if isinstance(bucket_or_name, Bucket) \
            else bucket_or_name
        with ms.mutate() as s:
            if name in s["buckets"]:
                raise Conflict(f"409 Already Exists: bucket {name}")
            s["buckets"][name] = {
                "name": name,
                "location": location or "US",
                "storage_class": "STANDARD",
                "created": ms.now(),
                "versioning_enabled": False,
                "labels": {},
                "lifecycle_rules": [],
                "objects": {},
            }
            ms.record(s, "storage_create_bucket", bucket_name=name,
                      location=location or "US")
        return Bucket(self, name)

    def delete_bucket(self, bucket_or_name, force=False, **_kw):
        name = bucket_or_name.name if isinstance(bucket_or_name, Bucket) \
            else bucket_or_name
        Bucket(self, name).delete(force=force)

    def close(self):
        pass
