"""orchestrator 服务入口：CLI 统一入口（F-ORCH-01/02）+ REST API 服务。

子命令：
- run：单命令跑全流程（--doc 需求文档可多份（C-10）--repos 仓库配置 JSON
  --rest-url 被测服务 --auth 鉴权配置 JSON 文件（C-15，可选））
- status：查 job 状态与产物清单（断点续跑诊断）
- serve：启动 REST API（Web 并存入口，V2.1）
- validate-deployment：启动前校验部门共享部署配置（不连接外部服务）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from orchestrator.artifact_migration import migrate_artifact_index
from orchestrator.artifacts import ArtifactStore, ensure_job_id
from orchestrator.config import Settings
from orchestrator.engine import Engine
from orchestrator.errors import PipelineError
from orchestrator.javaparser_client import JavaParserClient
from orchestrator.object_storage import build_object_storage
from orchestrator.persistence import Database
from orchestrator.pipeline import NodeStatus
from orchestrator.runner import (
    PipelineRunner,
    repository_inputs_from_config,
    summarize_outcome,
)
from orchestrator.security import AccessPolicy, SecretStore, SecretStoreError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _build_runner(settings: Settings) -> PipelineRunner:
    """组装双身份产物卷 + 客户端 + 引擎（单体过渡期编排进程内驱动）。"""
    root = Path(settings.artifacts_root)
    schemas = Path(settings.schemas_dir)
    object_storage = build_object_storage(settings)
    database = Database(settings.database_url) if settings.database_url else None
    if database is not None and settings.database_auto_create:
        database.migrate_schema()
    orch_store = ArtifactStore(
        root, schemas, "orchestrator", object_storage=object_storage, database=database
    )
    te_store = ArtifactStore(
        root, schemas, "test-executor", object_storage=object_storage, database=database
    )
    engine = Engine(orch_store)
    java_client = JavaParserClient(base_url=settings.java_parser_url)
    return PipelineRunner(
        settings, engine, orch_store, te_store, java_client, database=database
    )


async def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """run 子命令：跑全流程（状态已存在时自动断点续跑）。"""
    # C-12：完整正则校验（与 ArtifactStore 同源）；入口拒绝免产物校验级联降级
    try:
        ensure_job_id(args.job_id)
    except PipelineError as exc:
        logger.error(exc.message)
        return 2
    doc_paths = [Path(doc) for doc in args.doc]
    missing = [str(path) for path in doc_paths if not path.is_file()]
    if missing:
        logger.error("需求文档不存在：%s", "、".join(missing))
        return 2
    try:
        repositories = repository_inputs_from_config(json.loads(Path(args.repos).read_text()))
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("仓库配置非法：%s", exc)
        return 2
    if not repositories:
        logger.error("仓库配置为空（--repos JSON 数组，至少 1 个）")
        return 2
    auth: dict[str, object] | None = None
    if args.auth:
        try:
            auth = json.loads(Path(args.auth).read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("鉴权配置非法（C-15）：%s", exc)
            return 2

    runner = _build_runner(settings)
    outcome = await runner.run(
        job_id=args.job_id,
        doc_paths=doc_paths,
        repositories=repositories,
        rest_base_url=args.rest_url,
        force_node=args.force_node,
        auth=auth,
    )
    print(summarize_outcome(outcome))
    return 0 if outcome.ok else 1


async def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    """status 子命令：job 状态 + 产物清单（断点续跑诊断）。"""
    database = Database(settings.database_url) if settings.database_url else None
    if database is not None and settings.database_auto_create:
        database.migrate_schema()
    orch_store = ArtifactStore(
        Path(settings.artifacts_root),
        Path(settings.schemas_dir),
        "orchestrator",
        object_storage=build_object_storage(settings),
        database=database,
    )
    engine = Engine(orch_store)
    state = engine.open_job(args.job_id)
    print(f"job：{args.job_id}")
    for node in state.nodes:
        marker = {
            NodeStatus.COMPLETED: "✅",
            NodeStatus.RUNNING: "▶️",
            NodeStatus.MANUAL_INTERVENTION: "⚠️",
            NodeStatus.PENDING: "⬚",
        }[node.status]
        error = f"—— {node.error['code']}：{node.error['message']}" if node.error else ""
        print(f"  {marker} {node.name}（尝试 {node.attempts} 次）{error}")
    artifacts = [
        f.name
        for f in sorted(orch_store.job_dir(args.job_id).iterdir())
        if f.is_file() and f.name != "state.json"
    ]
    print(f"产物：{', '.join(artifacts) if artifacts else '<无>'}")
    return 0


def _cmd_serve(settings: Settings) -> int:
    """serve 子命令：启动 REST API。"""
    import uvicorn

    from orchestrator.api import create_app

    logger.info("orchestrator 启动：%s", settings.public_summary)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)
    return 0


def _cmd_migrate_artifacts(args: argparse.Namespace, settings: Settings) -> int:
    """索引历史任务产物；只登记/镜像，不删除或覆盖原文件。"""
    if not settings.database_url:
        logger.error("迁移历史产物需要配置 DATABASE_URL")
        return 2
    database = Database(settings.database_url)
    if settings.database_auto_create:
        database.migrate_schema()
    report = migrate_artifact_index(
        Path(settings.artifacts_root),
        Path(settings.schemas_dir),
        database,
        object_storage=build_object_storage(settings),
        job_id=args.job_id,
    )
    print(
        f"历史产物迁移完成：jobs={report.jobs} files={report.files} "
        f"skipped={report.skipped} errors={len(report.errors)}"
    )
    for error in report.errors[:20]:
        print(f"  - {error}")
    return 1 if report.errors else 0


def _cmd_init_db(settings: Settings) -> int:
    """兼容入口：执行显式、幂等的数据库迁移。"""
    return _cmd_migrate_db(settings)


def _cmd_migrate_db(settings: Settings) -> int:
    """执行显式、幂等的数据库迁移；生产部署在迁移窗口执行。"""
    if not settings.database_url:
        logger.error("数据库迁移需要配置 DATABASE_URL")
        return 2
    database = Database(settings.database_url)
    applied = database.migrate_schema()
    print(
        "数据库 Schema 迁移完成："
        f"applied={','.join(applied) if applied else '<none>'} "
        f"current={database.schema_version() or '<none>'}"
    )
    return 0


def _cmd_validate_deployment(settings: Settings) -> int:
    """校验部门部署必需配置，不连接外部服务、不输出任何密钥。"""
    errors: list[str] = []
    database_url = settings.database_url or ""
    if not database_url:
        errors.append("DATABASE_URL 未配置")
    elif not database_url.lower().startswith("postgresql"):
        errors.append("部门部署 DATABASE_URL 必须使用 PostgreSQL（postgresql+psycopg）")

    if not settings.secrets_encryption_key:
        errors.append("SECRETS_ENCRYPTION_KEY 未配置")
    else:
        try:
            SecretStore(Path(settings.artifacts_root), settings.secrets_encryption_key)
        except SecretStoreError as exc:
            errors.append(str(exc))

    if not settings.access_tokens_json:
        errors.append("AUTOTEST_ACCESS_TOKENS_JSON 未配置")
    else:
        try:
            AccessPolicy(settings.access_tokens_json)
        except SecretStoreError as exc:
            errors.append(str(exc))

    backend = settings.artifact_storage_backend.lower()
    if backend not in {"", "local", "filesystem", "s3"}:
        errors.append(f"不支持的 ARTIFACT_STORAGE_BACKEND：{backend}")
    if backend == "s3":
        if not settings.artifact_storage_bucket:
            errors.append("启用 S3/MinIO 时 ARTIFACT_STORAGE_BUCKET 不能为空")
        if not settings.artifact_storage_endpoint:
            errors.append("启用 S3/MinIO 时 ARTIFACT_STORAGE_ENDPOINT 不能为空")

    if errors:
        for error in errors:
            logger.error("部署配置校验失败：%s", error)
        return 2
    print(
        "部署配置校验通过："
        f"database=postgresql secret_store=enabled access_control=enabled "
        f"artifact_storage={backend or 'local'} llm_model={settings.llm_model_name} "
        f"llm_timeout_s={settings.llm_timeout_s:g}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数定义。"""
    parser = argparse.ArgumentParser(prog="orchestrator", description="LLM-AutoTest 编排服务")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="单命令跑全流程（F-ORCH-01）")
    run.add_argument("--job-id", required=True, help="job 标识（job-<uuid>）")
    run.add_argument(
        "--doc",
        required=True,
        action="append",
        help="需求文档路径（md/docx/xlsx；可重复传入多份，C-10）",
    )
    run.add_argument(
        "--repos", required=True, help="仓库配置 JSON 路径（[{name,path,branch,is_fallback?}]）"
    )
    run.add_argument(
        "--rest-url", required=True, help="被测 REST 服务地址（执行与复测共用）"
    )
    run.add_argument(
        "--force-node",
        default=None,
        help="重跑指定已完成节点（旧产物移入 archive/，留痕红线，设计 §1.2）",
    )
    run.add_argument(
        "--auth",
        default=None,
        help=(
            "鉴权配置 JSON 文件路径（C-15："
            "{login_url,username,password,insecure_tls}；建议文件而非命令行避免凭据进 shell 历史）"
        ),
    )

    status = sub.add_parser("status", help="查 job 状态与产物清单")
    status.add_argument("--job-id", required=True)

    sub.add_parser("serve", help="启动 REST API 服务（Web 并存入口）")
    migrate = sub.add_parser("migrate-artifacts", help="将历史 job 产物登记到数据库/对象存储")
    migrate.add_argument("--job-id", default=None, help="只迁移指定 job（缺省迁移全部）")
    sub.add_parser("migrate-db", help="执行显式、幂等数据库迁移（生产迁移窗口使用）")
    sub.add_parser("init-db", help="migrate-db 的兼容别名")
    sub.add_parser(
        "validate-deployment",
        help="校验部门部署必需配置（不连接数据库/对象存储，不输出密钥）",
    )
    return parser


def main() -> None:
    """CLI 入口：子命令分派。"""
    parser = build_parser()
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "serve":
        sys.exit(_cmd_serve(settings))
    if args.command == "run":
        sys.exit(asyncio.run(_cmd_run(args, settings)))
    if args.command == "status":
        sys.exit(asyncio.run(_cmd_status(args, settings)))
    if args.command == "migrate-artifacts":
        sys.exit(_cmd_migrate_artifacts(args, settings))
    if args.command == "init-db":
        sys.exit(_cmd_init_db(settings))
    if args.command == "migrate-db":
        sys.exit(_cmd_migrate_db(settings))
    if args.command == "validate-deployment":
        sys.exit(_cmd_validate_deployment(settings))


if __name__ == "__main__":
    main()
