"""Parse and normalise container image references (docker/distribution rules)."""
from __future__ import annotations

import re
from dataclasses import dataclass

DOCKER_HUB = "docker.io"
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
_TAG_RE = re.compile(r"^[\w][\w.-]{0,127}$")
_REPO_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$")
_DIGEST_RE = re.compile(r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[a-fA-F0-9]{32,}$")


@dataclass(frozen=True)
class ImageRef:
    registry: str          # e.g. docker.io, ghcr.io, registry.k8s.io
    repository: str        # e.g. library/nginx, bitnami/redis
    tag: str | None        # None if no tag given
    digest: str | None     # sha256:... if pinned
    original: str          # the text as written

    @property
    def is_docker_hub(self) -> bool:
        return self.registry == DOCKER_HUB

    @property
    def is_official(self) -> bool:
        return self.is_docker_hub and self.repository.startswith("library/")

    @property
    def namespace(self) -> str:
        return self.repository.split("/", 1)[0] if "/" in self.repository else ""

    @property
    def name(self) -> str:
        return self.repository.rsplit("/", 1)[-1]

    @property
    def effective_tag(self) -> str:
        return self.tag or "latest"

    @property
    def floating(self) -> bool:
        return self.digest is None and self.effective_tag == "latest"

    def short(self) -> str:
        """Familiar form, like the docker CLI prints it."""
        repo = self.repository
        if self.is_docker_hub:
            host = ""
            if repo.startswith("library/"):
                repo = repo[len("library/"):]
        else:
            host = self.registry + "/"
        s = host + repo
        if self.tag:
            s += ":" + self.tag
        if self.digest:
            s += "@" + self.digest
        return s

    def canonical(self) -> str:
        s = f"{self.registry}/{self.repository}:{self.effective_tag}"
        if self.digest:
            s += "@" + self.digest
        return s


class BadRef(ValueError):
    pass


def parse(text: str) -> ImageRef:
    raw = text.strip().strip("'\"")
    if not raw or any(c.isspace() for c in raw):
        raise BadRef(f"not an image reference: {text!r}")
    if "$" in raw or "{{" in raw or "}}" in raw:
        raise BadRef(f"templated reference: {text!r}")
    rest, digest = raw, None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
        if not _DIGEST_RE.match(digest):
            raise BadRef(f"bad digest in {text!r}")
    tag = None
    slash = rest.rfind("/")
    colon = rest.rfind(":")
    if colon > slash:
        rest, tag = rest[:colon], rest[colon + 1:]
        if not _TAG_RE.match(tag):
            raise BadRef(f"bad tag in {text!r}")
    parts = rest.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts[0].lower(), parts[1]
    else:
        registry, repo = DOCKER_HUB, rest
    if registry in _HUB_ALIASES:
        registry = DOCKER_HUB
    if registry == DOCKER_HUB and "/" not in repo:
        repo = "library/" + repo
    if not _REPO_RE.match(repo):
        raise BadRef(f"bad repository in {text!r}")
    return ImageRef(registry, repo, tag, digest, raw)


def try_parse(text: str) -> ImageRef | None:
    try:
        return parse(text)
    except BadRef:
        return None


def build(registry: str | None, repository: str, tag: str | None = None, digest: str | None = None) -> str:
    s = (registry.rstrip("/") + "/" if registry else "") + repository
    if tag:
        s += ":" + tag
    if digest:
        s += "@" + digest
    return s
