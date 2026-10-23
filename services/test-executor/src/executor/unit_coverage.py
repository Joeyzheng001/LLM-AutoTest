"""Run generated mock based Java unit tests in a pinned, isolated Git snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from common.artifacts import ArtifactStore
from common.errors import PipelineError, X_LLM_01
from common.llm.client import LlmResult
from common.llm_text import strip_code_fence
from common.oracle_conditions import required_control_kinds
from common.prompts_loader import PromptSpec, load_prompt
from common.security import redact_secrets

_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
_PACKAGE = re.compile(r"^[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*$")
_SECRET = re.compile(
    r'(?i)\b(password|passwd|secret|token|api[_-]?key)\b(\s*[:=]\s*)("[^"]*"|\'[^\']*\')'
)
_JACOCO_GOAL = "org.jacoco:jacoco-maven-plugin:0.8.12"
_MAX_CONTEXT_CHARS = 70_000
_CASE_MAVEN_TIMEOUT_SECONDS = 180
_SUITE_MAVEN_TIMEOUT_SECONDS = 900
_HOMEBREW_JAVA_21 = Path("/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home")
logger = logging.getLogger(__name__)


def _model_connection_failed(exc: Exception) -> bool:
    return isinstance(exc, PipelineError) and exc.code == X_LLM_01


def _working_tree_matches_source(repo: dict[str, Any]) -> bool:
    """Only borrow local tests when production Java still matches the pinned HEAD."""
    source = Path(str(repo.get("path", ""))).resolve()
    try:
        head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True, timeout=15).stdout.strip()
        if head != str(repo.get("target_sha", "")):
            return False
        dirty = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all", "--", ":(glob)**/src/main/**"],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout
        return not dirty.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def _existing_test_path(source_file: str) -> str | None:
    prefix, separator, relative = source_file.partition("src/main/java/")
    if (not separator or not relative.endswith(".java")
            or PurePosixPath(source_file).is_absolute()
            or ".." in PurePosixPath(source_file).parts):
        return None
    return f"{prefix}src/test/java/{relative[:-5]}Test.java"


def _existing_test_reference(repo: dict[str, Any], source_file: str) -> str | None:
    if not _working_tree_matches_source(repo):
        return None
    relative = _existing_test_path(source_file)
    if relative is None:
        return None
    path = Path(str(repo["path"])).resolve() / relative
    if not path.is_file() or path.stat().st_size > 90_000:
        return None
    source = path.read_text(encoding="utf-8")[:16_000]
    return _SECRET.sub(lambda match: f'{match.group(1)}{match.group(2)}"<redacted>"', source)


class SupportsComplete(Protocol):
    async def complete(self, node: str, prompt: PromptSpec, user_content: str,
                       max_attempts: int | None = None) -> LlmResult: ...


def parse_jacoco_xml(path: Path, *, include_missed: bool = False) -> dict[str, Any]:
    """Parse module and class LINE/BRANCH counters from one JaCoCo XML report."""
    root = ET.parse(path).getroot()
    def counter(parent: ET.Element, kind: str) -> dict[str, Any] | None:
        node = next((item for item in parent.findall("counter")
                     if item.get("type") == kind), None)
        if node is None:
            return None
        missed = int(node.get("missed", "0"))
        covered = int(node.get("covered", "0"))
        return {"covered": covered, "missed": missed,
                "rate": covered / (covered + missed) if covered + missed else None}

    line_counter = counter(root, "LINE")
    if line_counter is None:
        raise ValueError("JaCoCo XML 缺少 LINE counter")
    files: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    for package in root.findall("package"):
        package_name = str(package.get("name", ""))
        for class_node in package.findall("class"):
            class_name = str(class_node.get("name", ""))
            if "$" in class_name:
                continue
            classes.append({
                "name": class_name.replace("/", "."),
                "source_file": f"{package_name}/{class_node.get('sourcefilename', '')}",
                "line_coverage": counter(class_node, "LINE"),
                "branch_coverage": counter(class_node, "BRANCH"),
            })
        for source in package.findall("sourcefile"):
            lines = {
                int(line.attrib["nr"]): int(line.get("ci", "0")) > 0
                for line in source.findall("line")
            }
            if not lines:
                continue
            item = {
                "path": f"{package_name}/{source.get('name', '')}",
                "covered": sum(lines.values()),
                "missed": sum(not value for value in lines.values()),
                "covered_lines": sorted(number for number, hit in lines.items() if hit),
            }
            if include_missed:
                item["missed_lines"] = sorted(number for number, hit in lines.items() if not hit)
            files.append(item)
    return {**line_counter, "branch_coverage": counter(root, "BRANCH"),
            "classes": classes, "files": files}


def _surefire_result(module: Path, class_name: str) -> dict[str, Any]:
    reports = list((module / "target" / "surefire-reports").glob(f"TEST-*.{class_name}.xml"))
    if not reports:
        return {"status": "not_run", "reason": "未生成对应的 Surefire 测试记录"}
    counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    issues: list[tuple[str, str, str, str]] = []
    for report in reports:
        try:
            root = ET.parse(report).getroot()
        except (ET.ParseError, OSError):
            return {"status": "not_run", "reason": "Surefire 测试记录不可读"}
        for key in counts:
            counts[key] += int(root.get(key, "0"))
        for test_case in root.findall("testcase"):
            for kind in ("error", "failure"):
                issue = test_case.find(kind)
                if issue is not None:
                    issues.append((kind, issue.get("type", ""),
                                   (issue.get("message") or "").strip(),
                                   issue.text or ""))
    if counts["tests"] == 0 or counts["tests"] == counts["skipped"]:
        return {"status": "not_run", "reason": "测试均未实际执行", **counts}
    if issues:
        kind, issue_type, message, stack = issues[0]
        prefix = "执行异常" if kind == "error" else "断言失败"
        summary = f"{prefix}：{issue_type.split('.')[-1]} {message}".strip()[:500]
        fixture_error = (
            any(name in issue_type for name in (
                "InjectMocksException", "NotAMockException",
                "MissingMethodInvocationException", "InvalidUseOfMatchersException",
                "UnnecessaryStubbingException", "PotentialStubbingProblem",
                "MockitoException",
            ))
            or ("NullPointerException" in issue_type and (
                ".<init>(" in stack or re.search(r'because "this\.\w+" is null', message)
                or ('"entity" is null' in message and "AbstractRiskService.postWithJson" in stack)
            ))
            or ("Failed to set private fields" in message
                and any(name in stack for name in ("NoSuchFieldException", "IllegalArgumentException")))
        )
        return {"status": "fixture_failed" if fixture_error else "failed",
                "reason": summary, **counts}
    return {"status": "passed", **counts}


def _snapshot(repo: dict[str, Any], job_dir: Path) -> Path:
    source = Path(str(repo.get("path", ""))).resolve()
    sha = str(repo.get("target_sha", ""))
    if not source.is_dir() or not _SHA.fullmatch(sha):
        raise ValueError("被测仓库或目标提交不可用")
    key = hashlib.sha256(f"{source}\0{sha}".encode()).hexdigest()[:20]
    destination = job_dir / ".unit-workspaces" / key
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Maven projects may use git-commit-id-plugin during compilation. A local
    # shared clone preserves .git while checking out only the pinned commit;
    # untracked files and working-tree edits never enter this workspace.
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", "--quiet", str(source), str(destination)],
        capture_output=True, timeout=120, check=True,
    )
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--detach", "--quiet", sha],
        capture_output=True, timeout=120, check=True,
    )
    (destination / ".target-sha").write_text(sha, encoding="ascii")
    return destination


def _module_root(snapshot: Path, file: str) -> Path | None:
    relative = PurePosixPath(file)
    if relative.is_absolute() or ".." in relative.parts or not file.endswith(".java"):
        return None
    source = snapshot.joinpath(*relative.parts)
    for parent in source.parents:
        if parent == snapshot.parent:
            break
        if (parent / "pom.xml").is_file():
            return parent
    return None


def _superclass_context(snapshot: Path, file: str, content: str) -> str:
    """Include one local parent class when the tested behavior is inherited."""
    declaration = re.search(r"\bclass\s+\w+[^\{]*?\bextends\s+([\w.]+)", content)
    if declaration is None:
        return ""
    parent_name = declaration.group(1).split(".")[-1]
    if not re.fullmatch(r"[A-Za-z_]\w*", parent_name):
        return ""
    relative = PurePosixPath(file)
    if relative.is_absolute() or ".." in relative.parts:
        return ""
    candidate = snapshot / relative.parent / f"{parent_name}.java"
    if not candidate.is_file() or candidate.stat().st_size > 30_000:
        return ""
    parent = _SECRET.sub(
        lambda match: f'{match.group(1)}{match.group(2)}"<redacted>"',
        candidate.read_text(encoding="utf-8"),
    )
    return f"文件 {(relative.parent / candidate.name).as_posix()}（父类）:\n{parent}"


def _source_context(snapshot: Path, oracle: dict[str, Any],
                    evidence_by_id: dict[str, dict[str, Any]], repo_name: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for ref in oracle.get("evidence_refs") or []:
        item = evidence_by_id.get(str(ref))
        if not item or str(item.get("repo", "")) != repo_name:
            continue
        file = str(item.get("file", ""))
        relative = PurePosixPath(file)
        if (not file or file in seen or not file.endswith(".java")
                or relative.is_absolute() or ".." in relative.parts):
            continue
        seen.add(file)
        source = snapshot / file
        if not source.is_file() or source.stat().st_size > 90_000:
            continue
        content = _SECRET.sub(
            lambda match: f'{match.group(1)}{match.group(2)}"<redacted>"',
            source.read_text(encoding="utf-8"),
        )
        if sum(map(len, parts)) + len(content) > _MAX_CONTEXT_CHARS:
            continue
        parts.append(f"文件 {file}:\n{content}")
        parent_context = _superclass_context(snapshot, file, content)
        if parent_context and sum(map(len, parts)) + len(parent_context) <= _MAX_CONTEXT_CHARS:
            parts.append(parent_context)
    return "\n\n".join(parts)


def _valid_generated_source(raw: str, class_name: str,
                            test_framework: str | None = None,
                            source_context: str = "") -> tuple[str, str]:
    parsed = json.loads(strip_code_fence(raw))
    source = str(parsed.get("source", ""))
    package = str(parsed.get("package", ""))
    if not _PACKAGE.fullmatch(package):
        raise ValueError("JSON package 不是有效的 Java 包名")
    declared = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", source)
    if declared is None:
        source = f"package {package};\n\n" + source.lstrip("\ufeff\n")
    elif declared.group(1) != package:
        raise ValueError(f"源码 package {declared.group(1)} 与 JSON package {package} 不一致")
    if not re.search(rf"\bclass\s+{re.escape(class_name)}\b", source):
        raise ValueError(f"源码类名与要求的 {class_name} 不一致")
    if len(source) > 40_000 or source.count("@Test") != 1:
        raise ValueError("生成的 Java 单测大小或测试方法数不符合约定")
    if test_framework == "junit-jupiter" and (
        "org.junit.jupiter.api.Test" not in source or "org.junit.Test" in source
        or "@RunWith" in source or "org.mockito.runners." in source
    ):
        raise ValueError("项目使用 JUnit Jupiter，但生成的单测使用了 JUnit 4")
    if test_framework == "junit4" and "org.junit.Test" not in source:
        raise ValueError("项目使用 JUnit 4，但生成的单测没有 JUnit 4 测试入口")
    forbidden = (
        "@SpringBootTest", "ProcessBuilder", "Runtime.getRuntime", "System.exit",
        "java.net.", "java.nio.file.", "java.io.", "javax.sql.",
    )
    if any(token in source for token in forbidden):
        raise ValueError("生成的单测含外部进程、网络、文件或完整应用启动操作")
    if re.search(
        r"@Mock(?:\s*\([^)]*\))?\s+(?:(?:private|public|protected|final)\s+)*"
        r"(?:java\.util\.concurrent\.)?(?:Executor|ExecutorService|ThreadPoolExecutor)\s+\w+",
        source,
    ):
        raise ValueError("异步 Executor 不能使用不执行任务的 Mockito Mock；改用 Runnable::run 或真实可关闭执行器")
    if re.search(r"\b(?:Mockito\.)?mockStatic\s*\(", source):
        raise ValueError("当前隔离环境使用 Mockito subclass MockMaker，不支持静态 mock")
    if "@InjectMocks" in source:
        raise ValueError("不能使用 @InjectMocks 隐式构造被测类；请显式传入 Mock 和同步 Executor")
    if re.search(r"\b(?:getDeclaredField|getDeclaredMethod|setAccessible)\s*\(", source):
        raise ValueError("不能通过反射修改 private 成员；请从公开入口构造测试条件")
    for match in re.finditer(r"\bverifyNoInteractions\s*\(\s*(\w+)\s*\)", source):
        dependency = re.escape(match.group(1))
        if re.search(rf"\bwhen\s*\(\s*{dependency}\s*\.", source):
            raise ValueError(f"{match.group(1)} 已被桩设定，却断言无交互；请核对真实调用链")
    if (source_context and "postForObject(" in source
            and "RestTemplate" in source_context and ".exchange(" in source_context
            and ".postForObject(" not in source_context):
        raise ValueError("被测调用链使用 RestTemplate.exchange；请按实际签名设置桩和响应对象")
    if ("class StandardRiskTrialServiceImpl" in source_context
            and "new StandardRiskTrialServiceImpl(" in source
            and "IcmGatewayExecutorProperties" in source
            and "getComplexExecutors()" not in source):
        raise ValueError("被测构造函数读取 getComplexExecutors；必须给配置 Mock 设置非空列表")
    if ("class StandardRiskTrialServiceImpl" in source_context
            and "new StandardRiskTrialServiceImpl(" in source
            and "IcmGatewayExecutorProperties" in source
            and re.search(r"\bwhen\s*\([^;]*?\.execute\s*\(", source, re.S)
            and "getComplexTimeout()" not in source):
        raise ValueError("并发试算路径须给 getComplexTimeout 设置稳定的正值，不能使用 Mock 默认 0")
    if not re.search(r"\b(?:assert\w+|verify|fail)\s*\(", source):
        raise ValueError("生成的单测缺少结果断言或调用验证")
    return package, source


async def _run_maven(
    cmd: list[str], module: Path, snapshot: Path, timeout_seconds: int,
) -> tuple[bytes, int, bool]:
    """Bound Maven and its Surefire descendants, including a blocked forked JVM."""
    process = await asyncio.create_subprocess_exec(
        *cmd, cwd=module, env=_maven_environment(module, snapshot),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout_seconds)
        return stdout, int(process.returncode or 0), False
    except asyncio.TimeoutError:
        await _stop_maven(process)
        return b"", -1, True
    except asyncio.CancelledError:
        await _stop_maven(process)
        raise


async def _stop_maven(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.communicate(), 10)
    except asyncio.TimeoutError:
        logger.warning("Maven 停止后进程管道未及时关闭：pid=%s", process.pid)


def _test_framework(module: Path, snapshot: Path) -> str | None:
    # Maven inherits dependencies from parents in ~/.m2 as well as repository POMs.
    # An inherited Spring Boot test starter makes Surefire select JUnit Platform,
    # even when the module also declares JUnit 4 directly.
    pom = module / "pom.xml"
    seen: set[Path] = set()
    dependencies: set[tuple[str, str]] = set()
    while pom.is_file() and pom not in seen:
        seen.add(pom)
        try:
            root = ET.parse(pom).getroot()
        except (ET.ParseError, OSError):
            break
        namespace = "{http://maven.apache.org/POM/4.0.0}" if root.tag.startswith("{") else ""
        def value(node: ET.Element, name: str) -> str:
            return (node.findtext(f"{namespace}{name}") or "").strip()
        for dependency in root.findall(f"{namespace}dependencies/{namespace}dependency"):
            dependencies.add((value(dependency, "groupId"), value(dependency, "artifactId")))
        parent = root.find(f"{namespace}parent")
        if parent is None:
            break
        group, artifact, version = (value(parent, field)
                                    for field in ("groupId", "artifactId", "version"))
        relative = value(parent, "relativePath") or "../pom.xml"
        local_parent = (pom.parent / relative).resolve() if relative else None
        if local_parent and local_parent.is_file():
            pom = local_parent
        elif group and artifact and version and "${" not in version:
            pom = Path.home() / ".m2/repository" / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.pom"
        else:
            break
    if any(artifact in ("spring-boot-starter-test", "junit-jupiter", "junit-jupiter-api", "junit-jupiter-engine")
           for _, artifact in dependencies):
        return "junit-jupiter"
    if ("junit", "junit") in dependencies:
        return "junit4"
    return None


def _maven_error_summary(output: str) -> str:
    lines = output.splitlines()
    errors: list[str] = []
    for index, line in enumerate(lines):
        if "[ERROR]" not in line or any(token in line for token in
                                          ("[Help", "Failed to execute goal", "COMPILATION ERROR")):
            continue
        detail = line.removeprefix("[ERROR]").strip()
        if ".java:[" in detail:
            detail = re.sub(r"^.*?([^/]+\.java:\[\d+,\d+\])", r"\1", detail)
        if detail and detail not in errors:
            errors.append(detail)
        if len(errors) >= 4:
            break
    return "；".join(errors)[:500] or "Maven 未产生测试执行记录，请查看构建日志"


def _previous_passing_source(job_dir: Path, repo_name: str, source_sha: str,
                             case_id: str, class_name: str,
                             test_framework: str) -> tuple[str, str] | None:
    """Reuse a proven test only while upstream review and cases remain unchanged."""
    upstream = [job_dir / "testcases.json", job_dir / "review_mapping.json"]
    if not all(path.is_file() for path in upstream):
        return None
    latest_upstream = max(path.stat().st_mtime for path in upstream)
    archive = job_dir / "archive" / "auto_execute"
    for path in sorted(archive.glob("*_execution.json"), reverse=True):
        if path.stat().st_mtime < latest_upstream:
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for repo in (payload.get("unit_test_coverage") or {}).get("repositories") or []:
            if repo.get("repo") != repo_name or repo.get("source_sha") != source_sha:
                continue
            for item in repo.get("cases") or []:
                if item.get("case_id") != case_id or item.get("status") != "passed":
                    continue
                source = item.get("source")
                if not isinstance(source, str):
                    continue
                declaration = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", source)
                if declaration is None:
                    continue
                try:
                    return _valid_generated_source(json.dumps({
                        "package": declaration.group(1), "source": source,
                    }), class_name, test_framework)
                except ValueError:
                    continue
    return None


def _imported_source_context(snapshot: Path, generated_source: str) -> str:
    """Show real DTO/service signatures when a generated test fails to compile."""
    parts: list[str] = []
    for name in re.findall(r"(?m)^import\s+([\w.]+)\s*;", generated_source):
        if not name.startswith("com.") or not _PACKAGE.fullmatch(name):
            continue
        relative = "/".join(name.split(".")) + ".java"
        candidates = [snapshot / "src/main/java" / relative,
                      *snapshot.glob(f"*/src/main/java/{relative}")]
        source = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source is None:
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        if len(content) > 12_000 or sum(map(len, parts)) + len(content) > 25_000:
            continue
        content = _SECRET.sub(
            lambda match: f'{match.group(1)}{match.group(2)}"<redacted>"', content,
        )
        parts.append(f"{name}:\n{content}")
    return "\n\n".join(parts)


def _maven_environment(module: Path, snapshot: Path) -> dict[str, str]:
    env = os.environ.copy()
    override = env.get("UNIT_TEST_JAVA_HOME", "")
    if override:
        if not (Path(override) / "bin" / "java").is_file():
            raise ValueError("UNIT_TEST_JAVA_HOME 不是可用的 JDK 目录")
        env["JAVA_HOME"] = override
        return env
    if env.get("JAVA_HOME"):
        return env
    # Homebrew's default JDK can be newer than the tested Maven/Lombok toolchain.
    # Only select local JDK 21 when the project's POM explicitly targets 21.
    poms = [parent / "pom.xml" for parent in (module, *module.parents)
            if parent == snapshot or snapshot in parent.parents]
    if _HOMEBREW_JAVA_21.joinpath("bin", "java").is_file() and any(
        re.search(r"<(?:source|maven\.compiler\.(?:source|release))>\s*21\s*</", pom.read_text(encoding="utf-8"))
        for pom in poms if pom.is_file()
    ):
        env["JAVA_HOME"] = str(_HOMEBREW_JAVA_21)
    return env


def _covered_evidence_lines(coverage: dict[str, Any], oracle: dict[str, Any],
                            evidence: dict[str, dict[str, Any]], repo_name: str,
                            module: Path, snapshot: Path) -> int:
    module_relative = module.relative_to(snapshot).as_posix()
    module_prefix = "" if module_relative == "." else module_relative + "/"
    expected: dict[str, list[tuple[int, int] | None]] = {}
    for ref in oracle.get("evidence_refs") or []:
        item = evidence.get(str(ref)) or {}
        file = str(item.get("file", ""))
        if item.get("repo") != repo_name or not file.startswith(module_prefix):
            continue
        _, separator, suffix = file.partition("src/main/java/")
        if separator and suffix.endswith(".java"):
            start, end = item.get("start_line"), item.get("end_line")
            span = (int(start), int(end)) if start and end else None
            expected.setdefault(suffix, []).append(span)
    return sum(
        sum(any(span is None or span[0] <= line <= span[1]
                for span in expected.get(str(item.get("path")), []))
            for line in item.get("covered_lines") or [])
        for item in coverage.get("files") or []
    )


def _unit_target_files(
    repo_name: str, interfaces: list[dict[str, Any]], review: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, Any], str, list[str]]]:
    """Find executable components on each interface's evidenced call path."""
    targets: list[tuple[dict[str, Any], str, list[str]]] = []
    chains = {str(item.get("interface_id")): item for item in review.get("call_chains") or []
              if isinstance(item, dict)}
    for interface in interfaces:
        if str((interface.get("code_location") or {}).get("repo", "")) != repo_name:
            continue
        interface_id = str(interface.get("id", ""))
        refs = {
            str(ref)
            for oracle in review.get("test_oracles") or []
            if oracle.get("interface_id") == interface_id
            for ref in oracle.get("evidence_refs") or []
        }
        refs.update(
            str(ref)
            for branch in review.get("branch_inventory") or []
            if branch.get("interface_id") == interface_id
            for ref in branch.get("source_refs") or [branch.get("evidence_ref")]
            if ref
        )
        files = {str(evidence[ref].get("file")) for ref in refs if ref in evidence
                 and evidence[ref].get("repo") == repo_name}
        chain = chains.get(interface_id) or {}
        files.update(str(item.get("file", "")) for item in
                     [chain.get("entry") or {}, *(chain.get("callees") or [])])
        for file in sorted(files):
            if not file.endswith(("Controller.java", "Validator.java", "Resolver.java",
                                  "ServiceImpl.java")):
                continue
            target_refs = sorted(ref for ref in refs if ref in evidence
                                 and evidence[ref].get("file") == file)
            if not target_refs:
                continue
            targets.append((interface, file, target_refs))
    return targets


