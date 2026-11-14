"""Rules: which image references will fail (or start failing) to pull, and what to use instead."""
from __future__ import annotations

from dataclasses import dataclass

from .refs import ImageRef
from .scan import Occurrence

SEVERITIES = ("info", "warn", "error")
BITNAMI_LINK = "https://github.com/bitnami/charts/issues/35164"
K8S_LINK = "https://kubernetes.io/blog/2023/03/10/image-registry-redirect/"
HUB_LIMITS_LINK = "https://docs.docker.com/docker-hub/usage/pulls/"
MIRRORS = {
    "ecr": "public.ecr.aws/docker/library/",
    "gcr": "mirror.gcr.io/library/",
}

# Docker official images that Docker marked deprecated (no further updates), with the replacement
# their own Docker Hub page points to.
DEPRECATED_OFFICIAL = {
    "openjdk": "eclipse-temurin (or amazoncorretto, ibm-semeru-runtimes, sapmachine)",
    "java": "eclipse-temurin",
    "centos": "quay.io/centos/centos:stream9, rockylinux/rockylinux or almalinux",
    "jenkins": "jenkins/jenkins",
    "django": "python (pip install django)",
    "rails": "ruby (gem install rails)",
    "iojs": "node",
    "sentry": "getsentry/self-hosted",
    "owncloud": "owncloud/server",
    "celery": "python (pip install celery)",
}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    occ: Occurrence
    suggestion: str | None = None     # replacement reference, if a mechanical one exists
    link: str = ""

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "severity": self.severity, "file": self.occ.path, "line": self.occ.line,
            "image": self.occ.ref.short(), "kind": self.occ.kind, "message": self.message,
            "suggestion": self.suggestion, "link": self.link,
        }


def bitnami_legacy(ref: ImageRef) -> str:
    return ref.original.replace("bitnami/", "bitnamilegacy/", 1)


def k8s_registry(ref: ImageRef) -> str:
    return ref.original.replace("k8s.gcr.io", "registry.k8s.io", 1)


def mirrored(ref: ImageRef, mirror: str = "ecr") -> str | None:
    if not ref.is_official:
        return None
    suffix = ""
    if ref.tag:
        suffix += ":" + ref.tag
    if ref.digest:
        suffix += "@" + ref.digest
    return MIRRORS[mirror] + ref.name + suffix


def evaluate(occ: Occurrence, mirror: str = "ecr") -> list[Finding]:
    ref = occ.ref
    out: list[Finding] = []
    if ref.is_docker_hub and ref.namespace == "bitnami":
        if ref.effective_tag != "latest" or ref.digest:
            out.append(Finding(
                "PR101", "error",
                "versioned tags were removed from docker.io/bitnami when Bitnami moved its free catalog to "
                "bitnamilegacy (2025-08-28). Stopgap: bitnamilegacy (frozen, no security fixes); "
                "real fix: the upstream image.",
                occ, bitnami_legacy(ref), BITNAMI_LINK))
        else:
            out.append(Finding(
                "PR101", "warn",
                "since 2025-08-28 docker.io/bitnami only publishes a limited set of hardened images, on ':latest' "
                "only. It may disappear or change under you; pin a maintained image.",
                occ, None, BITNAMI_LINK))
    elif ref.is_docker_hub and ref.namespace == "bitnamilegacy":
        out.append(Finding(
            "PR102", "info",
            f"bitnamilegacy/{ref.name} is a frozen archive: it gets no updates or security fixes. "
            f"Plan a move to the upstream image.", occ, None, BITNAMI_LINK))
    if ref.registry == "k8s.gcr.io":
        out.append(Finding(
            "PR201", "warn",
            "k8s.gcr.io is frozen (nothing new since April 2023) and only redirects to registry.k8s.io; "
            "newer tags aren't there and the redirect breaks behind egress allow-lists.",
            occ, k8s_registry(ref), K8S_LINK))
    if ref.is_official and ref.name in DEPRECATED_OFFICIAL:
        out.append(Finding(
            "PR402", "warn",
            f"the '{ref.name}' official image is deprecated (no updates). Use {DEPRECATED_OFFICIAL[ref.name]}.",
            occ, None, f"https://hub.docker.com/_/{ref.name}"))
    if ref.is_docker_hub and occ.ci and not occ.ci_login:
        alt = mirrored(ref, mirror)
        out.append(Finding(
            "PR301", "warn",
            "anonymous Docker Hub pull in CI: shared runner IPs hit the pull rate limit ('toomanyrequests'). "
            + ("Use a mirror or log in." if alt else "Log in (docker/login-action) or pull through a mirror/cache."),
            occ, alt, HUB_LIMITS_LINK))
    if ref.floating and ref.namespace != "bitnami":
        out.append(Finding(
            "PR401", "info",
            "no tag / ':latest': what you pull changes without a diff. Pin a version or digest.",
            occ))
    return out


def evaluate_all(occs: list[Occurrence], mirror: str = "ecr") -> list[Finding]:
    out: list[Finding] = []
    for o in occs:
        out.extend(evaluate(o, mirror))
    return out


def online(findings: list[Finding], occs: list[Occurrence], checker, cache: dict | None = None) -> list[Finding]:
    """Confirm references against the registry. `checker(ref) -> registry.Check`."""
    cache = {} if cache is None else cache
    by_occ: dict[int, list[Finding]] = {}
    for f in findings:
        by_occ.setdefault(id(f.occ), []).append(f)
    extra: list[Finding] = []
    for o in occs:
        key = o.ref.canonical()
        if key not in cache:
            cache[key] = checker(o.ref)
        c = cache[key]
        mine = by_occ.get(id(o), [])
        if c.status in ("missing-tag", "missing-repo"):
            known = [f for f in mine if f.rule in ("PR101", "PR201", "PR402")]
            if known:
                known[0].severity = "error"
                known[0].message += f" Confirmed by the registry: {c.detail}."
                continue
            extra.append(Finding("PR501", "error", f"{o.ref.short()} can't be pulled: {c.detail}.", o))
        elif c.status == "rate-limited":
            extra.append(Finding("PR502", "warn", f"registry rate-limited the check for {o.ref.short()}.", o))
        elif c.status == "error":
            extra.append(Finding("PR503", "info", f"couldn't check {o.ref.short()}: {c.detail}", o))
    return findings + extra
