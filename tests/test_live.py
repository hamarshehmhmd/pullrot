"""Live checks against the real registries. Opt-in: PULLROT_LIVE=1 (CI runs them in a separate job)."""
import os

import pytest

from pullrot import registry
from pullrot.refs import parse

pytestmark = pytest.mark.skipif(not os.environ.get("PULLROT_LIVE"), reason="set PULLROT_LIVE=1")


def test_bitnami_versioned_tag_is_gone_and_legacy_exists():
    assert registry.check(parse("bitnami/redis:7.2.5")).status == "missing-tag"
    assert registry.check(parse("bitnamilegacy/redis:7.2.5")).status == "ok"


def test_other_registries():
    assert registry.check(parse("registry.k8s.io/pause:3.10")).status == "ok"
    assert registry.check(parse("public.ecr.aws/docker/library/postgres:16")).status == "ok"
    assert registry.check(parse("quay.io/prometheus/node-exporter:v1.8.2")).status == "ok"
    assert registry.check(parse("nginx:0.0.0-does-not-exist")).status == "missing-tag"


def test_docker_hub_ratelimit_headers():
    rl = registry.docker_hub_ratelimit()
    assert rl.limit is None or rl.limit > 0
