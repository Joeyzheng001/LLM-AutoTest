"""部署脚本的无 Docker 回归测试。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _fake_docker(tmp_path: Path) -> Path:
    script = tmp_path / "docker"
    script.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case " $* " in
  *\\ pg_dump\\ *) printf 'fake-custom-format-dump\\n' ;;
  *) cat >/dev/null ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _run_script(
    script: Path, *, env: dict[str, str], args: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test invokes the checked-in shell script
        ["sh", str(script), *args],  # noqa: S607 - test invokes the checked-in shell script
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_backup_script_writes_final_dump_atomically_and_checksum(tmp_path: Path) -> None:
    docker = _fake_docker(tmp_path)
    backup_dir = tmp_path / "backups"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(tmp_path / "backup-docker.log"),
        "BACKUP_DIR": str(backup_dir),
        "COMPOSE_ENV_FILE": str(tmp_path / ".env"),
    }
    result = _run_script(ROOT / "deploy/backup_postgres.sh", env=env, args=[])
    assert result.returncode == 0, result.stderr
    dumps = list(backup_dir.glob("autotest-*.dump"))
    assert len(dumps) == 1
    dump = dumps[0]
    checksum = dump.with_name(f"{dump.name}.sha256")
    assert checksum.is_file()
    assert dump.read_bytes() == b"fake-custom-format-dump\n"
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="utf-8").split()[0] == digest
    assert not list(backup_dir.glob(".autotest-*.dump"))
    assert stat.S_IMODE(dump.stat().st_mode) & 0o077 == 0
    assert docker.is_file()


def test_restore_script_rejects_checksum_before_touching_database(tmp_path: Path) -> None:
    docker = _fake_docker(tmp_path)
    dump = tmp_path / "broken.dump"
    dump.write_bytes(b"not-the-original")
    dump.with_name("broken.dump.sha256").write_text(
        f"{'0' * 64}  {dump.name}\n", encoding="utf-8"
    )
    log = tmp_path / "restore-docker.log"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(log),
        "COMPOSE_ENV_FILE": str(tmp_path / ".env"),
    }
    result = _run_script(ROOT / "deploy/restore_rehearsal.sh", env=env, args=[str(dump)])
    assert result.returncode != 0
    assert "dropdb" not in log.read_text(encoding="utf-8") if log.exists() else True
    assert docker.is_file()


def test_acceptance_smoke_checks_front_door_and_protected_api(tmp_path: Path) -> None:
    """验收探针只输出摘要，并能通过统一入口验证项目鉴权。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                payload = {"status": "ok"}
                content_type = "application/json"
            elif self.path == "/ready":
                payload = {"status": "ready"}
                content_type = "application/json"
            elif self.path == "/metrics":
                raw = b"autotest_database_up 1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            elif self.path == "/api/v1/jobs":
                assert self.headers.get("Authorization") == "Bearer test-secret"
                assert self.headers.get("X-Project-Id") == "fund"
                payload = {"items": []}
                content_type = "application/json"
            else:
                self.send_error(404)
                return
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **os.environ,
            "AUTOTEST_ACCEPTANCE_TOKEN": "test-secret",
            "AUTOTEST_ACCEPTANCE_PROJECT_ID": "fund",
        }
        result = subprocess.run(  # noqa: S603 - test invokes the checked-in probe
            [
                sys.executable,
                str(ROOT / "deploy/acceptance_smoke.py"),
                "--url",
                f"http://127.0.0.1:{server.server_port}",
                "--retries",
                "0",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["ok"] is True
        assert "test-secret" not in result.stdout
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_acceptance_smoke_rejects_spa_html_as_health_success() -> None:
    """统一入口不能把前端 SPA 的 200 HTML 当成后端探活成功。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            raw = b"<!doctype html><html><body>frontend</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import importlib.util

        source = ROOT / "deploy/acceptance_smoke.py"
        spec = importlib.util.spec_from_file_location("acceptance_smoke", source)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run_checks(
            f"http://127.0.0.1:{server.server_port}", retries=0
        )
        assert result["ok"] is False
        assert all(item["ok"] is False for item in result["checks"])
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_department_compose_keeps_container_repository_path_stable() -> None:
    compose = (ROOT / "deploy/docker-compose.department.yml").read_text(encoding="utf-8")
    assert "source: ${REPOSITORIES_HOST_ROOT:-/opt/repos}" in compose
    assert "target: /opt/repos" in compose
    assert "read_only: true" in compose
    assert "minio/minio:latest" not in compose
    assert "MINIO_IMAGE:-minio/minio:RELEASE.2024-06-13T22-53-53Z" in compose
    assert "${ORCHESTRATOR_IMAGE:-llm-autotest/orchestrator:0.1.0}" in compose

    dockerfile = (ROOT / "services/orchestrator/Dockerfile").read_text(encoding="utf-8")
    assert "ghcr.io/astral-sh/uv:latest" not in dockerfile
    assert "ARG UV_VERSION=0.12.9" in dockerfile

    overlay = (ROOT / "deploy/Dockerfile.orchestrator.overlay").read_text(encoding="utf-8")
    assert "ARG BASE_IMAGE=llm-autotest/orchestrator:0.1.0" in overlay
    assert "FROM ${BASE_IMAGE}" in overlay
    assert "apt-get" not in overlay
    assert "uv sync" not in overlay

    load_smoke = (ROOT / "deploy/load_smoke.py").read_text(encoding="utf-8")
    assert "from urllib.request import Request, urlopen" in load_smoke
    assert "import httpx" not in load_smoke
    assert "--url 必须是完整的 http(s) 地址" in load_smoke


def test_department_bootstrap_dry_run_has_safe_startup_order(tmp_path: Path) -> None:
    """一键部署先预检/迁移，再启动双实例并验收，且 dry-run 不打印密钥。"""
    env_file = tmp_path / "department.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=do-not-print\nAUTOTEST_ACCESS_TOKENS_JSON=do-not-print\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "DRY_RUN": "1",
        "COMPOSE_ENV_FILE": str(env_file),
        "ORCHESTRATOR_REPLICAS": "2",
    }
    result = _run_script(
        ROOT / "deploy/department_bootstrap.sh", env=env, args=[]
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert output.index("validate-deployment") < output.index("up -d postgres")
    assert output.index("migrate-db") < output.index("--scale orchestrator=2")
    assert "acceptance_smoke.py" in output
    assert "do-not-print" not in output

    env["ACCEPTANCE_INSECURE_TLS"] = "1"
    insecure = _run_script(
        ROOT / "deploy/department_bootstrap.sh", env=env, args=[]
    )
    assert insecure.returncode == 0, insecure.stderr
    assert "acceptance_smoke.py" in insecure.stdout
    assert "--insecure-tls" in insecure.stdout


def test_department_bootstrap_overlay_dry_run_uses_local_overlay_image(tmp_path: Path) -> None:
    """离线源码发布不能触发 orchestrator 的 apt/uv 完整构建。"""
    env_file = tmp_path / "department.env"
    env_file.write_text("POSTGRES_PASSWORD=do-not-print\n", encoding="utf-8")
    result = _run_script(
        ROOT / "deploy/department_bootstrap.sh",
        env={
            **os.environ,
            "DRY_RUN": "1",
            "COMPOSE_ENV_FILE": str(env_file),
            "ORCHESTRATOR_BUILD_MODE": "overlay",
            "ORCHESTRATOR_IMAGE": "llm-autotest/orchestrator:release-test",
        },
        args=[],
    )
    assert result.returncode == 0, result.stderr
    assert "offline_overlay_build.sh" in result.stdout
    assert "build orchestrator" not in result.stdout
    assert "do-not-print" not in result.stdout
