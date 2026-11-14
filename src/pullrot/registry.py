"""Anonymous (or token) checks against OCI / Docker Registry v2 APIs. HEAD requests only:
manifest HEADs don't count against Docker Hub's pull rate limit."""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .refs import ImageRef

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.v1+prettyjws",
])
UA = "pullrot/0.1 (+https://github.com/hamarshehmhmd/pullrot)"
HOSTS = {"docker.io": "https://registry-1.docker.io"}


def _overrides() -> dict[str, str]:
    """PULLROT_REGISTRY_MAP="docker.io=http://127.0.0.1:5000,ghcr.io=http://..." (tests, mirrors)."""
    out = {}
    for part in os.environ.get("PULLROT_REGISTRY_MAP", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip().rstrip("/")
    return out


def endpoint(registry: str) -> str:
    ov = _overrides()
    if registry in ov:
        return ov[registry]
    return HOSTS.get(registry, "https://" + registry)


@dataclass
class Check:
    status: str          # ok | missing-tag | missing-repo | rate-limited | error
    detail: str = ""
    digest: str = ""


def _request(url: str, method: str = "HEAD", headers: dict | None = None, timeout: float = 15.0):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read() if method == "GET" else b""
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body
    except urllib.error.HTTPError as e:
        body = e.read() if method == "GET" else b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body


def _parse_challenge(h: str) -> dict[str, str]:
    if not h or not h.lower().startswith("bearer"):
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', h))


def _basic_for(registry: str) -> str | None:
    if registry == "docker.io":
        user = os.environ.get("DOCKERHUB_USERNAME")
        tok = os.environ.get("DOCKERHUB_TOKEN")
        if user and tok:
            return "Basic " + base64.b64encode(f"{user}:{tok}".encode()).decode()
    return None


_TOKENS: dict[tuple[str, str], str | None] = {}


def _token(registry: str, challenge: dict[str, str], repo: str, timeout: float) -> str | None:
    realm = challenge.get("realm")
    if not realm:
        return None
    key = (realm, repo)
    if key in _TOKENS:
        return _TOKENS[key]
    q = {"scope": f"repository:{repo}:pull"}
    if challenge.get("service"):
        q["service"] = challenge["service"]
    headers = {}
    basic = _basic_for(registry)
    if basic:
        headers["Authorization"] = basic
    status, _, body = _request(realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(q),
                               "GET", headers, timeout)
    tok = None
    if status == 200:
        try:
            data = json.loads(body)
            tok = data.get("token") or data.get("access_token")
        except ValueError:
            tok = None
    _TOKENS[key] = tok
    return tok


def _authed(registry: str, path: str, method: str, accept: str | None, timeout: float):
    base = endpoint(registry)
    headers = {"Accept": accept} if accept else {}
    status, hdrs, body = _request(base + path, method, headers, timeout)
    if status == 401:
        ch = _parse_challenge(hdrs.get("www-authenticate", ""))
        repo = path.split("/v2/", 1)[1].rsplit("/", 2)[0]
        tok = _token(registry, ch, repo, timeout)
        if tok:
            headers["Authorization"] = "Bearer " + tok
            status, hdrs, body = _request(base + path, method, headers, timeout)
    return status, hdrs, body


def check(ref: ImageRef, timeout: float = 15.0) -> Check:
    target = ref.digest or ref.effective_tag
    try:
        status, hdrs, _ = _authed(ref.registry, f"/v2/{ref.repository}/manifests/{target}", "HEAD", ACCEPT, timeout)
        if status == 200:
            return Check("ok", digest=hdrs.get("docker-content-digest", ""))
        if status == 429:
            return Check("rate-limited", "registry returned 429 Too Many Requests")
        if status == 404:
            what = "digest" if ref.digest else "tag"
            if ref.registry == "docker.io" and "docker.io" not in _overrides():
                hs, _, _ = _request(f"https://hub.docker.com/v2/repositories/{ref.repository}/", "GET", {}, timeout)
                if hs == 404:
                    return Check("missing-repo", "repository not found")
                if hs == 200:
                    return Check("missing-tag", f"repository exists but {what} '{target}' does not")
            return Check("missing-tag", f"{what} '{target}' not found (HTTP 404)")
        if status in (401, 403):
            return Check("missing-repo", "repository not found, or private (no anonymous pull access)")
        return Check("error", f"HTTP {status}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return Check("error", str(getattr(e, "reason", e)))


@dataclass
class RateLimit:
    limit: int | None
    remaining: int | None
    window_s: int | None
    source: str
    authenticated: bool


def _rl(v: str | None) -> tuple[int | None, int | None]:
    if not v:
        return None, None
    m = re.match(r"\s*(\d+)(?:;w=(\d+))?", v)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else (None, None)


def docker_hub_ratelimit(timeout: float = 15.0) -> RateLimit:
    """Docker's documented way to read your current pull limit: HEAD ratelimitpreview/test (doesn't use a pull)."""
    status, hdrs, _ = _authed("docker.io", "/v2/ratelimitpreview/test/manifests/latest", "HEAD", ACCEPT, timeout)
    low = hdrs
    if status not in (200, 429):
        raise OSError(f"Docker Hub returned HTTP {status}")
    limit, win = _rl(low.get("ratelimit-limit"))
    remaining, _ = _rl(low.get("ratelimit-remaining"))
    return RateLimit(limit, remaining, win, low.get("docker-ratelimit-source", ""), bool(_basic_for("docker.io")))
