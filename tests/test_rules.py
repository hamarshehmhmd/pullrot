from pullrot.registry import Check
from pullrot.rules import evaluate, evaluate_all, mirrored, online
from pullrot.refs import parse
from pullrot.scan import Occurrence, scan_text


def occ(text, path="docker-compose.yml", ci=False, login=False):
    return Occurrence(path, 1, parse(text), "compose", [], ci, login)


def rules(o, **kw):
    return {(f.rule, f.severity) for f in evaluate(o, **kw)}


def test_bitnami_versioned_is_error_with_legacy_suggestion():
    fs = evaluate(occ("docker.io/bitnami/postgresql:16.3.0-debian-12-r14"))
    assert [(f.rule, f.severity) for f in fs] == [("PR101", "error")]
    assert fs[0].suggestion == "docker.io/bitnamilegacy/postgresql:16.3.0-debian-12-r14"
    assert "35164" in fs[0].link


def test_bitnami_latest_is_warning_without_suggestion():
    fs = evaluate(occ("bitnami/redis"))
    assert [(f.rule, f.severity, f.suggestion) for f in fs] == [("PR101", "warn", None)]


def test_bitnamilegacy_is_info():
    assert rules(occ("bitnamilegacy/redis:7.2.5")) == {("PR102", "info")}


def test_lookalikes_not_flagged():
    assert rules(occ("ghcr.io/bitnami/redis:7")) == set()
    assert rules(occ("bitnamisecure/redis:latest")) == {("PR401", "info")}
    assert rules(occ("mybitnami/redis:7")) == set()


def test_k8s_gcr():
    fs = evaluate(occ("k8s.gcr.io/ingress-nginx/controller:v1.2.1"))
    assert fs[0].rule == "PR201" and fs[0].suggestion == "registry.k8s.io/ingress-nginx/controller:v1.2.1"
    assert rules(occ("registry.k8s.io/pause:3.10")) == set()


def test_deprecated_official():
    assert ("PR402", "warn") in rules(occ("openjdk:17-jdk-slim"))
    assert ("PR402", "warn") in rules(occ("docker.io/library/centos:7"))
    assert rules(occ("eclipse-temurin:21")) == set()
    assert rules(occ("someone/openjdk:17")) == set()


def test_ci_anonymous_docker_hub():
    o = occ("postgres:16", ".github/workflows/ci.yml", ci=True)
    fs = [f for f in evaluate(o) if f.rule == "PR301"]
    assert fs[0].suggestion == "public.ecr.aws/docker/library/postgres:16"
    fs = [f for f in evaluate(o, mirror="gcr") if f.rule == "PR301"]
    assert fs[0].suggestion == "mirror.gcr.io/library/postgres:16"
    # non-official images have no generic mirror
    o2 = occ("hadolint/hadolint:v2.12.0", ".gitlab-ci.yml", ci=True)
    assert [f.suggestion for f in evaluate(o2) if f.rule == "PR301"] == [None]


def test_ci_with_login_or_other_registry_not_flagged():
    assert "PR301" not in {r for r, _ in rules(occ("postgres:16", ci=True, login=True))}
    assert "PR301" not in {r for r, _ in rules(occ("ghcr.io/a/b:1", ci=True))}
    assert "PR301" not in {r for r, _ in rules(occ("postgres:16"))}


def test_mirrored_keeps_digest():
    d = "sha256:" + "1" * 64
    assert mirrored(parse(f"node:20@{d}")) == f"public.ecr.aws/docker/library/node:20@{d}"
    assert mirrored(parse("bitnami/redis:7")) is None


def test_floating():
    assert ("PR401", "info") in rules(occ("nginx"))
    assert ("PR401", "info") in rules(occ("nginx:latest"))
    assert "PR401" not in {r for r, _ in rules(occ("nginx@sha256:" + "a" * 64))}


def test_online_merges_confirmation_and_adds_missing():
    occs = scan_text("services:\n  a:\n    image: bitnami/redis:7.2.5\n  b:\n    image: ghcr.io/acme/app:1\n"
                     "  c:\n    image: nginx:1.27\n  d:\n    image: quay.io/x/y:1\n  e:\n    image: gcr.io/a/b:2\n",
                     "compose.yaml")
    status = {"docker.io/bitnami/redis:7.2.5": Check("missing-tag", "tag '7.2.5' not found"),
              "ghcr.io/acme/app:1": Check("missing-repo", "repository not found"),
              "docker.io/library/nginx:1.27": Check("ok"),
              "quay.io/x/y:1": Check("rate-limited", "429"),
              "gcr.io/a/b:2": Check("error", "timed out")}
    fs = online(evaluate_all(occs), occs, lambda r: status[r.canonical()])
    got = sorted((f.rule, f.severity, f.occ.ref.short()) for f in fs)
    assert got == [("PR101", "error", "bitnami/redis:7.2.5"), ("PR501", "error", "ghcr.io/acme/app:1"),
                   ("PR502", "warn", "quay.io/x/y:1"), ("PR503", "info", "gcr.io/a/b:2")]
    assert "Confirmed by the registry" in [f for f in fs if f.rule == "PR101"][0].message


def test_online_checks_each_reference_once():
    occs = scan_text("services:\n  a:\n    image: nginx:1.27\n  b:\n    image: docker.io/library/nginx:1.27\n", "compose.yaml")
    calls = []
    online(evaluate_all(occs), occs, lambda r: calls.append(r) or Check("ok"))
    assert len(calls) == 1
