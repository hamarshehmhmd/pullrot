"""pullrot command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import __version__, registry
from .fix import apply, plan
from .refs import BadRef, parse
from .rules import SEVERITIES, evaluate_all, online
from .scan import Occurrence, Span, scan_paths

COLORS = {"error": "31", "warn": "33", "info": "36"}


def _c(code: str, s: str) -> str:
    if os.environ.get("NO_COLOR") or not (sys.stdout.isatty() or os.environ.get("FORCE_COLOR")):
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def _parallel_checker(occs, timeout):
    refs = {o.ref.canonical(): o.ref for o in occs}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(zip(refs, ex.map(lambda r: registry.check(r, timeout), refs.values())))
    return results


def _base(paths: list[str]) -> str | None:
    return paths[0] if len(paths) == 1 and os.path.isdir(paths[0]) else None


def cmd_scan(a) -> int:
    base = _base(a.paths)
    occs = scan_paths(a.paths, base)
    findings = evaluate_all(occs, a.mirror)
    if a.online and occs:
        cache = _parallel_checker(occs, a.timeout)
        findings = online(findings, occs, lambda r: cache[r.canonical()], cache)
    findings = [f for f in findings if SEVERITIES.index(f.severity) >= SEVERITIES.index(a.min_severity)]
    findings.sort(key=lambda f: (f.occ.path, f.occ.line, -SEVERITIES.index(f.severity), f.rule))
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    if a.format == "json":
        print(json.dumps({"version": __version__, "images": len(occs),
                          "files": len({o.path for o in occs}), "counts": counts,
                          "findings": [f.to_dict() for f in findings]}, indent=2))
    elif a.format == "github":
        lvl = {"error": "error", "warn": "warning", "info": "notice"}
        for f in findings:
            msg = f.message + (f" Suggested: {f.suggestion}" if f.suggestion else "")
            print(f"::{lvl[f.severity]} file={f.occ.path},line={f.occ.line},title={f.rule} {f.occ.ref.short()}::{msg}")
        print(f"pullrot: {len(occs)} image references, {counts['error']} errors, {counts['warn']} warnings, {counts['info']} notes")
    else:
        last = None
        for f in findings:
            if f.occ.path != last:
                print(_c("1", f.occ.path))
                last = f.occ.path
            sev = _c(COLORS[f.severity], f"{f.severity.upper():5}")
            print(f"  {f.occ.line:>4}  {sev} {f.rule}  {_c('1', f.occ.ref.short())}")
            print(f"        {f.message}")
            if f.suggestion:
                print(f"        {_c('32', '-> ' + f.suggestion)}")
        if findings:
            print()
        print(f"{len(occs)} image references in {len({o.path for o in occs})} files: "
              f"{_c('31', str(counts['error']) + ' errors')}, {_c('33', str(counts['warn']) + ' warnings')}, "
              f"{counts['info']} notes" + ("" if a.online else "  (add --online to check every tag against its registry)"))
    if a.fail_on == "never":
        return 0
    worst = [f for f in findings if SEVERITIES.index(f.severity) >= SEVERITIES.index(a.fail_on)]
    return 1 if worst else 0


def cmd_list(a) -> int:
    base = _base(a.paths)
    occs = scan_paths(a.paths, base)
    if a.format == "json":
        print(json.dumps([{"file": o.path, "line": o.line, "kind": o.kind, "image": o.ref.short(),
                           "canonical": o.ref.canonical(), "ci": o.ci} for o in occs], indent=2))
    else:
        for o in occs:
            print(f"{o.location():40} {o.kind:12} {o.ref.short()}")
    return 0


def cmd_fix(a) -> int:
    base = _base(a.paths) or "."
    occs = scan_paths(a.paths, _base(a.paths))
    findings = evaluate_all(occs, a.mirror or "ecr")
    changes = plan(findings, bitnami=not a.no_bitnami, k8s=not a.no_k8s, mirror=a.mirror)
    root = base if _base(a.paths) else "."
    if a.verify and changes:
        news = sorted({new for _, _, new in changes})
        with ThreadPoolExecutor(max_workers=8) as ex:
            ok = dict(zip(news, ex.map(lambda n: registry.check(parse(n), a.timeout).status == "ok", news)))
        for o, _, new in changes:
            if not ok[new]:
                print(f"not rewriting {o.location()}: {new} doesn't exist on its registry", file=sys.stderr)
        changes = [c for c in changes if ok[c[2]]]
    diff, n, skipped = apply(changes, root, write=a.write)
    if diff:
        sys.stdout.write(diff)
    for o in skipped:
        print(f"skipped {o.location()} ({o.ref.short()}): reference is built from variables, edit by hand", file=sys.stderr)
    verb = "rewrote" if a.write else "would rewrite"
    print(f"pullrot: {verb} {n} image reference(s)" + ("" if a.write or not n else "; run again with --write to apply"),
          file=sys.stderr)
    return 0


def cmd_check(a) -> int:
    bad = 0
    for text in a.images:
        try:
            ref = parse(text)
        except BadRef as e:
            print(f"{text}: {e}")
            bad += 1
            continue
        occ = Occurrence("<arg>", 0, ref, "arg", [Span(0, 0, 0)])
        fs = evaluate_all([occ])
        c = registry.check(ref, a.timeout)
        mark = {"ok": _c("32", "OK     "), "missing-tag": _c("31", "MISSING"), "missing-repo": _c("31", "MISSING"),
                "rate-limited": _c("33", "429    "), "error": _c("33", "ERROR  ")}[c.status]
        print(f"{mark} {ref.short()}" + (f"  ({c.detail})" if c.detail else "") +
              (f"  {c.digest}" if c.digest and a.digest else ""))
        for f in fs:
            if f.rule in ("PR401", "PR301"):
                continue
            print(f"        {f.rule}: {f.message}" + (f" -> {f.suggestion}" if f.suggestion else ""))
        if c.status != "ok":
            bad += 1
    return 1 if bad else 0


def cmd_ratelimit(a) -> int:
    try:
        rl = registry.docker_hub_ratelimit(a.timeout)
    except OSError as e:
        print(f"couldn't read Docker Hub rate limit: {e}", file=sys.stderr)
        return 2
    who = "authenticated (DOCKERHUB_USERNAME)" if rl.authenticated else "anonymous"
    if rl.limit is None:
        print(f"Docker Hub pulls ({who}): no rate limit headers returned (unlimited plan?)")
        return 0
    hours = f"{rl.window_s // 3600} h" if rl.window_s and rl.window_s % 3600 == 0 else f"{rl.window_s} s"
    print(f"Docker Hub pulls ({who}) from {rl.source or 'this IP'}: {rl.remaining} of {rl.limit} left per {hours}")
    if a.min is not None and rl.remaining is not None and rl.remaining < a.min:
        print(f"below --min {a.min}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pullrot", description="Find container image references that will fail to pull: "
                                "removed Bitnami tags, frozen registries, deprecated images, anonymous Docker Hub pulls in CI, "
                                "and tags that no longer exist.")
    p.add_argument("--version", action="version", version=f"pullrot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan Dockerfiles, compose, Kubernetes, Helm values, CI configs, devcontainers")
    s.add_argument("paths", nargs="*", default=["."])
    s.add_argument("--online", action="store_true", help="HEAD every image against its registry (no pulls used)")
    s.add_argument("--format", choices=["text", "json", "github"], default="text")
    s.add_argument("--fail-on", choices=["error", "warn", "info", "never"], default="error")
    s.add_argument("--min-severity", choices=list(SEVERITIES), default="info")
    s.add_argument("--mirror", choices=["ecr", "gcr"], default="ecr", help="mirror to suggest for Docker Hub official images")
    s.add_argument("--timeout", type=float, default=15.0)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("list", help="list every image reference found")
    s.add_argument("paths", nargs="*", default=["."])
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("fix", help="rewrite references: bitnami -> bitnamilegacy, k8s.gcr.io -> registry.k8s.io, optional CI mirror")
    s.add_argument("paths", nargs="*", default=["."])
    s.add_argument("--write", action="store_true", help="apply the changes (default: print a diff)")
    s.add_argument("--mirror", choices=["ecr", "gcr"], help="also point anonymous Docker Hub pulls in CI at a mirror")
    s.add_argument("--no-bitnami", action="store_true")
    s.add_argument("--no-k8s", action="store_true")
    s.add_argument("--verify", action="store_true", help="only rewrite to references that exist on their registry")
    s.add_argument("--timeout", type=float, default=15.0)
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("check", help="check that image references exist (HEAD, no pull used)")
    s.add_argument("images", nargs="+")
    s.add_argument("--digest", action="store_true", help="print the digest of images that exist")
    s.add_argument("--timeout", type=float, default=15.0)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("ratelimit", help="show your remaining Docker Hub pulls (doesn't use one)")
    s.add_argument("--min", type=int, help="exit 1 if fewer pulls than this are left")
    s.add_argument("--timeout", type=float, default=15.0)
    s.set_defaults(func=cmd_ratelimit)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