async def _plan_source_unit_cases(
    snapshot: Path, repo_name: str, interfaces: list[dict[str, Any]],
    review: dict[str, Any], evidence: dict[str, dict[str, Any]],
    cases: list[dict[str, Any]], llm: SupportsComplete | None,
    prompts_dir: Path | None, start_index: int,
) -> list[dict[str, Any]]:
    """Plan unit scenarios from component source, independently of HTTP cases."""
    if llm is None or prompts_dir is None:
        return []
    prompt = load_prompt(prompts_dir, "unit_test_plan")
    planned: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for interface, file, target_refs in _unit_target_files(repo_name, interfaces, review, evidence):
        source_path = snapshot / file
        if not source_path.is_file() or source_path.stat().st_size > 90_000:
            continue
        source = _SECRET.sub(
            lambda match: f'{match.group(1)}{match.group(2)}"<redacted>"',
            source_path.read_text(encoding="utf-8"),
        )
        parent_context = _superclass_context(snapshot, file, source)
        interface_id = str(interface.get("id", ""))
        branches = [item for item in review.get("branch_inventory") or []
                    if item.get("interface_id") == interface_id
                    and str(item.get("evidence_ref")) in target_refs]
        branch_ids = {str(item.get("branch_id")) for item in branches}
        related_oracles = [item for item in review.get("test_oracles") or []
                           if item.get("interface_id") == interface_id
                           and set(map(str, item.get("evidence_refs") or []))
                           .intersection(target_refs)]
        request = {
            "interface": {"id": interface_id,
                          "method": (interface.get("signature") or {}).get("method"),
                          "path": (interface.get("signature") or {}).get("path"),
                          "summary": (interface.get("signature") or {}).get("summary")},
            "target": {"file": file, "class_name": source_path.stem, "source": source,
                       "superclass_source": parent_context},
            "branches": [{"branch_id": item.get("branch_id"), "quote": item.get("quote"),
                          "evidence_ref": item.get("evidence_ref")}
                         for item in branches],
            "evidence": [{"evidence_id": ref, "file": file,
                          "start_line": evidence[ref].get("start_line"),
                          "end_line": evidence[ref].get("end_line")}
                         for ref in target_refs],
            "related_oracles": [{"oracle_id": item.get("oracle_id"),
                                 "branch_id": item.get("branch_id"),
                                 "scenario": item.get("scenario")}
                                for item in related_oracles],
        }
        try:
            response = await llm.complete("unit_test_plan", prompt,
                                          json.dumps(request, ensure_ascii=False))
            raw = json.loads(strip_code_fence(response.text))
            candidates = raw.get("cases") if isinstance(raw, dict) else None
            if not isinstance(candidates, list):
                raise ValueError("单测规划缺少 cases 数组")
        except Exception as exc:
            logger.warning("单测场景规划未完成：%s %s (%s)", interface_id, file, exc)
            planned.append({"case_id": f"UT-{start_index + len(planned):03d}",
                            "interface_id": interface_id, "repo": repo_name,
                            "title": f"{source_path.stem} 单测场景规划",
                            "target_source": file, "target_class": source_path.stem,
                            "source_kind": "source_branch",
                            "linked_interface_case_ids": [],
                            "status": "generation_deferred" if _model_connection_failed(exc) else "unsupported",
                            "reason": "模型连接失败，后续源码场景规划已暂停" if _model_connection_failed(exc)
                            else f"单测场景规划未完成（{type(exc).__name__}）"})
            if _model_connection_failed(exc):
                break
            continue
        before_count = len(planned)
        for candidate in candidates[:40]:
            if not isinstance(candidate, dict):
                continue
            method = str(candidate.get("target_method", "")).strip()
            title = str(candidate.get("title", "")).strip()
            expected = str(candidate.get("expected", "")).strip()
            if not method or not title or not expected or not re.search(
                rf"\b{re.escape(method)}\s*\(", source,
            ):
                continue
            signature = (interface_id, file, title)
            if signature in seen:
                continue
            seen.add(signature)
            selected_branches = [str(value) for value in candidate.get("branch_ids") or []
                                 if str(value) in branch_ids]
            selected_refs = [str(value) for value in candidate.get("evidence_refs") or []
                             if str(value) in target_refs]
            if not selected_refs:
                selected_refs = target_refs[:1]
            linked_oracles = {
                str(oracle_id)
                for branch in branches
                if str(branch.get("branch_id")) in selected_branches
                for oracle_id in branch.get("oracle_ids") or []
            }
            linked_oracles.update(str(oracle.get("oracle_id")) for oracle in related_oracles
                                  if str(oracle.get("branch_id")) in selected_branches)
            if (not linked_oracles and not selected_branches
                    and file == str((interface.get("code_location") or {}).get("file"))):
                linked_oracles.update(str(oracle.get("oracle_id")) for oracle in related_oracles
                                      if oracle.get("oracle_basis") == "integration_entry")
            linked_ids = sorted(str(case.get("id")) for case in cases
                                if case.get("interface_id") == interface_id
                                and str(case.get("oracle_id")) in linked_oracles)
            linked_cases = [case for case in cases if str(case.get("id")) in linked_ids]
            gap_kind = (
                "mock_or_config" if any(
                    case.get("oracle_control_kinds") or
                    case.get("execution_readiness") == "conditional"
                    for case in linked_cases
                ) else "component_detail" if linked_ids else "no_http_case"
            )
            planned.append({
                "case_id": f"UT-{start_index + len(planned):03d}",
                "interface_id": interface_id, "repo": repo_name,
                "title": title, "target_source": file,
                "target_class": source_path.stem, "target_method": method,
                "scenario_type": str(candidate.get("scenario_type", "normal")),
                "request_params": candidate.get("request_params")
                    if isinstance(candidate.get("request_params"), dict) else {},
                "mock_conditions": [str(value) for value in
                                    candidate.get("mock_conditions") or []]
                    if isinstance(candidate.get("mock_conditions"), list) else [],
                "expected": expected,
                "branch_ids": selected_branches,
                "evidence_refs": selected_refs,
                "linked_interface_case_ids": linked_ids,
                "gap_kind": gap_kind,
                "gap_reason": str(candidate.get("unit_reason", "")).strip(),
                "source_kind": "source_branch",
            })
        if len(planned) == before_count:
            planned.append({"case_id": f"UT-{start_index + len(planned):03d}",
                            "interface_id": interface_id, "repo": repo_name,
                            "title": f"{source_path.stem} 单测场景规划",
                            "target_source": file, "target_class": source_path.stem,
                            "source_kind": "source_branch",
                            "linked_interface_case_ids": [], "status": "unsupported",
                            "reason": "目标源码未形成可验证的单测场景"})
    return planned


