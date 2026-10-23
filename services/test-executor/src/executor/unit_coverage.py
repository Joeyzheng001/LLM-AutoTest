"""Run generated mock based Java unit tests in a pinned, isolated Git snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from common.artifacts import ArtifactStore
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
_MAVEN_TIMEOUT_SECONDS = 900
_HOMEBREW_JAVA_21 = Path("/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home")
logger = logging.getLogger(__name__)


class SupportsComplete(Protocol):
    async def complete(self, node: str, prompt: PromptSpec, user_content: str,
                       max_attempts: int | None = None) -> LlmResult: ...


def parse_jacoco_xml(path: Path) -> dict[str, Any]:
    """Use JaCoCo's LINE counter; source line details support honest drill-down."""
    root = ET.parse(path).getroot()
    counter = next((item for item in root.findall("counter")
                    if item.get("type") == "LINE"), None)
    if counter is None:
        raise ValueError("JaCoCo XML 缺少 LINE counter")
    missed = int(counter.get("missed", "0"))
    covered = int(counter.get("covered", "0"))
    files: list[dict[str, Any]] = []
    for package in root.findall("package"):
        package_name = str(package.get("name", ""))
        for source in package.findall("sourcefile"):
            lines = {
                int(line.attrib["nr"]): int(line.get("ci", "0")) > 0
                for line in source.findall("line")
            }
            if not lines:
                continue
            files.append({
                "path": f"{package_name}/{source.get('name', '')}",
                "covered": sum(lines.values()),
                "missed": sum(not value for value in lines.values()),
                "covered_lines": sorted(number for number, hit in lines.items() if hit),
            })
    return {"covered": covered, "missed": missed,
            "rate": covered / (covered + missed) if covered + missed else None,
            "files": files}


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
        fixture_error = ("InjectMocksException" in issue_type
                         or ("NullPointerException" in issue_type
                             and ".<init>(" in stack))
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
    return "\n\n".join(parts)


def _valid_generated_source(raw: str, class_name: str,
                            test_framework: str | None = None) -> tuple[str, str]:
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
    if not re.search(r"\b(?:assert\w+|verify|fail)\s*\(", source):
        raise ValueError("生成的单测缺少结果断言或调用验证")
    return package, source


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


