"""Shell-shaped formatters for the bootstrap output.

These render the ECOM VM's protobuf responses (tree / list / read / search /
exec) into terminal-shaped text — e.g. a `tree -L 2 /` listing or a
`cat -n <file>` block — so the model sees familiar shell output instead of
raw protobuf. Used by hermes_agent.py to assemble the `<bootstrap-output>`
section of the prompt. Truncation is always marked so the model never goes
blind on a large directory or file.
"""

from __future__ import annotations

import shlex

from bitgn.vm.ecom.ecom_pb2 import NodeKind


def _is_truncated(result) -> bool:
    return bool(getattr(result, "truncated", False))


def _mark_truncated(result, body: str, hint: str) -> str:
    if not _is_truncated(result):
        return body
    marker = f"[TRUNCATED: {hint}]"
    if not body:
        return marker
    return f"{body}\n{marker}"


def _render_command(command: str, body: str) -> str:
    return f"{command}\n{body}"


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "`-- " if is_last else "|-- "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '|   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(
            _format_tree_entry(
                child,
                prefix=child_prefix,
                is_last=idx == len(children) - 1,
            )
        )
    return lines


def format_tree(result, root: str = "/", level: int = 2) -> str:
    root_entry = result.root
    if not root_entry.name:
        body = "."
    else:
        lines = [root_entry.name]
        children = list(root_entry.children)
        for idx, child in enumerate(children):
            lines.extend(
                _format_tree_entry(child, is_last=idx == len(children) - 1)
            )
        body = "\n".join(lines)

    body = _mark_truncated(
        result,
        body,
        "tree output hit a limit; use a narrower root or call ws.find/ws.search",
    )
    level_arg = f" -L {level}" if level > 0 else ""
    return _render_command(f"tree{level_arg} {root or '/'}", body)


def format_list(result, path: str = "/") -> str:
    if not result.entries:
        body = "."
    else:
        body = "\n".join(
            f"{entry.name}/" if entry.kind == NodeKind.NODE_KIND_DIR else entry.name
            for entry in result.entries
        )
    return _render_command(f"ls {path}", body)


def format_read(
    result,
    path: str,
    number: bool = False,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    if start_line > 0 or end_line > 0:
        start = start_line if start_line > 0 else 1
        end = end_line if end_line > 0 else "$"
        command = f"sed -n '{start},{end}p' {path}"
    elif number:
        command = f"cat -n {path}"
    else:
        command = f"cat {path}"
    body = _mark_truncated(
        result,
        result.content,
        "file output hit a limit; use start_line/end_line to read a smaller range",
    )
    return _render_command(command, body)


def format_search(result, root: str = "/", pattern: str = "") -> str:
    root_q = shlex.quote(root or "/")
    pattern_q = shlex.quote(pattern)
    body = "\n".join(
        f"{match.path}:{match.line}:{match.line_text}" for match in result.matches
    )
    body = _mark_truncated(
        result,
        body,
        "search hit limit reached; narrow pattern/root or raise limit",
    )
    return _render_command(f"rg -n --no-heading -e {pattern_q} {root_q}", body)


def format_exec(result, path: str, args: list[str] | None = None, stdin: str = "") -> str:
    path_q = shlex.quote(path)
    args_q = " ".join(shlex.quote(arg) for arg in (args or []))
    command = f"{path_q} {args_q}".strip()
    if stdin:
        label = "SQL" if path == "/bin/sql" else "STDIN"
        command = f"{command} <<'{label}'\n{stdin.rstrip()}\n{label}"

    body_parts: list[str] = []
    if result.stdout:
        body_parts.append(result.stdout.rstrip())
    if result.stderr:
        body_parts.append(f"stderr:\n{result.stderr.rstrip()}")
    if getattr(result, "exit_code", 0):
        body_parts.append(f"[exit {result.exit_code}]")
    body = "\n".join(body_parts) if body_parts else "."
    body = _mark_truncated(
        result, body, "exec output hit a limit; narrow args/stdin or read a specific file"
    )
    return _render_command(command, body)
