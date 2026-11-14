import json

from pullrot import registry
from pullrot.cli import main
from pullrot.refs import parse


def test_check_statuses_against_fake_registry(fake_registry):
    assert registry.check(parse("nginx:1.27")).status == "ok"
    assert registry.check(parse("nginx:1.27")).digest == "sha256:" + "ab" * 32
    c = registry.check(parse("bitnami/redis:7.2.5"))
    assert c.status == "missing-tag" and "7.2.5" in c.detail
    assert registry.check(parse("nosuch/thing:1")).status == "missing-repo"
    assert registry.check(parse("registry.k8s.io/busybox:1.27")).status == "ok"


def test_only_head_requests_for_manifests(fake_registry):
    from conftest import FakeRegistry
    registry.check(parse("redis:7"))
    registry.check(parse("redis:7"))
    manifest_calls = [(m, p) for m, p in FakeRegistry.hits if "/manifests/" in p]
    assert manifest_calls and all(m == "HEAD" for m, _ in manifest_calls)
    # token fetched once and reused
    assert sum(1 for _, p in FakeRegistry.hits if p.startswith("/token")) == 1


def test_rate_limited(fake_registry):
    from conftest import FakeRegistry
    FakeRegistry.ratelimited.add("library/nginx")
    assert registry.check(parse("nginx:1.27")).status == "rate-limited"


def test_network_error(monkeypatch):
    monkeypatch.setenv("PULLROT_REGISTRY_MAP", "docker.io=http://127.0.0.1:9")
    assert registry.check(parse("nginx:1"), timeout=2).status == "error"


def test_ratelimit_cmd(fake_registry, capsys):
    assert main(["ratelimit"]) == 0
    out = capsys.readouterr().out
    assert "73 of 100 left per 6 h" in out and "anonymous" in out and "203.0.113.7" in out
    assert main(["ratelimit", "--min", "80"]) == 1


def test_scan_online_json(fake_registry, shop, capsys, monkeypatch):
    # only docker.io and registry.k8s.io go to the fake; make the rest fail fast
    monkeypatch.setenv("PULLROT_REGISTRY_MAP", f"docker.io={fake_registry},registry.k8s.io={fake_registry},"
                       f"k8s.gcr.io={fake_registry},ghcr.io=http://127.0.0.1:9,quay.io={fake_registry},"
                       f"mcr.microsoft.com={fake_registry},gcr.io={fake_registry}")
    rc = main(["scan", str(shop), "--online", "--format", "json", "--timeout", "3"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["images"] == 17 and data["files"] == 6
    pr101 = [f for f in data["findings"] if f["rule"] == "PR101"]
    assert len(pr101) == 4 and all(f["severity"] == "error" for f in pr101)
    assert any("Confirmed by the registry" in f["message"] for f in pr101)


def test_scan_text_and_fail_on(shop, capsys):
    assert main(["scan", str(shop)]) == 1
    out = capsys.readouterr().out
    assert "17 image references in 6 files" in out and "4 errors" in out
    assert "-> bitnamilegacy/postgresql:16.3.0-debian-12-r14" in out
    assert main(["scan", str(shop), "--fail-on", "never"]) == 0
    capsys.readouterr()
    assert main(["scan", str(shop / "deploy"), "--fail-on", "error"]) == 0
    assert main(["scan", str(shop / "deploy"), "--fail-on", "warn"]) == 1


def test_scan_github_format(shop, capsys, monkeypatch):
    monkeypatch.chdir(shop)
    main(["scan", ".", "--format", "github", "--min-severity", "warn"])
    out = capsys.readouterr().out
    assert "::error file=docker-compose.yml,line=5,title=PR101 bitnami/postgresql:16.3.0-debian-12-r14::" in out
    assert "::warning file=.github/workflows/ci.yml,line=6,title=PR301 node:20::" in out
    assert "::notice" not in out


def test_list_cmd(shop, capsys):
    main(["list", str(shop), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert {"file": "deploy/k8s/app.yaml", "line": 10, "kind": "kubernetes", "image": "k8s.gcr.io/busybox:1.27",
            "canonical": "k8s.gcr.io/busybox:1.27", "ci": False} in data


def test_fix_cmd_verify(fake_registry, shop, capsys):
    rc = main(["fix", str(shop), "--write", "--verify"])
    err = capsys.readouterr().err
    assert rc == 0
    # bitnamilegacy/kubectl:1.30.2 isn't in the fake registry, so it is not rewritten
    assert "not rewriting api/Dockerfile:12" in err
    assert "rewrote 4 image reference(s)" in err
    assert "bitnami/kubectl:1.30.2" in (shop / "api/Dockerfile").read_text()
    assert "bitnamilegacy/postgresql" in (shop / "docker-compose.yml").read_text()


def test_check_cmd(fake_registry, capsys):
    assert main(["check", "nginx:1.27", "bitnamilegacy/redis:7.2.5"]) == 0
    assert main(["check", "bitnami/redis:7.2.5"]) == 1
    out = capsys.readouterr().out
    assert "MISSING bitnami/redis:7.2.5" in out and "-> bitnamilegacy/redis:7.2.5" in out


def test_version(capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "pullrot 0.1.1" in capsys.readouterr().out


def test_github_format_paths_relative_to_cwd(shop, capsys, monkeypatch):
    monkeypatch.chdir(shop.parent)
    main(["scan", "shop/deploy", "--format", "github"])
    assert "::warning file=shop/deploy/k8s/app.yaml,line=10,title=PR201" in capsys.readouterr().out
