"""pdf-form — a minimal MCP server for AcroForm PDF forms (list fields / fill).

The read-only `pdf-tools` server covers reading; this adds the WRITE half the
form-fill lever needs: the agent lists a blank form's named fields (with their
tooltips — which may carry per-field instructions) and writes a filled copy.
Paths are confined to the workspace (PDF_FORM_WORKSPACE or cwd)."""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader, PdfWriter

WORKSPACE = Path(os.environ.get("PDF_FORM_WORKSPACE") or os.getcwd()).resolve()
mcp = FastMCP("pdf-form")


def _resolve(rel: str) -> Path:
    p = (WORKSPACE / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
    if not str(p).startswith(str(WORKSPACE)):
        raise ValueError(f"path escapes workspace: {rel}")
    return p


@mcp.tool()
def list_form_fields(path: str) -> dict:
    """List a PDF form's fields: {field_name: {"value": current, "tooltip": hint}}.
    `path` is workspace-relative."""
    fields = PdfReader(_resolve(path)).get_fields() or {}
    return {k: {"value": v.get("/V") or "", "tooltip": v.get("/TU") or ""}
            for k, v in fields.items()}


@mcp.tool()
def fill_form_fields(path: str, fields: dict, output_path: str) -> str:
    """Fill named fields of the PDF form at `path` (workspace-relative) and save the
    filled copy to `output_path`. `fields` maps field name -> string value; unnamed
    fields keep their current value. Returns the saved path."""
    src = _resolve(path)
    dest = _resolve(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(src)
    known = set((reader.get_fields() or {}).keys())
    unknown = sorted(set(map(str, fields)) - known)
    if unknown:
        raise ValueError(f"unknown field(s): {unknown}; form has: {sorted(known)}")
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        writer.update_page_form_field_values(
            page, {str(k): str(v) for k, v in fields.items()})
    with open(dest, "wb") as f:
        writer.write(f)
    return str(dest.relative_to(WORKSPACE))


if __name__ == "__main__":
    mcp.run()
