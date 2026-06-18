#!/usr/bin/env python3
"""Auto-resolve the two known merge-conflict patterns in the dev rebuild.

Usage:
  _resolve_conflicts.py server    <file>   — union-merge server.py catalog blocks
  _resolve_conflicts.py log_tools <file>   — take --ours, graft new code from --theirs
"""

import ast
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_show(ref, path):
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# server.py: union-merge conflict blocks
# ---------------------------------------------------------------------------

def _entry_key(line: str) -> str | None:
    """Extract the function-name key from a catalog tuple or tool-map dict line."""
    s = line.strip()
    m = re.search(r'^\("([\w\-]+)"', s)
    if m:
        return m.group(1)
    m = re.search(r'^"([\w\-]+)"\s*:', s)
    if m:
        return m.group(1)
    return None


def resolve_server_py(file_path: str) -> bool:
    content = Path(file_path).read_text()
    lines = content.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("<<<<<<<"):
            ours, theirs, state = [], [], "ours"
            i += 1
            while i < len(lines):
                l = lines[i]
                if l.startswith("======="):
                    state = "theirs"
                elif l.startswith(">>>>>>>"):
                    break
                elif state == "ours":
                    ours.append(l)
                else:
                    theirs.append(l)
                i += 1
            # Emit ours, then any theirs lines whose key isn't already in ours.
            ours_keys = {k for l in ours if (k := _entry_key(l))}
            result.extend(ours)
            for l in theirs:
                k = _entry_key(l)
                if k is None or k not in ours_keys:
                    result.append(l)
        else:
            result.append(line)
        i += 1

    merged = "".join(result)
    try:
        ast.parse(merged)
    except SyntaxError as e:
        print(f"  server.py: syntax error after merge: {e}", file=sys.stderr)
        return False

    Path(file_path).write_text(merged)
    print(f"  server.py: union-merged OK")
    return True


# ---------------------------------------------------------------------------
# log_tools.py: take --ours, graft new code from --theirs
# ---------------------------------------------------------------------------

