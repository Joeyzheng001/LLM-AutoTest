"""隔离 Java 单测与 JaCoCo 结果的确定性边界。"""

from __future__ import annotations

import subprocess
import asyncio
import json
from types import SimpleNamespace

import pytest

from executor import unit_coverage
from executor.unit_coverage import (
    _covered_evidence_lines, _maven_environment, _snapshot,
    _valid_generated_source, parse_jacoco_xml,
)


def test_parse_jacoco_line_counter_and_source_lines(tmp_path) -> None:
    report = tmp_path / "jacoco.xml"
    report.write_text(
        '<report name="sample"><package name="p/q"><sourcefile name="Service.java">'
        '<line nr="10" mi="0" ci="3" mb="0" cb="1"/>'
        '<line nr="11" mi="2" ci="0" mb="1" cb="0"/>'
        '</sourcefile></package><counter type="LINE" missed="1" covered="1"/></report>',
        encoding="utf-8",
    )

    coverage = parse_jacoco_xml(report)

    assert coverage["rate"] == 0.5
    assert coverage["covered"] == 1
    assert coverage["missed"] == 1
    assert coverage["files"][0]["path"] == "p/q/Service.java"
    assert coverage["files"][0]["covered_lines"] == [10]


def test_coverage_requires_oracle_evidence_span(tmp_path) -> None:
    snapshot = tmp_path / "repo"
    module = snapshot / "biz"
    module.mkdir(parents=True)
    oracle = {"evidence_refs": ["EVD-1"]}
    evidence = {"EVD-1": {"repo": "sample", "file": "biz/src/main/java/p/Service.java",
                          "start_line": 20, "end_line": 21}}
    coverage = {"files": [{"path": "p/Service.java", "covered_lines": [10, 20]}]}

    assert _covered_evidence_lines(coverage, oracle, evidence, "sample", module, snapshot) == 1
    coverage["files"][0]["covered_lines"] = [10]
    assert _covered_evidence_lines(coverage, oracle, evidence, "sample", module, snapshot) == 0


def test_generated_unit_source_rejects_external_process() -> None:
    source = {
        "package": "p.q",
        "source": (
            "package p.q; class AutoUnitTC014Test { @Test void branch() { "
            "Runtime.getRuntime().exec(\"sh\"); } }"
        ),
    }
    import json

    with pytest.raises(ValueError, match="外部进程"):
        _valid_generated_source(json.dumps(source), "AutoUnitTC014Test")


def test_generated_unit_source_requires_an_assertion() -> None:
    source = {"package": "p.q", "source": (
        "package p.q; class AutoUnitTC014Test { @Test void branch() {} }"
    )}

    with pytest.raises(ValueError, match="缺少结果断言"):
        _valid_generated_source(json.dumps(source), "AutoUnitTC014Test")


def test_snapshot_uses_target_commit_without_untracked_tests(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "pom.xml").write_text("<project/>", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "pom.xml"], check=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "snapshot",
    ], check=True)
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    (source / "UntrackedTest.java").write_text("class UntrackedTest {}", encoding="utf-8")

    snapshot = _snapshot({"path": str(source), "target_sha": sha}, tmp_path / "job")

    assert (snapshot / "pom.xml").is_file()
    assert not (snapshot / "UntrackedTest.java").exists()
    assert (source / "UntrackedTest.java").is_file()


def test_maven_uses_available_java_21_for_java_21_project(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    module = root / "biz"
    module.mkdir(parents=True)
    (root / "pom.xml").write_text("<project><source>21</source></project>", encoding="utf-8")
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    java_home = tmp_path / "jdk21"
    (java_home / "bin").mkdir(parents=True)
    (java_home / "bin/java").write_text("", encoding="utf-8")
    monkeypatch.setattr(unit_coverage, "_HOMEBREW_JAVA_21", java_home)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.delenv("UNIT_TEST_JAVA_HOME", raising=False)

    assert _maven_environment(module, root)["JAVA_HOME"] == str(java_home)


def test_controlled_case_gets_separate_unit_result_and_coverage(tmp_path, monkeypatch) -> None:
    snapshot = tmp_path / "snapshot"
    source = snapshot / "module/src/main/java/p/Service.java"
    source.parent.mkdir(parents=True)
    source.write_text("package p; class Service { Object run() { return null; } }", encoding="utf-8")
    (snapshot / "module/pom.xml").write_text(
        "<project><dependencies><dependency><artifactId>spring-boot-starter-test</artifactId>"
        "</dependency></dependencies></project>", encoding="utf-8"
    )
    monkeypatch.setattr(unit_coverage, "_snapshot", lambda repo, job_dir: snapshot)

    class FakeStore:
        def job_dir(self, job_id):
            return tmp_path / job_id

        def write_raw(self, job_id, name, content):
            return tmp_path / job_id / name

    class FakeLlm:
        async def complete(self, node, prompt, user_content):
            assert json.loads(user_content)["class_name"] == "AutoUnitTC014Test"
            return SimpleNamespace(text=json.dumps({
                "package": "p",
                "source": "package p; import org.junit.jupiter.api.Test; class AutoUnitTC014Test { "
                          "@Test void branch() { assertTrue(true); } }",
            }))

    class FakeProcess:
        returncode = 0

        def __init__(self, module):
            self.module = module

        async def communicate(self):
            reports = self.module / "target/surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-p.AutoUnitTC014Test.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8"
            )
            coverage = self.module / "target/site/jacoco/jacoco.xml"
            coverage.parent.mkdir(parents=True)
            coverage.write_text(
                '<report><package name="p"><sourcefile name="Service.java">'
                '<line nr="1" mi="0" ci="1" mb="0" cb="0"/>'
                '</sourcefile></package><counter type="LINE" missed="1" covered="1"/></report>',
                encoding="utf-8",
            )
            return b"", b""

    async def fake_maven(*args, **kwargs):
        return FakeProcess(kwargs["cwd"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_maven)
    cases = [{"id": "TC-014", "interface_id": "IF-REST-002", "oracle_id": "ORC-IF-REST-002-005",
              "oracle_status": "verified"}]
    results = [{"case_id": "TC-014", "execution_state": "not_executed"}]
    interfaces = [{"id": "IF-REST-002", "code_location": {"repo": "sample"}}]
    review = {"test_oracles": [{"oracle_id": "ORC-IF-REST-002-005",
                                "evidence_refs": ["EVD-0001"],
                                "control_kinds": ["downstream_response"]}],
              "evidence_catalog": [{"evidence_id": "EVD-0001", "repo": "sample",
                                    "file": "module/src/main/java/p/Service.java"}]}
    inventory = {"repositories": [{"name": "sample", "target_sha": "a" * 40,
                                   "language": None}]}
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "unit_test_generate.md").write_text(
        "---\nversion: 1.0\nspec: TEST\nowner: 丙\nchangelog:\n  - v1\n"
        "sampling:\n  temperature: 0.2\n  top_p: 0.8\n  seed: null\n---\nGenerate.",
        encoding="utf-8",
    )

    outcome = asyncio.run(unit_coverage.run_controlled_unit_coverage(
        FakeStore(), "job-test", cases, results, interfaces, review, inventory,
        FakeLlm(), prompt_dir,
    ))

    assert outcome["cases_passed"] == 1, outcome
    assert outcome["repositories"][0]["modules"][0]["line_coverage"]["rate"] == 0.5
    assert results[0]["execution_state"] == "not_executed"
    assert results[0]["unit_test"]["status"] == "passed"
