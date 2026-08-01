"""`google.api_core` shim namespace (exceptions + no-op retry)."""

from . import exceptions, retry  # noqa: F401

__all__ = ["exceptions", "retry"]
