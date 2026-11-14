import pytest

from pullrot.refs import BadRef, parse, try_parse


@pytest.mark.parametrize("text,registry,repo,tag", [
    ("nginx", "docker.io", "library/nginx", None),
    ("nginx:1.27", "docker.io", "library/nginx", "1.27"),
    ("bitnami/redis:7.2.5", "docker.io", "bitnami/redis", "7.2.5"),
    ("docker.io/bitnami/redis:7.2.5", "docker.io", "bitnami/redis", "7.2.5"),
    ("index.docker.io/library/redis", "docker.io", "library/redis", None),
    ("ghcr.io/astral-sh/uv:0.5", "ghcr.io", "astral-sh/uv", "0.5"),
    ("localhost:5000/app:dev", "localhost:5000", "app", "dev"),
    ("registry.example.com:443/team/app", "registry.example.com:443", "team/app", None),
    ("k8s.gcr.io/pause:3.9", "k8s.gcr.io", "pause", "3.9"),
])
def test_parse(text, registry, repo, tag):
    r = parse(text)
    assert (r.registry, r.repository, r.tag) == (registry, repo, tag)


def test_digest_and_short_forms():
    d = "sha256:" + "0" * 64
    r = parse(f"nginx:1.27@{d}")
    assert r.digest == d and not r.floating
    assert r.short() == f"nginx:1.27@{d}"
    assert parse("docker.io/library/nginx").short() == "nginx"
    assert parse("nginx").canonical() == "docker.io/library/nginx:latest"
    assert parse("nginx").floating and parse("nginx:latest").floating


def test_official_and_namespace():
    assert parse("redis").is_official
    assert not parse("bitnami/redis").is_official
    assert parse("bitnami/redis").namespace == "bitnami"
    assert parse("quay.io/prometheus/node-exporter").name == "node-exporter"


@pytest.mark.parametrize("bad", ["", "a b", "${IMAGE}", "{{ .Values.image }}", "Nginx:1", "nginx:@x",
                                 "nginx@sha256:short", "$REGISTRY/app"])
def test_rejects(bad):
    with pytest.raises(BadRef):
        parse(bad)
    assert try_parse(bad) is None
