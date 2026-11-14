import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "shop")

# repo -> set of tags that exist
CONTENT = {
    "library/nginx": {"1.27", "latest"},
    "library/redis": {"7", "latest"},
    "bitnami/redis": {"latest"},
    "bitnamilegacy/redis": {"7.2.5", "7.2.4-debian-12-r9"},
    "bitnamilegacy/postgresql": {"16.3.0-debian-12-r14"},
    "busybox": {"1.27"},
}


class FakeRegistry(BaseHTTPRequestHandler):
    """Docker Registry v2 with Docker Hub-style bearer token auth."""
    hits = []
    ratelimited = set()

    def log_message(self, *a):
        pass

    def _send(self, code, headers=None, body=b""):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        FakeRegistry.hits.append((self.command, self.path))
        host = f"http://{self.headers['Host']}"
        if self.path.startswith("/token"):
            scope = self.path.split("scope=")[1].split("&")[0]
            return self._send(200, {"Content-Type": "application/json"},
                              json.dumps({"token": "tok:" + scope}).encode())
        if not self.path.startswith("/v2/"):
            return self._send(404)
        rest = self.path[4:]
        if "/manifests/" in rest:
            repo, ref = rest.split("/manifests/")
        elif "/tags/list" in rest:
            repo, ref = rest.split("/tags/list")[0], None
        else:
            return self._send(404)
        if self.headers.get("Authorization") != f"Bearer tok:repository%3A{repo.replace('/', '%2F')}%3Apull":
            return self._send(401, {"WWW-Authenticate":
                                    f'Bearer realm="{host}/token",service="fake",scope="repository:{repo}:pull"'})
        if repo not in CONTENT:
            return self._send(401)
        if repo in FakeRegistry.ratelimited:
            return self._send(429)
        if ref is None:
            return self._send(200, {"Content-Type": "application/json"},
                              json.dumps({"name": repo, "tags": sorted(CONTENT[repo])}).encode())
        if ref in CONTENT[repo]:
            return self._send(200, {"Docker-Content-Digest": "sha256:" + "ab" * 32,
                                    "RateLimit-Limit": "100;w=21600", "RateLimit-Remaining": "73;w=21600",
                                    "docker-ratelimit-source": "203.0.113.7"})
        return self._send(404)

    do_GET = _route
    do_HEAD = _route


CONTENT["ratelimitpreview/test"] = {"latest"}


@pytest.fixture
def fake_registry(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("PULLROT_REGISTRY_MAP", f"docker.io={url},registry.k8s.io={url}")
    from pullrot import registry
    registry._TOKENS.clear()
    FakeRegistry.hits.clear()
    FakeRegistry.ratelimited.clear()
    yield url
    srv.shutdown()


@pytest.fixture
def shop(tmp_path):
    dst = tmp_path / "shop"
    shutil.copytree(EXAMPLE, dst)
    return dst
