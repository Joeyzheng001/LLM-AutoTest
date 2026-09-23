#!/usr/bin/env python3
"""离线评估 job 产物的结构合规与语义质量指标。

用法：
    python3 evaluate_job.py /path/to/job [golden.quality.json] [output.json]

脚本不调用 LLM、不访问被测服务，适合在模型/Prompt 变更后重复运行。
退出码：0 表示结构与 manifest 阈值均通过；1 表示不通过；2 表示参数错误。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "orchestrator" / "src"))

from orchestrator.golden_evaluation import evaluate_artifacts  # noqa: E402


DEFAULT_MANIFEST = Path(__file__).resolve().parent / "golden.quality.json"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not 1 <= len(args) <= 3:
        print(__doc__)
        return 2
    artifact_dir = Path(args[0])
    manifest_path = Path(args[1]) if len(args) >= 2 else DEFAULT_MANIFEST
    output_path = Path(args[2]) if len(args) == 3 else None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest 顶层必须是对象")
        report = evaluate_artifacts(artifact_dir, manifest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"评估失败：{exc}")
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path is not None:
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
