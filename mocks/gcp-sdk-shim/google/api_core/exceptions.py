"""`google.api_core.exceptions` shim — mock-raised error hierarchy."""


class GoogleAPIError(Exception):
    code = None


class GoogleAPICallError(GoogleAPIError):
    """Raised for errors returned by an API call (real SDK's base class)."""

    def __init__(self, message="", errors=(), response=None, **_kw):
        super().__init__(message)
        self.message = message
        self.errors = list(errors)
        self.response = response


class ClientError(GoogleAPICallError):
    pass


class BadRequest(ClientError):
    code = 400


class Unauthorized(ClientError):
    code = 401


class Forbidden(ClientError):
    code = 403


class NotFound(ClientError):
    code = 404


class Conflict(ClientError):
    code = 409


class PreconditionFailed(ClientError):
    code = 412


class TooManyRequests(ClientError):
    code = 429


class ServerError(GoogleAPICallError):
    pass


class InternalServerError(ServerError):
    code = 500


class ServiceUnavailable(ServerError):
    code = 503


class RetryError(GoogleAPIError):
    pass


class AlreadyExists(Conflict):
    pass


class PermissionDenied(Forbidden):
    pass


class InvalidArgument(BadRequest):
    pass


class DeadlineExceeded(ServerError):
    code = 504
