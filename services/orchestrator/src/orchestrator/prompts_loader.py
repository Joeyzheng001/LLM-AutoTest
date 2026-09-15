"""Prompt 加载器：解析 prompts/ 下带元信息块的 Markdown（prompts/_template.md 约定）。

红线（.rules/llm-integrity.md）：Prompt 只能从文件加载，禁止代码内联字符串；
元信息块字段名与模板一致，禁止自造。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptSpec:
    """单个 Prompt 的加载结果（模板头部元信息 + 正文）。"""

    name: str  # 文件名去扩展名（调用方引用名）
    version: str
    spec: str  # 对应规格编号（如 F-REQ-01）
    owner: str  # 主笔人（甲/乙/丙）
    changelog: tuple[str, ...]  # "版本 | 日期 | 说明"
    sampling: dict[str, float | int | None]  # 模板建议值（实际以 Settings 为准）
    body: str  # `---` 元信息块之后的 Prompt 正文

    @property
    def ref(self) -> str:
        """留痕用引用名：name@version（llm_calls.jsonl 的 prompt_file 字段）。"""
        return f"{self.name}@{self.version}"


def load_prompt(prompts_dir: Path, name: str) -> PromptSpec:
    """按名称加载 Prompt 文件并解析元信息块。

    Raises:
        PipelineError: X-LLM-03——文件缺失或元信息块不合规（配置错误，人工介入）。
    """
    from orchestrator.errors import X_LLM_03, PipelineError

    path = prompts_dir / f"{name}.md"
    if not path.is_file():
        raise PipelineError(
            code=X_LLM_03,
            message=f"Prompt 文件不存在：{path}",
            details={"prompt": name},
        )

    text = path.read_text(encoding="utf-8")
    version, spec, owner, changelog, sampling, body = _parse_meta(text)
    if not version:
        raise PipelineError(
            code=X_LLM_03,
            message=f"Prompt 元信息块缺 version 字段：{path.name}",
            details={"prompt": name},
        )
    return PromptSpec(
        name=name,
        version=version,
        spec=spec,
        owner=owner,
        changelog=changelog,
        sampling=sampling,
        body=body,
    )


def _parse_meta(
    text: str,
) -> tuple[str, str, str, tuple[str, ...], dict[str, float | int | None], str]:
    """解析 `---` 包裹的元信息块；格式不合法时返回空 version 交由上层报错。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", "", "", (), {}, text

    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return "", "", "", (), {}, text

    meta_lines = lines[1:end]
    body = "\n".join(lines[end + 1 :]).strip()

    version, spec, owner = "", "", ""
    changelog: list[str] = []
    sampling: dict[str, float | int | None] = {}
    in_sampling = False
    for raw in meta_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 顶层键（无缩进非列表）结束 sampling 子块
        if not raw[:1].isspace() and not line.startswith("-"):
            in_sampling = False
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip()
            continue
        if line.startswith("spec:"):
            spec = line.split(":", 1)[1].split("#", 1)[0].strip()
            continue
        if line.startswith("owner:"):
            owner = line.split(":", 1)[1].split("#", 1)[0].strip()
            continue
        if line.startswith("changelog:"):
            continue
        if line.startswith("sampling:"):
            in_sampling = True
            continue
        if line.startswith("- ") and not in_sampling:
            # changelog 列表项："1.0 | 2026-09-15 | 初版"
            changelog.append(line[2:].strip())
            continue
        if in_sampling and ":" in line:
            key, _, value = line.partition(":")
            sampling[key.strip()] = _parse_scalar(value.strip())
    return version, spec, owner, tuple(changelog), sampling, body


def _parse_scalar(value: str) -> float | int | None:
    """解析采样参数标量：null → None；整数保留 int（seed），其余 float。"""
    if value in ("null", "~", ""):
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        return None