async def _run_generated_suite_coverage(
    snapshot: Path, module: Path,
    tests: list[dict[str, str]],
) -> dict[str, Any]:
    """Measure a module's generated tests together; keep module/class counters distinct."""
    targets: list[Path] = []
    class_names: list[str] = []
    for test in tests:
        source = test["source"]
        package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", source)
        if package_match is None:
            continue
        target = (module / "src/test/java" /
                  Path(*package_match.group(1).split(".")) /
                  f"{test['test_class']}.java")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        targets.append(target)
        class_names.append(test["test_class"])
    if not targets:
        return {"status": "unavailable", "reason": "没有可合并执行的生成单测"}
    coverage_path = module / "target/site/jacoco/jacoco.xml"
    try:
        for stale in (module / "target/jacoco.exec", coverage_path):
            stale.unlink(missing_ok=True)
        for class_name in class_names:
            for stale in (module / "target/surefire-reports").glob(f"TEST-*.{class_name}.xml"):
                stale.unlink(missing_ok=True)
        cmd = ["mvn", "-B", "-o", f"-Dtest={','.join(class_names)}",
               "-DfailIfNoTests=true", "-Dsurefire.failIfNoSpecifiedTests=true",
               "-Dmaven.test.failure.ignore=true",
               f"{_JACOCO_GOAL}:prepare-agent", "test", f"{_JACOCO_GOAL}:report"]
        stdout, _, timed_out = await _run_maven(
            cmd, module, snapshot, _SUITE_MAVEN_TIMEOUT_SECONDS,
        )
        if timed_out:
            return {"status": "unavailable", "reason": "生成单测合跑超时"}
        outcomes = [_surefire_result(module, class_name) for class_name in class_names]
        case_outcomes = [
            {"case_id": test["case_id"], "status": outcome["status"],
             "reason": outcome.get("reason", "")}
            for test, outcome in zip(tests, outcomes)
        ]
        ran = sum(int(item.get("tests", 0)) - int(item.get("skipped", 0))
                  for item in outcomes)
        if ran <= 0 or not coverage_path.is_file():
            return {"status": "unavailable", "reason": "合跑未产生有效 Surefire/JaCoCo 记录",
                    "build_error": _maven_error_summary(stdout.decode("utf-8", errors="replace")),
                    "case_outcomes": case_outcomes}
        parsed = parse_jacoco_xml(coverage_path, include_missed=True)
        wanted: dict[str, set[str]] = {}
        for test in tests:
            _, separator, relative = test["target_source"].partition("src/main/java/")
            if separator and relative.endswith(".java"):
                wanted.setdefault(relative.removesuffix(".java").replace("/", "."), set()).add(
                    test["case_id"]
                )
        file_by_path = {str(item["path"]): item for item in parsed["files"]}
        classes: list[dict[str, Any]] = []
        for item in parsed["classes"]:
            if item["name"] not in wanted:
                continue
            file_item = file_by_path.get(str(item["source_file"])) or {}
            classes.append({**item, "case_ids": sorted(wanted[item["name"]]),
                            "missed_lines": (file_item.get("missed_lines") or [])[:50]})
        if not classes:
            return {"status": "unavailable", "reason": "JaCoCo 未找到生成单测对应的目标类",
                    "case_outcomes": case_outcomes}
        def total(kind: str) -> dict[str, Any] | None:
            counters = [item[kind] for item in classes if item.get(kind)]
            if not counters:
                return None
            covered = sum(int(item["covered"]) for item in counters)
            missed = sum(int(item["missed"]) for item in counters)
            return {"covered": covered, "missed": missed,
                    "rate": covered / (covered + missed) if covered + missed else None}
        failures = sum(int(item.get("failures", 0)) + int(item.get("errors", 0))
                       for item in outcomes)
        return {"status": "partial" if failures else "completed",
                "scope": "generated_tests_only", "tests_executed": ran,
                "tests_failed": failures, "classes": classes,
                "case_outcomes": case_outcomes,
                "line_coverage": total("line_coverage"),
                "branch_coverage": total("branch_coverage")}
    except (OSError, ET.ParseError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"合跑覆盖率不可用（{type(exc).__name__}）"}
    finally:
        for target in targets:
            target.unlink(missing_ok=True)


