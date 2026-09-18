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
    knowledge_refs: tuple[str, ...] = ()  # 本次调用注入的 Skill/记忆版本（不含原文）

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
    knowledge_body, knowledge_refs = _load_node_knowledge(prompts_dir, name)
    return PromptSpec(
        name=name,
        version=version,
        spec=spec,
        owner=owner,
        changelog=changelog,
        sampling=sampling,
        body=f"{body}\n\n{knowledge_body}".strip() if knowledge_body else body,
        knowledge_refs=knowledge_refs,
    )


# 每个模型节点只加载自己需要的知识。禁止把全部历史经验拼入每一轮调用，
# 否则 Token 膨胀并稀释当前任务约束，反而降低结构化输出稳定性。
_NODE_KNOWLEDGE: dict[str, tuple[str, ...]] = {
    "requirement_diff": (
        "skills/requirement-diff-analysis.md",
        "skills/structured-output-compatibility.md",
    ),
    "requirement_review": (
        "skills/requirement-output-stability.md",
        "skills/structured-output-compatibility.md",
        "memory/model-output-drift.md",
    ),
    "req_parse": (
        "skills/requirement-item-extraction.md",
        "skills/structured-output-compatibility.md",
        "memory/model-output-drift.md",
    ),
    "interface_reconstruct": (
        "skills/interface-reconstruction.md",
        "skills/structured-output-compatibility.md",
    ),
    "code_review": (
        "skills/code-review-evidence.md",
        "skills/structured-output-compatibility.md",
        "memory/server-side-evidence.md",
    ),
    "testcase_generate": (
        "skills/functional-test-design.md",
        "skills/structured-output-compatibility.md",
        "memory/test-design-lessons.md",
    ),
    "e2e_testcase_generate": (
        "skills/e2e-automation.md",
        "skills/structured-output-compatibility.md",
        "memory/e2e-environment.md",
    ),
}


def _load_node_knowledge(prompts_dir: Path, name: str) -> tuple[str, tuple[str, ...]]:
    """加载节点 Skill 与审核记忆；文件不存在视为配置错误，禁止静默退化。"""
    from orchestrator.errors import X_LLM_03, PipelineError

    # 单元测试或外部最小 Prompt 包不带知识目录时保持旧加载器行为；
    # 正式 prompts 包一旦声明 knowledge 目录，就要求节点映射文件完整存在。
    if not (prompts_dir / "skills").is_dir():
        return "", ()
    pieces: list[str] = []
    refs: list[str] = []
    for relative in _NODE_KNOWLEDGE.get(name, ()):
        path = prompts_dir / relative
        if not path.is_file():
            raise PipelineError(
                code=X_LLM_03,
                message=f"LLM 知识文件不存在：{path}",
                details={"prompt": name, "knowledge": relative},
            )
        text = path.read_text(encoding="utf-8").strip()
        version, _, _, _, _, body = _parse_meta(text)
        if not version or not body:
            raise PipelineError(
                code=X_LLM_03,
                message=f"LLM 知识文件元信息或正文不合规：{path.name}",
                details={"prompt": name, "knowledge": relative},
            )
        refs.append(f"{relative.removesuffix('.md')}@{version}")
        pieces.append(f"\n\n## 受控知识：{relative.removesuffix('.md')}\n{body}")
    return "".join(pieces), tuple(refs)


def _parse_meta(
    text: str,
) -> tuple[str, str, str, tuple[str, ...], dict[str, float | int | None], str]:
    """解析 `---` 包裹的元信息块；格式不合法时返回空 version 交由上层报错。

    容忍元信息块前的 HTML 注释与空行（模板约定：首行用途注释）。块后的正文原样保留。
    """
    lines = text.splitlines()
    # 跳过前导空行与 <!-- --> 注释行，定位元信息块首行
    start = 0
    while start < len(lines) and (
        not lines[start].strip() or lines[start].lstrip().startswith("<!--")
    ):
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        return "", "", "", (), {}, text

    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return "", "", "", (), {}, text

    meta_lines = lines[start + 1 : end]
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
