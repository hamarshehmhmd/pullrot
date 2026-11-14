from pullrot.fix import apply, plan
from pullrot.rules import evaluate_all
from pullrot.scan import scan_paths


def run(shop, **kw):
    occs = scan_paths([str(shop)], str(shop))
    return apply(plan(evaluate_all(occs, kw.get("mirror") or "ecr"), mirror=kw.get("mirror")), str(shop),
                 write=kw.get("write", False))


def test_dry_run_changes_nothing(shop):
    before = (shop / "docker-compose.yml").read_text()
    diff, n, skipped = run(shop)
    assert n == 5 and not skipped
    assert "+    image: bitnamilegacy/postgresql:16.3.0-debian-12-r14" in diff
    assert '+    image: "docker.io/bitnamilegacy/redis:7.2.5"' in diff
    assert "+          image: registry.k8s.io/busybox:1.27" in diff
    assert "+COPY --from=bitnamilegacy/kubectl:1.30.2 /opt/bitnami/kubectl/bin/kubectl" in diff
    assert "+    repository: bitnamilegacy/redis" in diff
    assert "public.ecr.aws" not in diff          # mirrors are opt-in
    assert (shop / "docker-compose.yml").read_text() == before


def test_write_with_mirror_then_rescan_is_clean(shop):
    diff, n, _ = run(shop, mirror="ecr", write=True)
    assert n == 8
    wf = (shop / ".github/workflows/ci.yml").read_text()
    assert "container: public.ecr.aws/docker/library/node:20" in wf
    assert "image: public.ecr.aws/docker/library/redis\n" in wf
    assert "hadolint/hadolint:v2.12.0" in wf      # not an official image: left alone
    values = (shop / "charts/shop/values.yaml").read_text()
    assert "registry: docker.io\n    repository: bitnamilegacy/redis\n    tag: 7.2.4-debian-12-r9" in values
    # second run: nothing left to rewrite, comments and layout preserved
    diff2, n2, _ = run(shop, mirror="ecr", write=True)
    assert n2 == 0 and diff2 == ""
    left = {(f.rule, f.severity) for f in evaluate_all(scan_paths([str(shop)], str(shop)))}
    assert ("PR101", "error") not in left and ("PR201", "warn") not in left
    assert ("PR301", "warn") in left           # hadolint: no generic mirror, still reported


def test_arg_built_reference_is_skipped(tmp_path):
    (tmp_path / "Dockerfile").write_text("ARG V=7.2.5\nFROM bitnami/redis:${V}\n")
    diff, n, skipped = run(tmp_path, write=True)
    assert n == 0 and len(skipped) == 1
    assert (tmp_path / "Dockerfile").read_text() == "ARG V=7.2.5\nFROM bitnami/redis:${V}\n"