async def _run_existing_test_baseline(
    repo: dict[str, Any], job_dir: Path, target_sources: set[str],
) -> dict[str, Any]:
    """Run exact existing *Test.java classes separately from generated UT cases."""
    source_repo = Path(str(repo.get("path", ""))).resolve()
    if not _working_tree_matches_source(repo):
        return {"status": "unavailable", "reason": "工作区源码提交或生产代码与目标提交不一致，无法引用本地测试"}
    selected: dict[str, list[tuple[str, str]]] = {}
    for source_file in sorted(target_sources):
        test_file = _existing_test_path(source_file)
        if test_file is None or not (source_repo / test_file).is_file():
            continue
        module_name = source_file.split("/src/main/java/", 1)[0]
        if module_name == source_file:
            continue
        selected.setdefault(module_name, []).append((source_file, test_file))
    if not selected:
        return {"status": "unavailable", "reason": "目标类没有对应的现有 Java 测试文件"}
    snapshot = await asyncio.to_thread(_snapshot, repo, job_dir / ".unit-baseline")
    modules: list[dict[str, Any]] = []
    for module_name, paths in selected.items():
        module = snapshot / module_name
        if not (module / "pom.xml").is_file():
            modules.append({"module": module_name, "status": "unavailable", "reason": "Maven 模块不存在"})
            continue
        imported: list[dict[str, str]] = []
        pom = source_repo / module_name / "pom.xml"
        pom_provenance = "pinned_commit"
        if pom.is_file() and pom.read_bytes() != (module / "pom.xml").read_bytes():
            shutil.copyfile(pom, module / "pom.xml")
            pom_provenance = "working_tree_uncommitted"
        for source_file, test_file in paths:
            original = source_repo / test_file
            target = snapshot / test_file
            target.parent.mkdir(parents=True, exist_ok=True)
            original_bytes = original.read_bytes()
            committed_bytes = target.read_bytes() if target.is_file() else None
            target.write_bytes(original_bytes)
            imported.append({
                "target_source": source_file, "test_file": test_file,
                "test_class": original.stem,
                "provenance": "pinned_commit" if original_bytes == committed_bytes else "working_tree_uncommitted",
                "sha256": hashlib.sha256(original_bytes).hexdigest(),
            })
        class_names = [item["test_class"] for item in imported]
        coverage_path = module / "target/site/jacoco/jacoco.xml"
        for stale in (module / "target/jacoco.exec", coverage_path):
            stale.unlink(missing_ok=True)
        for class_name in class_names:
            for stale in (module / "target/surefire-reports").glob(f"TEST-*.{class_name}.xml"):
                stale.unlink(missing_ok=True)
        command = ["mvn", "-B", "-o", f"-Dtest={','.join(class_names)}",
                   "-DfailIfNoTests=true", "-Dsurefire.failIfNoSpecifiedTests=true",
                   "-Dmaven.test.failure.ignore=true", f"{_JACOCO_GOAL}:prepare-agent",
                   "test", f"{_JACOCO_GOAL}:report"]
        try:
            stdout, _, timed_out = await _run_maven(command, module, snapshot, _SUITE_MAVEN_TIMEOUT_SECONDS)
            outcomes = [_surefire_result(module, class_name) for class_name in class_names]
            tests_executed = sum(max(0, int(row.get("tests", 0)) - int(row.get("skipped", 0))) for row in outcomes)
            tests_failed = sum(int(row.get("failures", 0)) + int(row.get("errors", 0)) for row in outcomes)
            record: dict[str, Any] = {
                "module": module_name, "tests": imported, "pom_provenance": pom_provenance,
                "pom_sha256": hashlib.sha256((module / "pom.xml").read_bytes()).hexdigest(),
                "tests_executed": tests_executed, "tests_failed": tests_failed,
                "status": "unavailable" if timed_out or tests_executed == 0 else
                    ("partial" if tests_failed else "completed"),
                "test_outcomes": [dict(test, **outcome) for test, outcome in zip(imported, outcomes)],
            }
            if record["status"] == "unavailable":
                record["reason"] = "现有单测超时" if timed_out else _maven_error_summary(
                    stdout.decode("utf-8", errors="replace"))
            if coverage_path.is_file() and tests_executed:
                parsed = parse_jacoco_xml(coverage_path, include_missed=True)
                wanted = {item["target_source"].partition("src/main/java/")[2]
                          .removesuffix(".java").replace("/", ".") for item in imported}
                record["classes"] = [item for item in parsed["classes"] if item["name"] in wanted]
            modules.append(record)
        except (OSError, ET.ParseError, ValueError) as exc:
            modules.append({"module": module_name, "status": "unavailable",
                            "tests": imported, "reason": f"现有单测运行记录不可用（{type(exc).__name__}）"})
    return {"status": "completed" if any(item.get("tests_executed") for item in modules)
            else "unavailable", "scope": "existing_tests_separate",
            "source_sha": repo.get("target_sha"), "modules": modules,
            "tests_executed": sum(int(item.get("tests_executed", 0)) for item in modules),
            "tests_failed": sum(int(item.get("tests_failed", 0)) for item in modules)}


