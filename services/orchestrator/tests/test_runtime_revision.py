"""部署版本与工作区版本不一致时禁止继续启动任务。"""

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException

from orchestrator.api import _spawn_run
from orchestrator.runtime_revision import RuntimeRevision


@pytest.mark.parametrize(
    "relative",
    [
        "services/common/src/common/assertions.py",
        "prompts/oracles.md",
        "schemas/oracles.json",
    ],
)
def test_runtime_guard_detects_code_prompt_and_contract_changes(
    tmp_path: Path, relative: str
) -> None:
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text("original", encoding="utf-8")
    revision = RuntimeRevision(tmp_path)
    assert revision.restart_required is False
    source.write_text("modified", encoding="utf-8")
    assert revision.restart_required is True
    app = FastAPI()
    app.state.runtime_revision = revision
    with pytest.raises(HTTPException) as exc:
        _spawn_run(app, "job-not-started")
    assert exc.value.status_code == 409
    assert "重启" in str(exc.value.detail)
    assert not hasattr(app.state, "job_tasks")
