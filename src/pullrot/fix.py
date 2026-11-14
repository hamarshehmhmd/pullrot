"""Rewrite image references in place (or print a diff)."""
from __future__ import annotations

import difflib
import os
from collections import defaultdict

from .refs import ImageRef
from .rules import Finding, bitnami_legacy, k8s_registry, mirrored
from .scan import Occurrence


def plan(findings: list[Finding], bitnami: bool = True, k8s: bool = True, mirror: str | None = None) -> list[tuple[Occurrence, str, str]]:
    """Return (occurrence, rule, new reference) for every mechanical fix the user asked for."""
    out = []
    seen = set()
    for f in findings:
        o = f.occ
        if id(o) in seen:
            continue
        new = None
        if f.rule == "PR101" and bitnami and f.suggestion:
            new = f.suggestion
        elif f.rule == "PR201" and k8s:
            new = k8s_registry(o.ref)
        elif f.rule == "PR301" and mirror:
            new = mirrored(o.ref, mirror)
        if new and new != o.ref.original:
            out.append((o, f.rule, new))
            seen.add(id(o))
    return out


def _apply_to_lines(lines: list[str], occ: Occurrence, new: str) -> bool:
    ref: ImageRef = occ.ref
    spans = {s.role: s for s in occ.spans}
    if "ref" in spans:
        s = spans["ref"]
        line = lines[s.line - 1]
        if line[s.start:s.end] != ref.original:
            return False
        lines[s.line - 1] = line[:s.start] + new + line[s.end:]
        return True
    if "repository" in spans and ref.namespace == "bitnami" and new.replace("bitnamilegacy/", "bitnami/", 1) == ref.original:
        s = spans["repository"]
        line = lines[s.line - 1]
        cur = line[s.start:s.end]
        if not cur.startswith("bitnami/"):
            return False
        lines[s.line - 1] = line[:s.start] + "bitnamilegacy/" + cur[len("bitnami/"):] + line[s.end:]
        return True
    return False


def apply(changes: list[tuple[Occurrence, str, str]], root: str = ".", write: bool = False) -> tuple[str, int, list[Occurrence]]:
    """Returns (unified diff, number of references changed, skipped occurrences)."""
    by_file: dict[str, list] = defaultdict(list)
    for occ, rule, new in changes:
        by_file[occ.path].append((occ, new))
    diff, n, skipped = [], 0, []
    for path, items in sorted(by_file.items()):
        full = path if os.path.isabs(path) else os.path.join(root, path)
        with open(full, encoding="utf-8") as fh:
            old = fh.read()
        lines = old.split("\n")
        # right-to-left within a line so earlier columns stay valid
        for occ, new in sorted(items, key=lambda t: (t[0].line, -(t[0].spans[0].start if t[0].spans else 0))):
            if _apply_to_lines(lines, occ, new):
                n += 1
            else:
                skipped.append(occ)
        text = "\n".join(lines)
        if text != old:
            diff.extend(difflib.unified_diff(old.splitlines(True), text.splitlines(True), "a/" + path, "b/" + path))
            if write:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(text)
    return "".join(diff), n, skipped