async def generate_controlled_unit_cases(
    store: ArtifactStore, job_id: str, cases: list[dict[str, Any]],
    interfaces: list[dict[str, Any]], review: dict[str, Any],
    inventory: dict[str, Any], llm: SupportsComplete | None,
    prompts_dir: Path | None,
) -> list[dict[str, Any]]:
    """Prepare reviewable Java unit tests before any HTTP execution occurs."""
    by_oracle = {str(item.get("oracle_id")): item for item in review.get("test_oracles") or []
                 if isinstance(item, dict)}
    by_interface = {str(item.get("id")): item for item in interfaces}
    by_repo = {str(item.get("name")): item for item in inventory.get("repositories") or []
               if isinstance(item, dict)}
    evidence = {str(item.get("evidence_id")): item for item in review.get("evidence_catalog") or []
                if isinstance(item, dict)}
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    source_plans: dict[str, dict[str, Any]] = {}
    for case in cases:
        oracle = by_oracle.get(str(case.get("oracle_id")))
        interface = by_interface.get(str(case.get("interface_id")), {})
        if case.get("oracle_status") != "verified" or not oracle:
            continue
        if not (required_control_kinds(oracle) or oracle.get("control_kinds")
                or oracle.get("execution_readiness") == "conditional"):
            continue
        repo_name = str((interface.get("code_location") or {}).get("repo", ""))
        if repo_name in by_repo and str(by_repo[repo_name].get("language") or "java").lower() == "java":
            groups.setdefault(repo_name, []).append((case, oracle))
    for interface in interfaces:
        repo_name = str((interface.get("code_location") or {}).get("repo", ""))
        if repo_name in by_repo and str(by_repo[repo_name].get("language") or "java").lower() == "java":
            groups.setdefault(repo_name, [])

    planned: list[dict[str, Any]] = []
    next_unit_index = 1
    model_unavailable = False
    prompt = load_prompt(prompts_dir, "unit_test_generate") if llm and prompts_dir and groups else None
    for repo_name, entries in groups.items():
        repo = by_repo[repo_name]
        try:
            snapshot = await asyncio.to_thread(_snapshot, repo, store.job_dir(job_id))
        except Exception as exc:
            planned.extend({"case_id": str(case["id"]), "interface_id": str(case.get("interface_id", "")),
                            "repo": repo_name, "status": "unsupported",
                            "reason": f"隔离源码快照不可用（{type(exc).__name__}）"}
                           for case, _ in entries)
            continue
        repo_source_plans = await _plan_source_unit_cases(
            snapshot, repo_name, interfaces, review, evidence, cases, llm,
            prompts_dir, next_unit_index,
        )
        if any(item.get("status") == "generation_deferred" for item in repo_source_plans):
            model_unavailable = True
        next_unit_index += len(repo_source_plans)
        for source_plan in repo_source_plans:
            case_id = str(source_plan["case_id"])
            if source_plan.get("status") in ("unsupported", "generation_deferred"):
                planned.append({**source_plan, "source_sha": str(repo.get("target_sha", ""))})
                continue
            source_plans[case_id] = source_plan
            entries.append((
                {"id": case_id, "interface_id": source_plan["interface_id"],
                 "title": source_plan["title"],
                 "params": source_plan["request_params"],
                 "state_conditions": source_plan["mock_conditions"],
                 "expected": source_plan["expected"]},
                {"evidence_refs": source_plan["evidence_refs"],
                 "branch_id": source_plan["branch_ids"][0]
                     if source_plan["branch_ids"] else None},
            ))
        for case, oracle in entries:
            case_id = str(case["id"])
            class_name = "AutoUnit" + re.sub(r"\W", "", case_id) + "Test"
            item: dict[str, Any] = {
                "case_id": case_id, "interface_id": str(case.get("interface_id", "")),
                "oracle_id": str(case.get("oracle_id", "")), "title": str(case.get("title", "")),
                "repo": repo_name, "source_sha": str(repo.get("target_sha", "")),
                "test_class": class_name, "request_params": redact_secrets(case.get("params") or {}),
                "mock_conditions": case.get("state_conditions") or [],
                "expected": case.get("expected_behavior") or case.get("expected"),
                "status": "unsupported",
            }
            if case_id in source_plans:
                item.update(source_plans[case_id])
            else:
                item.update({
                    "source_kind": "interface_control",
                    "linked_interface_case_ids": [case_id],
                    "gap_kind": "mock_or_config",
                    "gap_reason": "接口测试需要受控下游响应或配置；用单测隔离该依赖",
                    "branch_ids": [str(oracle["branch_id"])] if oracle.get("branch_id") else [],
                    "evidence_refs": [str(ref) for ref in oracle.get("evidence_refs") or []],
                })
            planned.append(item)
            first_ref = next((evidence.get(str(ref)) for ref in oracle.get("evidence_refs") or []
                              if evidence.get(str(ref), {}).get("file", "").endswith(".java")
                              and evidence.get(str(ref), {}).get("repo") == repo_name), None)
            if first_ref:
                item["target_source"] = str(first_ref.get("file", ""))
                item.setdefault("target_class", Path(str(first_ref.get("file", ""))).stem)
            module = _module_root(snapshot, str((first_ref or {}).get("file", "")))
            if module is None:
                item["reason"] = "未找到对应的 Maven 模块"
                continue
            item["module"] = module.relative_to(snapshot).as_posix()
            framework = _test_framework(module, snapshot)
            if framework is None:
                item["reason"] = "未识别项目使用的 JUnit 测试框架"
                continue
            item["test_framework"] = framework
            context = _source_context(snapshot, oracle, evidence, repo_name)
            if not context:
                item["reason"] = "缺少可引用的 Java 源码证据"
                continue
            if prompt is None or llm is None:
                item.update(status="generation_failed", reason="未配置单元测试生成模型")
                continue
            request = {"case_id": case_id, "class_name": class_name,
                       "test_framework": framework, "case": redact_secrets(case),
                       "oracle": oracle, "source": context,
                       "pom": (module / "pom.xml").read_text(encoding="utf-8")[:12_000]}
            reference = _existing_test_reference(repo, str((first_ref or {}).get("file", "")))
            if reference:
                request["existing_test_reference"] = reference
            if case_id in source_plans:
                request["unit_plan"] = redact_secrets(source_plans[case_id])
            if model_unavailable:
                item.update(status="generation_deferred", reason="模型连接失败，本轮暂停后续单测生成")
                continue
            try:
                answer = await llm.complete("unit_test_generate", prompt,
                                            json.dumps(request, ensure_ascii=False))
                try:
                    package, source = _valid_generated_source(answer.text, class_name, framework, context)
                except ValueError as validation_error:
                    repaired = await llm.complete("unit_test_generate", prompt,
                                                  json.dumps({**request,
                                                              "validation_error": str(validation_error),
                                                              "previous_output": answer.text}, ensure_ascii=False))
                    package, source = _valid_generated_source(repaired.text, class_name, framework, context)
                target = module / "src/test/java" / Path(*package.split(".")) / f"{class_name}.java"
                if target.exists():
                    raise ValueError("目标提交已有同名测试类")
                artifact = f"scripts/unit_{case_id}.java"
                store.write_raw(job_id, artifact, source)
                item.update(status="generated", source_artifact=artifact, source=source)
            except Exception as exc:
                if _model_connection_failed(exc):
                    model_unavailable = True
                    item.update(status="generation_deferred", reason="模型连接失败，已暂停后续单测生成")
                else:
                    item.update(status="generation_failed",
                                reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    return planned


async def run_controlled_unit_coverage(
    store: ArtifactStore, job_id: str, cases: list[dict[str, Any]],
    results: list[dict[str, Any]], interfaces: list[dict[str, Any]],
    review: dict[str, Any], inventory: dict[str, Any],
    llm: SupportsComplete | None, prompts_dir: Path | None,
    planned_cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run prepared unit cases for blocked HTTP scenarios; retain legacy jobs without a plan."""
    by_oracle = {str(item.get("oracle_id")): item for item in review.get("test_oracles") or []
                 if isinstance(item, dict)}
    by_interface = {str(item.get("id")): item for item in interfaces}
    by_repo = {str(item.get("name")): item for item in inventory.get("repositories") or []
               if isinstance(item, dict)}
    by_result = {str(item.get("case_id")): item for item in results}
    planned_by_id = {str(item.get("case_id")): item for item in planned_cases or []
                     if isinstance(item, dict)}
    evidence = {str(item.get("evidence_id")): item for item in review.get("evidence_catalog") or []
                if isinstance(item, dict)}
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    if planned_cases is not None:
        case_by_id = {str(case.get("id")): case for case in cases}
        for planned in planned_cases:
            case_id = str(planned.get("case_id", ""))
            repo_name = str(planned.get("repo", ""))
            if repo_name not in by_repo or not case_id:
                continue
            case = case_by_id.get(case_id) or {
                "id": case_id, "interface_id": planned.get("interface_id"),
                "title": planned.get("title"),
                "params": planned.get("request_params") or {},
                "expected": planned.get("expected"),
            }
            oracle = by_oracle.get(str(planned.get("oracle_id"))) or {
                "evidence_refs": planned.get("evidence_refs") or [],
                "branch_id": (planned.get("branch_ids") or [None])[0],
            }
            groups.setdefault(repo_name, []).append((case, oracle))
    else:
        for case in cases:
            result = by_result.get(str(case.get("id")))
            oracle = by_oracle.get(str(case.get("oracle_id")), {})
            interface = by_interface.get(str(case.get("interface_id")), {})
            if not result or not (
                result.get("execution_state") == "not_executed"
                or result.get("failure_kind") == "environment"
            ):
                continue
            if case.get("oracle_status") != "verified" or not oracle:
                continue
            if not (required_control_kinds(oracle) or oracle.get("control_kinds")
                    or oracle.get("execution_readiness") == "conditional"
                    or result.get("missing_control_kinds")):
                continue
            repo_name = str((interface.get("code_location") or {}).get("repo", ""))
            if repo_name in by_repo and str(by_repo[repo_name].get("language") or "java").lower() == "java":
                groups.setdefault(repo_name, []).append((case, oracle))
    output: dict[str, Any] = {"status": "not_applicable", "repositories": [],
                              "cases_total": sum(map(len, groups.values())),
                              "cases_passed": 0, "cases_failed": 0}
    if not groups:
        return output
    if planned_cases is None and (llm is None or prompts_dir is None):
        output["status"] = "unavailable"
        output["reason"] = "未配置单元测试生成模型"
        return output
    prompt = load_prompt(prompts_dir, "unit_test_generate") if llm and prompts_dir else None
    model_unavailable = False
    for repo_name, entries in groups.items():
        repo = by_repo[repo_name]
        repo_result: dict[str, Any] = {"repo": repo_name, "source_sha": repo.get("target_sha"),
                                       "status": "unavailable", "cases": []}
        output["repositories"].append(repo_result)
        try:
            target_sources = {
                str(plan.get("target_source")) for plan in planned_cases or []
                if plan.get("repo") == repo_name and plan.get("target_source")
            }
            for _, oracle in entries:
                target_sources.update(
                    str(evidence[str(ref)].get("file")) for ref in oracle.get("evidence_refs") or []
                    if str(ref) in evidence and evidence[str(ref)].get("repo") == repo_name
                )
            try:
                repo_result["existing_suite"] = await _run_existing_test_baseline(
                    repo, store.job_dir(job_id), target_sources)
            except Exception as baseline_error:
                logger.warning("现有单测基线不可用：%s (%s)", repo_name, baseline_error)
                repo_result["existing_suite"] = {"status": "unavailable",
                    "reason": f"现有单测基线不可用（{type(baseline_error).__name__}）"}
            snapshot = await asyncio.to_thread(_snapshot, repo, store.job_dir(job_id))
            generated_count = 0
            suite_tests: dict[str, list[dict[str, str]]] = {}
            for case, oracle in entries:
                case_id = str(case["id"])
                planned = planned_by_id.get(case_id) if planned_cases is not None else None
                run_source_artifact = (f"scripts/unit_{case_id}.executed.java" if planned is not None
                                       else f"scripts/unit_{case_id}.java")
                if planned is not None and planned.get("status") != "generated":
                    repo_result["cases"].append({
                        "case_id": case_id,
                        "status": planned.get("status", "generation_failed"),
                        "reason": planned.get("reason", "用例生成阶段未产生可运行的 Java 单测"),
                    })
                    continue
                if planned is not None and planned.get("source_sha") != repo.get("target_sha"):
                    repo_result["cases"].append({"case_id": case_id, "status": "unsupported",
                                                 "reason": "生成单测的源码提交与当前执行提交不一致；需从用例生成重跑"})
                    continue
                class_name = "AutoUnit" + re.sub(r"\W", "", case_id) + "Test"
                first_ref = next((evidence.get(str(ref)) for ref in oracle.get("evidence_refs") or []
                                  if evidence.get(str(ref), {}).get("file", "").endswith(".java")
                                  and evidence.get(str(ref), {}).get("repo") == repo_name), None)
                module = _module_root(snapshot, str((first_ref or {}).get("file", "")))
                if module is None:
                    repo_result["cases"].append({"case_id": case_id, "status": "unsupported",
                                                 "reason": "未找到对应的 Maven 模块"})
                    continue
                if planned is not None and planned.get("module") != module.relative_to(snapshot).as_posix():
                    repo_result["cases"].append({"case_id": case_id, "status": "unsupported",
                                                 "reason": "生成单测的 Maven 模块与当前源码证据不一致"})
                    continue
                test_framework = _test_framework(module, snapshot)
                if test_framework is None:
                    repo_result["cases"].append({"case_id": case_id, "status": "unsupported",
                                                 "reason": "未识别项目使用的 JUnit 测试框架"})
                    continue
                context = _source_context(snapshot, oracle, evidence, repo_name)
                if not context:
                    repo_result["cases"].append({"case_id": case_id, "status": "unsupported",
                                                 "reason": "缺少可引用的 Java 源码证据"})
                    continue
                request = {"case_id": case_id, "class_name": class_name,
                           "test_framework": test_framework,
                           "case": redact_secrets(case), "oracle": oracle, "source": context,
                           "pom": (module / "pom.xml").read_text(encoding="utf-8")[:12_000]}
                reference = _existing_test_reference(repo, str((first_ref or {}).get("file", "")))
                if reference:
                    request["existing_test_reference"] = reference
                if planned is not None and planned.get("source_kind") == "source_branch":
                    request["unit_plan"] = redact_secrets(planned)
                try:
                    previous = None
                    if planned is not None:
                        planned_source = str(planned.get("source", ""))
                        declaration = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", planned_source)
                        if declaration is None:
                            raise ValueError("用例生成产物缺少 Java package 声明")
                        package, source = _valid_generated_source(json.dumps({
                            "package": declaration.group(1), "source": planned_source,
                        }), class_name, test_framework, context)
                    else:
                        previous = _previous_passing_source(
                            store.job_dir(job_id), repo_name, str(repo.get("target_sha")),
                            case_id, class_name, test_framework,
                        )
                        if previous is not None:
                            package, source = previous
                        else:
                            if model_unavailable:
                                raise PipelineError(X_LLM_01, "模型连接失败，本轮暂停后续单测生成")
                            answer = await llm.complete("unit_test_generate", prompt,
                                                    json.dumps(request, ensure_ascii=False))
                            try:
                                package, source = _valid_generated_source(answer.text, class_name,
                                                                          test_framework, context)
                            except ValueError as validation_error:
                                repair_request = {**request, "validation_error": str(validation_error),
                                                  "previous_output": answer.text}
                                answer = await llm.complete("unit_test_generate", prompt,
                                                            json.dumps(repair_request, ensure_ascii=False))
                                package, source = _valid_generated_source(answer.text, class_name,
                                                                          test_framework, context)
                    relative = Path(*package.split(".")) / f"{class_name}.java"
                    target = module / "src" / "test" / "java" / relative
                    if target.exists():
                        raise ValueError("目标提交已有同名测试类")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(source, encoding="utf-8")
                    store.write_raw(job_id, run_source_artifact, source)
                except Exception as exc:  # model failure belongs to this unit candidate
                    if _model_connection_failed(exc):
                        model_unavailable = True
                    repo_result["cases"].append({
                        "case_id": case_id,
                        "status": "generation_deferred" if _model_connection_failed(exc)
                            else "generation_failed",
                        "reason": "模型连接失败，本轮暂停后续单测生成" if _model_connection_failed(exc)
                            else str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                    })
                    continue
                generated_count += 1
                mock_maker = module / "src/test/resources/mockito-extensions/org.mockito.plugins.MockMaker"
                if not mock_maker.exists():
                    mock_maker.parent.mkdir(parents=True, exist_ok=True)
                    mock_maker.write_text("mock-maker-subclass\n", encoding="utf-8")
                coverage_path = module / "target" / "site" / "jacoco" / "jacoco.xml"
                cmd = ["mvn", "-B", "-o", f"-Dtest={class_name}",
                       "-DfailIfNoTests=true", "-Dsurefire.failIfNoSpecifiedTests=true",
                       f"{_JACOCO_GOAL}:prepare-agent", "test", f"{_JACOCO_GOAL}:report"]
                log_artifact = f"scripts/unit_{case_id}.log"
                run_logs: list[str] = []
                coverage: dict[str, Any] | None = None
                outcome: dict[str, Any] = {"status": "not_run", "reason": "尚未执行"}
                last_executed: tuple[dict[str, Any], str] | None = None
                for attempt in range(3):
                    for stale in (module / "target/jacoco.exec", coverage_path):
                        stale.unlink(missing_ok=True)
                    for stale in (module / "target/surefire-reports").glob(f"TEST-*.{class_name}.xml"):
                        stale.unlink(missing_ok=True)
                    stdout = b""
                    timed_out = False
                    try:
                        stdout, exit_code, timed_out = await _run_maven(
                            cmd, module, snapshot, _CASE_MAVEN_TIMEOUT_SECONDS,
                        )
                    except OSError:
                        exit_code = -1
                    output_text = redact_secrets(stdout.decode("utf-8", errors="replace"))
                    timeout_detail = (f"单测运行超过 {_CASE_MAVEN_TIMEOUT_SECONDS} 秒，"
                                      "已终止 Maven 和 Surefire 子进程") if timed_out else ""
                    run_logs.append(f"=== Maven 尝试 {attempt + 1} ===\n"
                                    f"{timeout_detail}\n{output_text[-12_000:]}")
                    outcome = _surefire_result(module, class_name)
                    if outcome["status"] in ("passed", "failed", "fixture_failed"):
                        last_executed = (outcome.copy(), source)
                    if timed_out:
                        outcome = {"status": "timed_out", "reason": timeout_detail}
                        break
                    if outcome["status"] == "fixture_failed":
                        detail = outcome["reason"]
                    elif exit_code != 0 and outcome["status"] == "not_run":
                        detail = _maven_error_summary(output_text)
                        outcome = {"status": "build_failed", "reason": detail}
                    elif exit_code == 0 and outcome["status"] == "not_run":
                        detail = "Maven 构建成功，但 Surefire 没有发现生成的测试方法"
                        outcome["reason"] = detail
                    else:
                        break
                    if attempt == 2 or exit_code == -1:
                        break
                    if llm is None or prompt is None:
                        outcome["reason"] += "；未配置生成模型，无法根据编译错误修复"
                        break
                    if model_unavailable:
                        outcome = {**outcome, "status": "repair_deferred",
                                   "reason": f"{detail}；模型连接失败，本轮暂停自动修复"}
                        break
                    # Feed the actual compiler or discovery error back to the
                    # generator; otherwise one bad import leaves the case untested.
                    repair_request = {**request, "compilation_error": detail,
                                      "related_types": _imported_source_context(snapshot, source),
                                      "previous_output": json.dumps({"package": package,
                                                                       "source": source}, ensure_ascii=False)}
                    try:
                        repaired = await llm.complete("unit_test_generate", prompt,
                                                      json.dumps(repair_request, ensure_ascii=False))
                        package, source = _valid_generated_source(repaired.text, class_name,
                                                                  test_framework, context)
                        if package != ".".join(relative.parts[:-1]):
                            raise ValueError("修复后的 package 与测试文件路径不一致")
                        target.write_text(source, encoding="utf-8")
                        store.write_raw(job_id, run_source_artifact, source)
                    except Exception as repair_error:
                        if _model_connection_failed(repair_error):
                            model_unavailable = True
                            outcome = {**outcome, "status": "repair_deferred",
                                       "reason": f"{detail}；模型连接失败，本轮暂停自动修复"}
                        else:
                            outcome["reason"] += f"；自动修复失败：{type(repair_error).__name__}"
                        break
                if outcome["status"] == "build_failed" and last_executed is not None:
                    outcome, source = last_executed
                    outcome = {**outcome, "reason": outcome.get("reason", "")
                               + "；后续自动修复未能编译，保留前一次执行结果"}
                    store.write_raw(job_id, run_source_artifact, source)
                store.write_raw(job_id, log_artifact, "\n\n".join(run_logs)[-36_000:])
                if coverage_path.is_file() and outcome["status"] in ("passed", "failed"):
                    try:
                        coverage = parse_jacoco_xml(coverage_path)
                        repo_result.setdefault("modules", []).append({
                            "module": module.relative_to(snapshot).as_posix(),
                            "case_id": case_id,
                            "line_coverage": coverage,
                        })
                    except (ET.ParseError, ValueError, OSError):
                        repo_result["coverage_warning"] = "JaCoCo XML 不可读"
                if outcome["status"] == "passed":
                    hit_lines = _covered_evidence_lines(coverage or {}, oracle, evidence,
                                                        repo_name, module, snapshot)
                    if not hit_lines:
                        outcome = {**outcome, "status": "source_not_covered",
                                   "reason": "单测通过，但 JaCoCo 未记录目标源码证据行被执行"}
                    else:
                        outcome["covered_evidence_lines"] = hit_lines
                repo_result["cases"].append({
                    "case_id": case_id, "test_class": class_name,
                    "source_artifact": run_source_artifact,
                    "log_artifact": log_artifact,
                    "reused_verified_source": previous is not None,
                    "source": source, **outcome,
                })
                if outcome["status"] in ("passed", "failed", "source_not_covered"):
                    suite_tests.setdefault(module.relative_to(snapshot).as_posix(), []).append({
                        "case_id": case_id, "test_class": class_name,
                        "target_source": str(planned.get("target_source", "")) if planned
                            else str((first_ref or {}).get("file", "")),
                        "source": source,
                    })
                target.unlink(missing_ok=True)
            suite_modules: list[dict[str, Any]] = []
            for module_name, test_sources in suite_tests.items():
                module = snapshot / module_name
                summary = await _run_generated_suite_coverage(snapshot, module, test_sources)
                suite_modules.append({"module": module_name, **summary})
                by_case = {str(item.get("case_id")): item
                           for item in summary.get("case_outcomes") or []}
                for item in repo_result["cases"]:
                    suite_result = by_case.get(str(item.get("case_id")))
                    if suite_result is None:
                        continue
                    item["suite_status"] = suite_result["status"]
                    if suite_result.get("reason"):
                        item["suite_reason"] = suite_result["reason"]
                    isolated = ("passed" if item["status"] == "source_not_covered"
                                else item["status"])
                    if suite_result["status"] != isolated:
                        item["consistency_warning"] = (
                            f"逐条执行为 {isolated}，合跑为 {suite_result['status']}；"
                            "需复跑核对测试隔离或时序条件"
                        )
            if suite_modules:
                repo_result["suite_coverage"] = {"scope": "generated_tests_only",
                                                  "modules": suite_modules}
            repo_result["status"] = "completed" if generated_count else "unavailable"
        except Exception as exc:
            logger.exception("隔离单元测试执行失败：%s", repo_name)
            repo_result["reason"] = f"隔离源码快照或 Maven 工程不可用（{type(exc).__name__}）"
            for case, _ in entries:
                if not any(item["case_id"] == case["id"] for item in repo_result["cases"]):
                    repo_result["cases"].append({"case_id": case["id"], "status": "unsupported",
                                                 "reason": "隔离源码快照或 Maven 工程不可用"})
        if planned_cases is not None:
            for item in repo_result["cases"]:
                plan = planned_by_id.get(str(item.get("case_id"))) or {}
                for key in ("interface_id", "title", "target_source", "target_class",
                            "target_method", "source_kind", "scenario_type", "branch_ids",
                            "evidence_refs", "linked_interface_case_ids", "gap_kind",
                            "gap_reason", "request_params", "mock_conditions", "expected"):
                    if key in plan:
                        item[key] = plan[key]
    case_results = {item["case_id"]: item for repo in output["repositories"]
                    for item in repo["cases"]}
    for result in results:
        unit = case_results.get(str(result.get("case_id")))
        if unit:
            result["unit_test"] = unit
    output["cases_passed"] = sum(item["status"] == "passed" for item in case_results.values())
    output["cases_deferred"] = sum(item["status"] in ("generation_deferred", "repair_deferred")
                                   for item in case_results.values())
    output["cases_failed"] = output["cases_total"] - output["cases_passed"]
    output["status"] = "completed" if output["repositories"] else "unavailable"
    return output
