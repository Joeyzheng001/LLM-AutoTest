"""orchestrator 服务入口：CLI 统一入口（F-ORCH-01/02）+ REST API 服务。

子命令：
- run：单命令跑全流程（--doc 需求文档 --repos 仓库配置 JSON --rest-url 被测服务）
- status：查 job 状态与产物清单（断点续跑诊断）
- serve：启动 REST API（Web 并存入口，V2.1）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from orchestrator.artifacts import ArtifactStore
from orchestrator.config import Settings
from orchestrator.engine import Engine
from orchestrator.javaparser_client import JavaParserClient
from orchestrator.pipeline import NodeStatus
from orchestrator.runner import (
    PipelineRunner,
    repository_inputs_from_config,
    summarize_outcome,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _build_runner(settings: Settings) -> PipelineRunner:
    """组装双身份产物卷 + 客户端 + 引擎（单体过渡期编排进程内驱动）。"""
    root = Path(settings.artifacts_root)
    schemas = Path(settings.schemas_dir)
    orch_store = ArtifactStore(root, schemas, "orchestrator")
    te_store = ArtifactStore(root, schemas, "test-executor")
    engine = Engine(orch_store)
    java_client = JavaParserClient(base_url=settings.java_parser_url)
    return PipelineRunner(settings, engine, orch_store, te_store, java_client)


async def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """run 子命令：跑全流程（状态已存在时自动断点续跑）。"""
    doc_path = Path(args.doc)
    if not doc_path.is_file():
        logger.error("需求文档不存在：%s", doc_path)
        return 2
    try:
        repositories = repository_inputs_from_config(json.loads(Path(args.repos).read_text()))
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("仓库配置非法：%s", exc)
        return 2
    if not repositories:
        logger.error("仓库配置为空（--repos JSON 数组）")
        return 2

    runner = _build_runner(settings)
    outcome = await runner.run(
        job_id=args.job_id,
        doc_path=doc_path,
        repositories=repositories,
        rest_base_url=args.rest_url,
        force_node=args.force_node,
    )
    print(summarize_outcome(outcome))
    return 0 if outcome.ok else 1


async def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    """status 子命令：job 状态 + 产物清单（断点续跑诊断）。"""
    orch_store = ArtifactStore(
        Path(settings.artifacts_root), Path(settings.schemas_dir), "orchestrator"
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


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数定义。"""
    parser = argparse.ArgumentParser(prog="orchestrator", description="LLM-AutoTest 编排服务")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="单命令跑全流程（F-ORCH-01）")
    run.add_argument("--job-id", required=True, help="job 标识（job-<uuid>）")
    run.add_argument("--doc", required=True, help="需求文档路径（md/docx/xlsx）")
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

    status = sub.add_parser("status", help="查 job 状态与产物清单")
    status.add_argument("--job-id", required=True)

    sub.add_parser("serve", help="启动 REST API 服务（Web 并存入口）")
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


if __name__ == "__main__":
    main()
