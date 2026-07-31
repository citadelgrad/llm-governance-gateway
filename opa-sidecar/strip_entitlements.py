"""Strip the `entitlements := {...}` literal from authz.rego for the sidecar image.

Mirrors, in reverse, governance/app/entitlements.py's brace-matching
extraction. The OPA Sidecar polls a bundle that activates base data at
data.mcp.authz.entitlements (see governance/app/entitlements.py); OPA
rejects a rule and bundle base data coexisting at the same path
(rego_compile_error: "conflicting rule for data path ... found"). Removing
the literal here, at image build time, avoids that conflict, but the bare
`entitlements[role]` references in the two `allow` rules still need an
explicit `import data.mcp.authz.entitlements` — Rego does not auto-resolve
an unqualified name to base data just because no local rule defines it
(rego_unsafe_var_error otherwise). This script adds that import. The
canonical policies/mcp/authz.rego on disk is never modified.
"""

from __future__ import annotations

import re
import sys

ENTITLEMENTS_MARKER = re.compile(r"entitlements\s*:=\s*")
IMPORT_STATEMENT = "import data.mcp.authz.entitlements\n"
LAST_IMPORT_LINE = re.compile(r"^import .*$", re.MULTILINE)


def _find_block_end(text: str, start: int) -> int:
    depth = 0
    in_string = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if ch == "\\":
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced braces in entitlements value")


def _inject_entitlements_import(rego_text: str) -> str:
    imports = list(LAST_IMPORT_LINE.finditer(rego_text))
    if imports:
        insert_at = imports[-1].end()
        return rego_text[:insert_at] + "\n" + IMPORT_STATEMENT.rstrip("\n") + rego_text[insert_at:]

    package_match = re.search(r"^package .*$", rego_text, re.MULTILINE)
    if not package_match:
        raise ValueError("no 'package' declaration found")
    insert_at = package_match.end()
    return rego_text[:insert_at] + "\n\n" + IMPORT_STATEMENT.rstrip("\n") + rego_text[insert_at:]


def strip_entitlements(rego_text: str) -> str:
    match = ENTITLEMENTS_MARKER.search(rego_text)
    if not match:
        raise ValueError("no 'entitlements :=' assignment found")

    start = rego_text.find("{", match.end())
    if start == -1:
        raise ValueError("no opening '{' found after 'entitlements :='")

    end = _find_block_end(rego_text, start)
    stripped = rego_text[: match.start()] + rego_text[end:]
    return _inject_entitlements_import(stripped)


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: strip_entitlements.py <input.rego> <output.rego>", file=sys.stderr)
        raise SystemExit(2)

    src_path, dst_path = sys.argv[1], sys.argv[2]
    with open(src_path, encoding="utf-8") as f:
        text = f.read()

    stripped = strip_entitlements(text)

    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(stripped)


if __name__ == "__main__":
    main()