def _func_nodes(source: str) -> dict:
    """Return {name: node} for all async/sync functions in source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def _new_module_constants(theirs_content: str, base_content: str) -> str:
    """Return source text for new top-level constant assignments in theirs vs base."""
    try:
        theirs_tree = ast.parse(theirs_content)
        base_tree = ast.parse(base_content)
    except SyntaxError:
        return ""

    base_names: set[str] = set()
    for node in base_tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    base_names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            base_names.add(node.target.id)

    theirs_lines = theirs_content.splitlines()
    blocks: list[str] = []
    for node in theirs_tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not names or any(n in base_names for n in names):
                continue
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in base_names:
                continue
        else:
            continue

        # Grab the node's source lines, plus any comment block immediately above.
        start = node.lineno - 1  # 0-indexed
        comment_start = start
        j = start - 1
        while j >= 0 and theirs_lines[j].strip().startswith("#"):
            comment_start = j
            j -= 1
        blocks.append("\n".join(theirs_lines[comment_start:node.end_lineno]))

    return "\n\n".join(blocks)


def _find_mcp_tool_decorator_line(lines: list[str], func_lineno_1idx: int) -> int:
    """Return the 0-indexed line of the @mcp.tool() decorator before func_lineno."""
    idx = func_lineno_1idx - 1  # convert to 0-indexed
    j = idx - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j >= 0 and "@mcp.tool()" in lines[j]:
        return j
    return idx  # fallback: point at the def itself


def _extract_graft(theirs_content: str, base_content: str, new_funcs: set[str]) -> str | None:
    """Extract content from theirs that contains new_funcs, stopping before get_logfiles_state."""
    theirs_lines = theirs_content.splitlines()
    theirs_nodes = _func_nodes(theirs_content)
    base_nodes = _func_nodes(base_content)

    logfiles_node = theirs_nodes.get("get_logfiles_state")
    if not logfiles_node:
        print("  log_tools.py: get_logfiles_state not found in theirs", file=sys.stderr)
        return None

    # 0-indexed line of @mcp.tool() for get_logfiles_state in theirs
    logfiles_decorator_0idx = _find_mcp_tool_decorator_line(theirs_lines, logfiles_node.lineno)

    # Find where new content starts: the line AFTER the last base function
    # that appears before get_logfiles_state in theirs (0-indexed).
    last_base_end_0idx = 0
    for name, node in theirs_nodes.items():
        if name in base_nodes and name != "get_logfiles_state":
            end_0idx = node.end_lineno  # end_lineno is 1-indexed; as 0-idx it's the NEXT line
            if end_0idx > last_base_end_0idx and (node.lineno - 1) < logfiles_decorator_0idx:
                last_base_end_0idx = end_0idx

    graft_lines = theirs_lines[last_base_end_0idx:logfiles_decorator_0idx]

    # Strip leading/trailing blank lines.
    while graft_lines and not graft_lines[0].strip():
        graft_lines.pop(0)
    while graft_lines and not graft_lines[-1].strip():
        graft_lines.pop()

    if not graft_lines:
        print("  log_tools.py: nothing to graft (empty range)", file=sys.stderr)
        return None

    return "\n".join(graft_lines)


def _insert_graft(ours_content: str, graft: str) -> str:
    """Insert graft into ours immediately before @mcp.tool() + get_logfiles_state."""
    ours_lines = ours_content.splitlines()

    # Find the @mcp.tool() decorator for get_logfiles_state in ours.
    decorator_0idx = None
    for i, line in enumerate(ours_lines):
        if "async def get_logfiles_state" in line:
            decorator_0idx = _find_mcp_tool_decorator_line(ours_lines, i + 1)
            break

    if decorator_0idx is None:
        # Fallback: append at end.
        return ours_content.rstrip("\n") + "\n\n\n" + graft + "\n"

    # Find last non-blank line before the decorator.
    content_end = decorator_0idx - 1
    while content_end >= 0 and not ours_lines[content_end].strip():
        content_end -= 1

    before = ours_lines[: content_end + 1]
    after = ours_lines[decorator_0idx:]
    result = before + ["", ""] + graft.splitlines() + ["", ""] + after
    return "\n".join(result) + "\n"


def resolve_log_tools_py(file_path: str) -> bool:
    # Take ours first (discard the conflict markers).
    subprocess.run(["git", "checkout", "--ours", "--", file_path], check=True)

    ours_content = Path(file_path).read_text()
    theirs_content = git_show("MERGE_HEAD", file_path)
    base_content = git_show("upstream/main", file_path)

    if not theirs_content or not base_content:
        print("  log_tools.py: could not retrieve theirs/base from git", file=sys.stderr)
        return False

    ours_funcs = set(_func_nodes(ours_content))
    theirs_funcs = set(_func_nodes(theirs_content))
    base_funcs = set(_func_nodes(base_content))
    new_funcs = theirs_funcs - base_funcs

    if not new_funcs:
        print("  log_tools.py: no new functions detected — keeping ours as-is")
        return True

    print(f"  log_tools.py: grafting {sorted(new_funcs)} from theirs")

    # Check if already present in ours (from a previous merge of a related branch).
    already_present = new_funcs & ours_funcs
    to_graft = new_funcs - ours_funcs
    if already_present:
        print(f"  log_tools.py: already in ours, skipping: {sorted(already_present)}")
    if not to_graft:
        print("  log_tools.py: nothing new to graft — ours already complete")
        return True

    graft = _extract_graft(theirs_content, base_content, to_graft)
    if not graft:
        print("  log_tools.py: graft extraction failed", file=sys.stderr)
        return False

    constants = _new_module_constants(theirs_content, base_content)
    if constants:
        ours_names = {
            t.id
            for node in ast.parse(ours_content).body
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        filtered = []
        for block in constants.split("\n\n"):
            name_match = re.search(r"^([A-Z_][A-Z0-9_]*)\s*=", block, re.MULTILINE)
            if name_match and name_match.group(1) in ours_names:
                print(f"  log_tools.py: constant {name_match.group(1)} already in ours, skipping")
                continue
            filtered.append(block)
        if filtered:
            graft = "\n\n".join(filtered) + "\n\n" + graft

    merged = _insert_graft(ours_content, graft)

    try:
        ast.parse(merged)
    except SyntaxError as e:
        print(f"  log_tools.py: syntax error after graft: {e}", file=sys.stderr)
        return False

    Path(file_path).write_text(merged)
    print("  log_tools.py: graft OK")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <server|log_tools> <file_path>", file=sys.stderr)
        sys.exit(1)

    mode, file_path = sys.argv[1], sys.argv[2]
    ok = False
    if mode == "server":
        ok = resolve_server_py(file_path)
    elif mode == "log_tools":
        ok = resolve_log_tools_py(file_path)
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
