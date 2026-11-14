"""Find container image references in a repository, with file and line."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .refs import ImageRef, try_parse, build

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".terraform", "vendor"}
MAX_BYTES = 2_000_000

CI_MARKERS = (".github/workflows/", ".gitlab-ci", ".circleci/", "bitbucket-pipelines", "azure-pipelines",
              ".drone.yml", ".woodpecker", ".buildkite/", ".gitea/workflows/", ".forgejo/workflows/")
LOGIN_RE = re.compile(r"docker/login-action|docker\s+login\b|DOCKERHUB_(USERNAME|TOKEN|PASSWORD)|"
                      r"DOCKER_(USERNAME|PASSWORD|AUTH_CONFIG)|credentials:\s*$", re.I | re.M)


@dataclass
class Span:
    """Where a piece of the reference sits in the file (1-based line, 0-based columns)."""
    line: int
    start: int
    end: int
    role: str = "ref"   # ref | registry | repository | tag


@dataclass
class Occurrence:
    path: str
    line: int
    ref: ImageRef
    kind: str                 # dockerfile, compose, kubernetes, workflow, gitlab, helm, devcontainer, yaml
    spans: list[Span] = field(default_factory=list)
    ci: bool = False
    ci_login: bool = False

    def location(self) -> str:
        return f"{self.path}:{self.line}"


def is_ci_path(path: str) -> bool:
    p = path.replace(os.sep, "/")
    return any(m in p for m in CI_MARKERS)


def classify(path: str) -> str | None:
    base = os.path.basename(path)
    low = base.lower()
    p = path.replace(os.sep, "/")
    if low.startswith(("dockerfile", "containerfile")) or low.endswith((".dockerfile", ".containerfile")):
        return "dockerfile"
    if low == "devcontainer.json" or low == ".devcontainer.json" or ("/.devcontainer/" in "/" + p and low.endswith(".json")):
        return "devcontainer"
    if low.endswith((".yml", ".yaml")):
        if "/.github/workflows/" in "/" + p or "/.gitea/workflows/" in "/" + p or "/.forgejo/workflows/" in "/" + p:
            return "workflow"
        if low.startswith(".gitlab-ci"):
            return "gitlab"
        if low.startswith(("docker-compose", "compose")):
            return "compose"
        return "yaml"
    return None


def iter_files(paths: list[str]):
    for root in paths:
        if os.path.isfile(root):
            if classify(root):
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if classify(full):
                    yield full


def scan_paths(paths: list[str], base: str | None = None) -> list[Occurrence]:
    out: list[Occurrence] = []
    for f in iter_files(paths):
        try:
            if os.path.getsize(f) > MAX_BYTES:
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(f, base) if base else f
        out.extend(scan_text(text, rel))
    return out


def scan_text(text: str, path: str) -> list[Occurrence]:
    kind = classify(path) or "yaml"
    if kind == "dockerfile":
        occs = scan_dockerfile(text, path)
    elif kind == "devcontainer":
        occs = scan_json(text, path)
    else:
        occs = scan_yaml(text, path, kind)
    if is_ci_path(path):
        login = bool(LOGIN_RE.search(text))
        for o in occs:
            o.ci, o.ci_login = True, login
    return occs


# ---------------------------------------------------------------- Dockerfile

_ARG_RE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S*))?", re.I)
_FROM_RE = re.compile(r"^(\s*FROM\s+(?:--\S+\s+)*)(\S+)(?:\s+AS\s+(\S+))?", re.I)
_COPY_FROM_RE = re.compile(r"^(\s*(?:COPY|ADD|RUN)\s+.*?--(?:from|mount=[^\s]*from)=)([^\s,]+)", re.I)
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-+]([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _subst(value: str, args: dict[str, str]) -> str | None:
    unresolved = False

    def rep(m):
        nonlocal unresolved
        name = m.group(1) or m.group(3)
        if name in args and args[name] != "":
            return args[name]
        if m.group(2) is not None:
            return m.group(2)
        unresolved = True
        return m.group(0)
    v = _VAR_RE.sub(rep, value.strip("'\""))
    return None if unresolved else v


def scan_dockerfile(text: str, path: str) -> list[Occurrence]:
    occs: list[Occurrence] = []
    args: dict[str, str] = {}
    stages: set[str] = set()
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = _ARG_RE.match(line)
        if m:
            if m.group(2) is not None:
                args[m.group(1)] = m.group(2).strip("'\"")
            continue
        m = _FROM_RE.match(line)
        if m:
            raw = m.group(2)
            if m.group(3):
                stages.add(m.group(3).lower())
            if raw.lower() in stages or raw.lower() == "scratch":
                continue
            value = _subst(raw, args)
            ref = try_parse(value) if value else None
            if ref:
                spans = [Span(i, m.start(2), m.end(2))] if value == raw else []
                occs.append(Occurrence(path, i, ref, "dockerfile", spans))
            continue
        m = _COPY_FROM_RE.match(line)
        if m:
            raw = m.group(2)
            if raw.lower() in stages or raw.isdigit() or not any(c in raw for c in "/:."):
                continue
            value = _subst(raw, args)
            ref = try_parse(value) if value else None
            if ref:
                spans = [Span(i, m.start(2), m.end(2))] if value == raw else []
                occs.append(Occurrence(path, i, ref, "dockerfile", spans))
    return occs


# ---------------------------------------------------------------- JSON (devcontainer)

_JSON_IMAGE_RE = re.compile(r'"image"\s*:\s*"([^"]+)"')


def scan_json(text: str, path: str) -> list[Occurrence]:
    occs = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("//"):
            continue
        for m in _JSON_IMAGE_RE.finditer(line):
            ref = try_parse(m.group(1))
            if ref:
                occs.append(Occurrence(path, i, ref, "devcontainer", [Span(i, m.start(1), m.end(1))]))
    return occs


# ---------------------------------------------------------------- YAML (line based, no dependency)

_KV_RE = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z_][\w.-]*)\s*:(?:\s+(.*?))?\s*$")
_USES_DOCKER_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?docker://([^\s'\"#]+)")


def _value_span(line: str, value: str) -> tuple[int, int, str]:
    """Columns of a YAML scalar inside a line, without quotes or trailing comment."""
    v = value
    q = v[:1] if v[:1] in "'\"" else ""
    if q:
        close = v.find(q, 1)
        inner = v[1:close] if close > 0 else v[1:]
    else:
        inner = v.split(" #", 1)[0].rstrip()
    start = line.find(inner, line.find(":") + 1)
    return start, start + len(inner), inner


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _key_indent(line: str) -> int:
    s = line.lstrip(" ")
    ind = len(line) - len(s)
    if s.startswith("- "):
        ind += 2 + (len(s[2:]) - len(s[2:].lstrip(" ")))
    return ind


def _yaml_kind(kind: str, text: str) -> str:
    if kind != "yaml":
        return kind
    if re.search(r"^\s*apiVersion:\s*\S+", text, re.M) and re.search(r"^\s*kind:\s*\S+", text, re.M):
        return "kubernetes"
    if re.search(r"^services:\s*$", text, re.M):
        return "compose"
    return "helm" if re.search(r"^\s*repository:\s*\S", text, re.M) else "yaml"


def scan_yaml(text: str, path: str, kind: str) -> list[Occurrence]:
    kind = _yaml_kind(kind, text)
    lines = text.splitlines()
    occs: list[Occurrence] = []
    used: set[int] = set()
    for i, line in enumerate(lines):
        st = line.lstrip()
        if not st or st.startswith("#"):
            continue
        m = _USES_DOCKER_RE.match(line)
        if m:
            ref = try_parse(m.group(1))
            if ref:
                occs.append(Occurrence(path, i + 1, ref, kind, [Span(i + 1, m.start(1), m.end(1))]))
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(2), (m.group(3) or "")
        if value.startswith("#"):
            value = ""
        if key in ("image", "container") and value and not value.startswith(("{", "[", "|", ">", "&", "*")):
            if key == "container" and kind != "workflow":
                continue
            s, e, inner = _value_span(line, value)
            ref = try_parse(inner)
            if ref and s >= 0:
                occs.append(Occurrence(path, i + 1, ref, kind, [Span(i + 1, s, e)]))
            continue
        if key == "image" and not value:
            # gitlab: image:\n  name: foo   |  helm: image:\n  registry/repository/tag
            child = _children(lines, i)
            if "name" in child and "repository" not in child:
                ln, s, e, inner = child["name"]
                ref = try_parse(inner)
                if ref:
                    occs.append(Occurrence(path, ln + 1, ref, kind, [Span(ln + 1, s, e)]))
                    used.add(ln)
            continue
        if key == "repository" and value and i not in used:
            occ = _helm_block(lines, i, path)
            if occ:
                occs.append(occ)
    return occs


def _children(lines: list[str], i: int) -> dict[str, tuple[int, int, int, str]]:
    base = _key_indent(lines[i])
    out: dict[str, tuple[int, int, int, str]] = {}
    child_ind = None
    for j in range(i + 1, len(lines)):
        ln = lines[j]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ind = _key_indent(ln)
        if ind <= base:
            break
        if child_ind is None:
            child_ind = ind
        if ind != child_ind:
            continue
        m = _KV_RE.match(ln)
        if m and m.group(3) and not m.group(3).startswith("#"):
            s, e, inner = _value_span(ln, m.group(3))
            out[m.group(2)] = (j, s, e, inner)
    return out


def _siblings(lines: list[str], i: int) -> tuple[dict[str, tuple[int, int, int, str]], str]:
    ind = _key_indent(lines[i])
    lo = i
    while lo > 0:
        prev = lines[lo - 1]
        if prev.strip() and not prev.lstrip().startswith("#") and _key_indent(prev) < ind:
            break
        lo -= 1
    parent = ""
    if lo > 0:
        pm = _KV_RE.match(lines[lo - 1])
        parent = pm.group(2) if pm else ""
    out: dict[str, tuple[int, int, int, str]] = {}
    for j in range(lo, len(lines)):
        ln = lines[j]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        k = _key_indent(ln)
        if j > i and k < ind:
            break
        if k != ind:
            continue
        m = _KV_RE.match(ln)
        if m and m.group(3) and not m.group(3).startswith("#"):
            s, e, inner = _value_span(ln, m.group(3))
            out.setdefault(m.group(2), (j, s, e, inner))
    return out, parent


def _helm_block(lines: list[str], i: int, path: str) -> Occurrence | None:
    sib, parent = _siblings(lines, i)
    if "repository" not in sib or sib["repository"][0] != i:
        return None
    if not ("tag" in sib or "registry" in sib or "digest" in sib or "image" in parent.lower()):
        return None
    repo = sib["repository"][3]
    if "://" in repo or repo.startswith(("oci:", "http")):
        return None   # a chart repository, not an image
    reg = sib.get("registry", (0, 0, 0, ""))[3] or None
    tag = sib.get("tag", (0, 0, 0, ""))[3] or None
    dig = sib.get("digest", (0, 0, 0, ""))[3] or None
    if tag and (tag.startswith(("'", '"')) or tag in ("null", "~")):
        tag = None
    if dig in ("null", "~", "''", '""'):
        dig = None
    ref = try_parse(build(reg, repo, tag, dig))
    if not ref:
        return None
    spans = [Span(i + 1, sib["repository"][1], sib["repository"][2], "repository")]
    for role in ("registry", "tag"):
        if role in sib:
            j, s, e, _ = sib[role]
            spans.append(Span(j + 1, s, e, role))
    return Occurrence(path, i + 1, ref, "helm", spans)
