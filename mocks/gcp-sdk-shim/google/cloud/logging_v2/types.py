"""`google.cloud.logging_v2.types` shim — request/resource message stubs.

Toolathlon's preprocess builds `LogBucket(...)` / `CreateBucketRequest(...)`
and hands them to ConfigServiceV2Client. These plain classes carry the same
attributes; the shim client reads them with getattr, exactly as the real
proto messages are read.
"""


class LogBucket:
    def __init__(self, retention_days=30, description="", locked=False,
                 name=None, **kwargs):
        self.retention_days = retention_days
        self.description = description
        self.locked = locked
        self.name = name
        for k, v in kwargs.items():
            setattr(self, k, v)


class CreateBucketRequest:
    def __init__(self, parent=None, bucket_id=None, bucket=None, **kwargs):
        self.parent = parent
        self.bucket_id = bucket_id
        self.bucket = bucket
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get(self, key, default=None):
        return getattr(self, key, default)


class DeleteBucketRequest:
    def __init__(self, name=None, **kwargs):
        self.name = name
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get(self, key, default=None):
        return getattr(self, key, default)


class GetBucketRequest(DeleteBucketRequest):
    pass


class ListBucketsRequest:
    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get(self, key, default=None):
        return getattr(self, key, default)


class LogSink:
    def __init__(self, name=None, destination=None, filter=None,
                 description="", **kwargs):
        self.name = name
        self.destination = destination
        self.filter = filter
        self.description = description
        for k, v in kwargs.items():
            setattr(self, k, v)
