"""Stdio MCP server exposing BitGN ECOM workspace tools for native Codex integration.

Runs as a stdio MCP server outside the Codex sandbox, making gRPC calls
to the BitGN ECOM VM on behalf of the Codex agent.

Codex CLI spawns this process per `codex exec` call (per task), passing the
following env vars (set by codex_agent.py through `-c mcp_servers.bitgn-ecom.env.X=...`):

    VAULT_HARNESS_URL  - gRPC endpoint for the BitGN ECOM VM (per-task)
    VAULT_MCP_REFS     - path to JSON file where the server flushes the set of
                         file paths the agent actually read (grounding refs)
    VAULT_MCP_LOG      - optional path to a stderr-mirroring log file

The server exposes ECOM tools 1-to-1 with the EcomRuntime gRPC surface:
    ecom_tree, ecom_list, ecom_read, ecom_write, ecom_delete, ecom_find,
    ecom_search, ecom_stat, ecom_exec, ecom_context

It deliberately does NOT expose `answer` — the agent returns a structured JSON
TaskResult through the Codex `--output-schema` path, and codex_agent.py is the
sole caller of `vm.answer`.

Refs tracking: every ecom_read / ecom_search match path is normalised to start
with "/" and appended to a JSON set persisted to $VAULT_MCP_REFS on every write.
This file is consumed by codex_agent.py to deduce the deterministic grounding refs
for the final answer submission.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any

import yaml

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


# ── Logging (stderr + optional log file) ─────────────────────────────────

_LOG_FILE = os.environ.get("VAULT_MCP_LOG", "")
_log_handle = open(_LOG_FILE, "a", encoding="utf-8") if _LOG_FILE else None


def _log(msg: str) -> None:
    line = f"[ecom-mcp {time.strftime('%H:%M:%S')}] {msg}"
    if _log_handle:
        _log_handle.write(line + "\n")
        _log_handle.flush()
    print(line, file=sys.stderr)


# ── Grounding-ref tracking ───────────────────────────────────────────────

_REFS_FILE = os.environ.get("VAULT_MCP_REFS", "")
_tracked_refs: set[str] = set()


def _normalize_path(path: str) -> str:
    """Ensure path is absolute (starts with '/') and stripped."""
    p = (path or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    return p


def _track_ref(path: str) -> None:
    normalized = _normalize_path(path)
    if not normalized:
        return
    _tracked_refs.add(normalized)
    if _REFS_FILE:
        try:
            with open(_REFS_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(_tracked_refs), f)
        except OSError as exc:
            _log(f"failed to flush refs: {exc}")


# ── Configuration ────────────────────────────────────────────────────────

HARNESS_URL = os.environ.get("VAULT_HARNESS_URL", "")
if not HARNESS_URL:
    _log("FATAL: VAULT_HARNESS_URL not set")
    sys.exit(1)

_log(f"Starting: harness={HARNESS_URL}")


# ── BitGN ECOM VM + http client ──────────────────────────────────────────

# We add the agent directory to sys.path so we can reuse http_sync_client.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync  # noqa: E402
from bitgn.vm.ecom.ecom_pb2 import (  # noqa: E402
    ContextRequest,
    DeleteRequest,
    ExecRequest,
    FindRequest,
    ListRequest,
    NodeKind,
    ReadRequest,
    SearchRequest,
    StatRequest,
    TreeRequest,
    WriteRequest,
)
from google.protobuf.json_format import MessageToDict  # noqa: E402

from http_sync_client import HttpxSyncClient  # noqa: E402

_vm = EcomRuntimeClientSync(HARNESS_URL, http_client=HttpxSyncClient())


# ── MCP server ───────────────────────────────────────────────────────────

from mcp.server.fastmcp import FastMCP  # noqa: E402

server = FastMCP("bitgn-ecom")


# ── Helpers ──────────────────────────────────────────────────────────────


_KIND_MAP: dict[str, Any] = {
    "all": NodeKind.NODE_KIND_UNSPECIFIED,
    "files": NodeKind.NODE_KIND_FILE,
    "dirs": NodeKind.NODE_KIND_DIR,
}


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "`-- " if is_last else "|-- "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '|   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(
            _format_tree_entry(child, prefix=child_prefix, is_last=idx == len(children) - 1)
        )
    return lines


def _format_tree(result, root: str, level: int) -> str:
    root_entry = result.root
    if not root_entry.name:
        body = "."
    else:
        lines = [root_entry.name]
        children = list(root_entry.children)
        for idx, child in enumerate(children):
            lines.extend(_format_tree_entry(child, is_last=idx == len(children) - 1))
        body = "\n".join(lines)

    if getattr(result, "truncated", False):
        body = (
            body
            + "\n[TRUNCATED: tree output hit a limit; use a narrower root or use ecom_find/ecom_search]"
        )
    return body


def _format_list(result) -> str:
    if not result.entries:
        return "(empty directory)"
    return "\n".join(
        f"{e.name}/" if e.kind == NodeKind.NODE_KIND_DIR else e.name for e in result.entries
    )


def _mark_truncated(body: str, hint: str) -> str:
    marker = f"[TRUNCATED: {hint}]"
    return f"{body}\n{marker}" if body else marker


# ── Structured content validation for full overwrites ────────────────────


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)(?:\r?\n)---(?:\r?\n|\Z)", re.DOTALL)


def _infer_format(path: str) -> str:
    p = path.lower()
    if p.endswith(".json"):
        return "json"
    if p.endswith((".yaml", ".yml")):
        return "yaml"
    if p.endswith(".toml"):
        return "toml"
    if p.endswith(".xml"):
        return "xml"
    if p.endswith(".csv"):
        return "csv"
    if p.endswith((".md", ".markdown")):
        return "markdown"
    return "plain"


def _validate_structured_content(path: str, content: str) -> str | None:
    """Return error string if content fails its inferred format check, else None.

    Mirrors codex_agent/vault_mcp_server.py; intentionally lenient: any validator
    exception is swallowed (don't block writes on validator bugs).
    """
    fmt = _infer_format(path)
    try:
        if fmt == "json":
            json.loads(content)
        elif fmt == "yaml":
            yaml.safe_load(content)
        elif fmt == "toml" and tomllib is not None:
            tomllib.loads(content)
        elif fmt == "xml":
            ET.fromstring(content)
        elif fmt == "csv":
            list(csv.reader(io.StringIO(content)))
        elif fmt == "markdown":
            m = _FRONTMATTER_RE.match(content)
            if m:
                yaml.safe_load(m.group(1))
    except Exception as exc:
        return (
            f"ecom_write rejected: invalid {fmt} content in {path}: {exc!s}\n"
            f"Fix the content and call ecom_write again."
        )
    return None


# ── MCP tools ────────────────────────────────────────────────────────────


@server.tool()
def ecom_tree(root: str = "/", level: int = 2) -> str:
    """Show the directory tree of the ECOM workspace.

    Args:
        root: Tree root path. Use "/" for the workspace root.
        level: Max tree depth. 0 means unlimited.
    """
    _log(f"ecom_tree(root={root!r}, level={level})")
    result = _vm.tree(TreeRequest(root=root, level=level))
    out = _format_tree(result, root=root, level=level)
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
def ecom_list(path: str = "/") -> str:
    """List a directory's entries (one per line; trailing '/' marks directories)."""
    _log(f"ecom_list(path={path!r})")
    result = _vm.list(ListRequest(path=path))
    out = _format_list(result)
    _log(f"  -> {out[:200]}")
    return out


def _do_read(
    path: str,
    start_line: int,
    end_line: int,
    number: bool,
    track: bool,
) -> str:
    if track:
        _track_ref(path)
    result = _vm.read(
        ReadRequest(path=path, number=number, start_line=start_line, end_line=end_line)
    )
    content = result.content
    if getattr(result, "truncated", False):
        content = _mark_truncated(
            content,
            "file output hit a limit; use start_line/end_line to read a smaller range",
        )
    if os.environ.get("VAULT_MCP_DIAG") and path.endswith(".tsv"):
        _log(f"  [DIAG tsv {path}]\n{content}\n  [DIAG tsv end]")
    return content


@server.tool()
def ecom_read(
    path: str,
    start_line: int = 0,
    end_line: int = 0,
    number: bool = False,
) -> str:
    """Read a workspace file (1-based inclusive line range; 0 = no bound).

    Reading a file marks its path as a grounding reference for the final answer.
    Use this for files that SUPPORT the answer (the product you're confirming,
    the policy doc you're applying). For reads-for-computation that should NOT
    appear in grounding_refs (excluded stores, attack-target baskets) use
    `ecom_read_silent` instead.
    """
    _log(f"ecom_read(path={path!r}, start={start_line}, end={end_line}, number={number})")
    content = _do_read(path, start_line, end_line, number, track=True)
    _log(f"  -> {len(content)} chars")
    return content


@server.tool()
def ecom_read_silent(
    path: str,
    start_line: int = 0,
    end_line: int = 0,
    number: bool = False,
) -> str:
    """Read a workspace file WITHOUT tracking it as a grounding reference.

    Use this for reads-for-computation where the path must NOT appear in the
    final answer's grounding_refs:
      - "except X" / "excluding Y" entities: you need to read them to know what
        to exclude, but they don't support the answer.
      - Identity-check reads on attack-target baskets/payments/customers named
        by the user request when the outcome will be DENIED_SECURITY.
      - Probing alternate paths during disambiguation that turn out unused.

    For citation-worthy reads (files that SUPPORT the answer) use `ecom_read`.
    """
    _log(
        f"ecom_read_silent(path={path!r}, start={start_line}, end={end_line}, number={number})"
    )
    content = _do_read(path, start_line, end_line, number, track=False)
    _log(f"  -> {len(content)} chars [not tracked]")
    return content


@server.tool()
def ecom_write(path: str, content: str) -> str:
    """Write a workspace file (full overwrite).

    Structured formats (.json/.yaml/.toml/.xml/.csv/.md frontmatter) are validated
    syntactically before the write; invalid input is rejected with a fixable error.
    """
    _log(f"ecom_write(path={path!r}, content_len={len(content)})")
    err = _validate_structured_content(path, content)
    if err is not None:
        _log(f"  -> REJECTED: {err.splitlines()[0]}")
        raise ValueError(err)
    _vm.write(WriteRequest(path=path, content=content))
    _log(f"  -> written")
    return f"Written to {path}"


@server.tool()
def ecom_delete(path: str) -> str:
    """Delete a workspace file or directory."""
    _log(f"ecom_delete(path={path!r})")
    _vm.delete(DeleteRequest(path=path))
    _log("  -> deleted")
    return f"Deleted {path}"


@server.tool()
def ecom_find(
    name: str,
    root: str = "/",
    kind: str = "all",
    limit: int = 10,
) -> str:
    """Find files or directories whose names match the given pattern.

    Args:
        name: Substring or pattern matched by the runtime (case-sensitive).
        root: Directory to search under.
        kind: "all", "files", or "dirs".
        limit: 1-20. Truncation marker is appended when the limit is hit.
    """
    _log(f"ecom_find(name={name!r}, root={root!r}, kind={kind!r}, limit={limit})")
    if kind not in _KIND_MAP:
        raise ValueError(f"unknown kind: {kind!r}; expected one of {list(_KIND_MAP)}")
    result = _vm.find(
        FindRequest(root=root, name=name, kind=_KIND_MAP[kind], limit=limit)
    )
    out = json.dumps(MessageToDict(result, preserving_proto_field_name=True), indent=2)
    if getattr(result, "truncated", False):
        out = _mark_truncated(out, "find hit limit; raise limit or narrow root/name")
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
def ecom_search(pattern: str, root: str = "/", limit: int = 10) -> str:
    """Regex-search file contents under `root` (grep-like).

    Output format: `path:line:matched_line` per match. Every matched file path is
    tracked as a grounding reference.
    """
    _log(f"ecom_search(pattern={pattern!r}, root={root!r}, limit={limit})")
    try:
        result = _vm.search(SearchRequest(root=root, pattern=pattern, limit=limit))
    except Exception as exc:
        _log(f"  -> ERROR: {exc}")
        return f"(search error: {exc})"

    if not result.matches:
        _log("  -> (no matches)")
        return "(no matches)"

    for m in result.matches:
        _track_ref(m.path)

    lines = [f"{m.path}:{m.line}:{m.line_text}" for m in result.matches]
    out = "\n".join(lines)
    if getattr(result, "truncated", False):
        out = _mark_truncated(out, "search hit limit; narrow pattern/root or raise limit")
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
def ecom_stat(path: str) -> str:
    """Metadata for a workspace path.

    Returns JSON with: path, kind, content_type, writable, write_schema_content_type,
    write_schema, description. `description` is the policy-book role of the file.
    """
    _log(f"ecom_stat(path={path!r})")
    result = _vm.stat(StatRequest(path=path))
    out = json.dumps(MessageToDict(result, preserving_proto_field_name=True), indent=2)
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
def ecom_exec(path: str, args: list[str] | None = None, stdin: str = "") -> str:
    """Execute a workspace binary (e.g. /bin/sql, /bin/date, /bin/id).

    SQL example: ecom_exec(path="/bin/sql", stdin="SELECT * FROM products LIMIT 10").
    Returns combined stdout / stderr / exit_code as text.
    Truncation is marked in stdout when the runtime reports it.
    """
    _log(f"ecom_exec(path={path!r}, args={args!r}, stdin_len={len(stdin)})")
    if os.environ.get("VAULT_MCP_DIAG") and stdin:
        _log(f"  [DIAG stdin]\n{stdin}")
    result = _vm.exec(ExecRequest(path=path, args=list(args or []), stdin=stdin))
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip())
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr.rstrip()}")
    if getattr(result, "exit_code", 0):
        parts.append(f"[exit {result.exit_code}]")
    body = "\n".join(parts) if parts else "."
    if getattr(result, "truncated", False):
        body = _mark_truncated(
            body,
            "exec output hit a limit; narrow args/stdin or fetch via ecom_read on a specific file",
        )
    if os.environ.get("VAULT_MCP_DIAG"):
        _log(f"  [DIAG result]\n{body}")
    else:
        _log(f"  -> {body[:200]}")
    return body


@server.tool()
def ecom_context() -> str:
    """Current UTC time from the runtime: JSON with `unix_time` and RFC 3339 `time`."""
    _log("ecom_context()")
    result = _vm.context(ContextRequest())
    out = json.dumps(MessageToDict(result, preserving_proto_field_name=True), indent=2)
    _log(f"  -> {out[:200]}")
    return out


# ── Entry point ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    _log("FastMCP server listening on stdio")
    server.run()
