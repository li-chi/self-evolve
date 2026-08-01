"""Load a mock MCP server module under its own name.

Every mock ships a module called `server`, so a plain `import server`
would hand the second router the first router's module (sys.modules is
keyed by name). Each mock is therefore loaded from its file under a unique
module name, and cached here.
"""

from __future__ import annotations

import importlib.util
import os
import sys

MOCKS_ROOT = os.environ.get("MOCKS_ROOT", "/opt/mocks")
_CACHE: dict = {}


def load(mock_dir: str):
    """Import <MOCKS_ROOT>/<mock_dir>/server.py as `mock_<mock_dir>`."""
    if mock_dir in _CACHE:
        return _CACHE[mock_dir]
    path = os.path.join(MOCKS_ROOT, mock_dir, "server.py")
    name = "mock_" + mock_dir.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # the mock's own directory must be importable for its local helpers
    sys.path.insert(0, os.path.dirname(path))
    spec.loader.exec_module(module)
    _CACHE[mock_dir] = module
    return module
