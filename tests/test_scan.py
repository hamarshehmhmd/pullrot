from pullrot.scan import classify, scan_paths, scan_text


def refs(occs):
    return [(o.line, o.ref.short()) for o in occs]


def test_classify():
    assert classify("api/Dockerfile") == "dockerfile"
    assert classify("Dockerfile.dev") == "dockerfile"
    assert classify("build/app.dockerfile") == "dockerfile"
    assert classify("Containerfile") == "dockerfile"
    assert classify(".github/workflows/ci.yml") == "workflow"
    assert classify(".gitlab-ci.yml") == "gitlab"
    assert classify("compose.yaml") == "compose"
    assert classify(".devcontainer/devcontainer.json") == "devcontainer"
    assert classify("README.md") is None


def test_dockerfile_args_stages_copy_from():
    text = """\
ARG BASE=python
ARG VER
FROM --platform=$BUILDPLATFORM ${BASE}:3.12-slim AS builder
FROM builder AS test
FROM scratch AS empty
# FROM commented:1
FROM node:${VER:-20}
COPY --from=builder /a /b
COPY --from=bitnami/kubectl:1.30 /k /k
COPY --from=0 /x /y
FROM ghcr.io/org/${UNKNOWN}:1
FROM registry.k8s.io/pause:3.10
"""
    occs = scan_text(text, "Dockerfile")
    assert refs(occs) == [(3, "python:3.12-slim"), (7, "node:20"), (9, "bitnami/kubectl:1.30"),
                          (12, "registry.k8s.io/pause:3.10")]
    assert occs[0].spans == []           # built from ARGs: no in-place span
    assert occs[2].spans[0].start == len("COPY --from=")


def test_compose_and_comments():
    text = 'services:\n  a:\n    image: "postgres:16" # db\n  b:\n    image: redis  # cache\n  c:\n    build: .\n  # image: old:1\n'
    occs = scan_text(text, "docker-compose.yml")
    assert refs(occs) == [(3, "postgres:16"), (5, "redis")]
    line = text.splitlines()[2]
    s = occs[0].spans[0]
    assert line[s.start:s.end] == "postgres:16"


def test_kubernetes_list_items():
    text = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n    - image: k8s.gcr.io/busybox:1.27\n      name: a\n    - name: b\n      image: 'nginx:1.27'\n"
    occs = scan_text(text, "pod.yaml")
    assert refs(occs) == [(5, "k8s.gcr.io/busybox:1.27"), (8, "nginx:1.27")]
    assert all(o.kind == "kubernetes" for o in occs)


def test_helm_values_blocks():
    text = """\
redis:
  image:
    registry: docker.io
    repository: bitnami/redis
    tag: 7.2.4-debian-12-r9
  persistence:
    enabled: true
exporter:
  image:
    repository: bitnami/redis-exporter
    tag: "1.58.0"
    pullPolicy: IfNotPresent
nothing:
  repository: https://charts.bitnami.com/bitnami
  tag: x
"""
    occs = scan_text(text, "values.yaml")
    assert refs(occs) == [(4, "bitnami/redis:7.2.4-debian-12-r9"), (10, "bitnami/redis-exporter:1.58.0")]
    roles = {s.role: s.line for s in occs[0].spans}
    assert roles == {"repository": 4, "registry": 3, "tag": 5}


def test_helm_templated_values_are_skipped():
    text = "image:\n  repository: \"{{ .Values.repo }}\"\n  tag: 1\n"
    assert scan_text(text, "values.yaml") == []


def test_gitlab_image_name_and_string():
    text = "image: node:20\ntest:\n  image:\n    name: bitnami/kubectl:1.29\n    entrypoint: ['']\n  services:\n    - postgres:16\n    - name: redis:7\n"
    occs = scan_text(text, ".gitlab-ci.yml")
    assert refs(occs)[:2] == [(1, "node:20"), (4, "bitnami/kubectl:1.29")]
    assert all(o.ci and not o.ci_login for o in occs)


def test_workflow_container_services_uses_docker_and_login():
    text = """\
jobs:
  t:
    container: node:20
    services:
      pg:
        image: postgres:16
    steps:
      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
      - uses: docker://hadolint/hadolint:v2.12.0
      - uses: actions/checkout@v4
"""
    occs = scan_text(text, ".github/workflows/ci.yml")
    assert refs(occs) == [(3, "node:20"), (6, "postgres:16"), (11, "hadolint/hadolint:v2.12.0")]
    assert all(o.ci and o.ci_login for o in occs)


def test_container_key_outside_workflows_ignored():
    assert scan_text("container: nginx\n", "values.yaml") == []


def test_devcontainer_json():
    text = '{\n  // comment "image": "x:1"\n  "image": "mcr.microsoft.com/devcontainers/base:bookworm"\n}\n'
    occs = scan_text(text, ".devcontainer/devcontainer.json")
    assert refs(occs) == [(3, "mcr.microsoft.com/devcontainers/base:bookworm")]


def test_example_repo(shop):
    occs = scan_paths([str(shop)], str(shop))
    files = {o.path for o in occs}
    assert len(occs) == 17
    assert files == {"docker-compose.yml", ".devcontainer/devcontainer.json", ".github/workflows/ci.yml",
                     "api/Dockerfile", "charts/shop/values.yaml", "deploy/k8s/app.yaml"}


def test_skips_vendor_dirs(tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "Dockerfile").write_text("FROM bitnami/redis:7\n")
    (tmp_path / "Dockerfile").write_text("FROM alpine:3.20\n")
    assert [o.ref.short() for o in scan_paths([str(tmp_path)], str(tmp_path))] == ["alpine:3.20"]