async def run_controlled_unit_coverage(
    store: ArtifactStore, job_id: str, cases: list[dict[str, Any]],
    results: list[dict[str, Any]], interfaces: list[dict[str, Any]],
    review: dict[str, Any], inventory: dict[str, Any],
    llm: SupportsComplete | None, prompts_dir: Path | None,
) -> dict[str, Any]:
    """Test controlled cases blocked by setup; never reclassify HTTP results."""
    by_oracle = {str(item.get("oracle_id")): item for item in review.get("test_oracles") or []
                 if isinstance(item, dict)}
    by_interface = {str(item.get("id")): item for item in interfaces}
    by_repo = {str(item.get("name")): item for item in inventory.get("repositories") or []
               if isinstance(item, dict)}
    by_result = {str(item.get("case_id")): item for item in results}
    evidence = {str(item.get("evidence_id")): item for item in review.get("evidence_catalog") or []
                if isinstance(item, dict)}
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
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
    if llm is None or prompts_dir is None:
        output["status"] = "unavailable"
        output["reason"] = "未配置单元测试生成模型"
        return output
    prompt = load_prompt(prompts_dir, "unit_test_generate")
    for repo_name, entries in groups.items():
        repo = by_repo[repo_name]
        repo_result: dict[str, Any] = {"repo": repo_name, "source_sha": repo.get("target_sha"),
                                       "status": "unavailable", "cases": []}
        output["repositories"].append(repo_result)
        try:
            snapshot = await asyncio.to_thread(_snapshot, repo, store.job_dir(job_id))
            generated_count = 0
            for case, oracle in entries:
                case_id = str(case["id"])
                class_name = "AutoUnit" + re.sub(r"\W", "", case_id) + "Test"
                first_ref = next((evidence.get(str(ref)) for ref in oracle.get("evidence_refs") or []
                                  if evidence.get(str(ref), {}).get("file", "").endswith(".java")
                                  and evidence.get(str(ref), {}).get("repo") == repo_name), None)
                module = _module_root(snapshot, str((first_ref or {}).get("file", "")))
                if module is None:
                    repo_result["cases"].append({"case_id": case_id, "status": "unsupported",
                                                 "reason": "未找到对应的 Maven 模块"})
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
                try:
                    previous = _previous_passing_source(
                        store.job_dir(job_id), repo_name, str(repo.get("target_sha")),
                        case_id, class_name, test_framework,
                    )
                    if previous is not None:
                        package, source = previous
                    else:
                        answer = await llm.complete("unit_test_generate", prompt,
                                                    json.dumps(request, ensure_ascii=False))
                        try:
                            package, source = _valid_generated_source(answer.text, class_name,
                                                                      test_framework)
                        except ValueError as validation_error:
                            if not any(word in str(validation_error) for word in ("JUnit", "类名")):
                                raise
                            repair_request = {**request, "validation_error": str(validation_error),
                                              "previous_output": answer.text}
                            answer = await llm.complete("unit_test_generate", prompt,
                                                        json.dumps(repair_request, ensure_ascii=False))
                            package, source = _valid_generated_source(answer.text, class_name,
                                                                      test_framework)
                    relative = Path(*package.split(".")) / f"{class_name}.java"
                    target = module / "src" / "test" / "java" / relative
                    if target.exists():
                        raise ValueError("目标提交已有同名测试类")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(source, encoding="utf-8")
                    store.write_raw(job_id, f"scripts/unit_{case_id}.java", source)
                except Exception as exc:  # model failure belongs to this unit candidate
                    repo_result["cases"].append({"case_id": case_id, "status": "generation_failed",
                                                 "reason": str(exc) if isinstance(exc, ValueError)
                                                 else type(exc).__name__})
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
                    process: asyncio.subprocess.Process | None = None
                    stdout = b""
                    try:
                        process = await asyncio.create_subprocess_exec(
                            *cmd, cwd=module, env=_maven_environment(module, snapshot),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        stdout, _ = await asyncio.wait_for(process.communicate(), _MAVEN_TIMEOUT_SECONDS)
                        exit_code = process.returncode
                    except (OSError, asyncio.TimeoutError):
                        if process is not None and process.returncode is None:
                            process.kill()
                            await process.communicate()
                        exit_code = -1
                    output_text = redact_secrets(stdout.decode("utf-8", errors="replace"))
                    run_logs.append(f"=== Maven 尝试 {attempt + 1} ===\n{output_text[-12_000:]}")
                    outcome = _surefire_result(module, class_name)
                    if outcome["status"] in ("passed", "failed", "fixture_failed"):
                        last_executed = (outcome.copy(), source)
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
                                                                  test_framework)
                        if package != ".".join(relative.parts[:-1]):
                            raise ValueError("修复后的 package 与测试文件路径不一致")
                        target.write_text(source, encoding="utf-8")
                        store.write_raw(job_id, f"scripts/unit_{case_id}.java", source)
                    except Exception as repair_error:
                        outcome["reason"] += f"；自动修复失败：{type(repair_error).__name__}"
                        break
                if outcome["status"] == "build_failed" and last_executed is not None:
                    outcome, source = last_executed
                    outcome = {**outcome, "reason": outcome.get("reason", "")
                               + "；后续自动修复未能编译，保留前一次执行结果"}
                    store.write_raw(job_id, f"scripts/unit_{case_id}.java", source)
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
                    "source_artifact": f"scripts/unit_{case_id}.java",
                    "log_artifact": log_artifact,
                    "reused_verified_source": previous is not None,
                    "source": source, **outcome,
                })
                target.unlink(missing_ok=True)
            repo_result["status"] = "completed" if generated_count else "unavailable"
        except Exception as exc:
            logger.exception("隔离单元测试执行失败：%s", repo_name)
            repo_result["reason"] = f"隔离源码快照或 Maven 工程不可用（{type(exc).__name__}）"
            for case, _ in entries:
                if not any(item["case_id"] == case["id"] for item in repo_result["cases"]):
                    repo_result["cases"].append({"case_id": case["id"], "status": "unsupported",
                                                 "reason": "隔离源码快照或 Maven 工程不可用"})
    case_results = {item["case_id"]: item for repo in output["repositories"]
                    for item in repo["cases"]}
    for result in results:
        unit = case_results.get(str(result.get("case_id")))
        if unit:
            result["unit_test"] = unit
    output["cases_passed"] = sum(item["status"] == "passed" for item in case_results.values())
    output["cases_failed"] = output["cases_total"] - output["cases_passed"]
    output["status"] = "completed" if output["repositories"] else "unavailable"
    return output
