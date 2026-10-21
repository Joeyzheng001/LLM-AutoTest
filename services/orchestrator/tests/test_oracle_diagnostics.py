"""执行覆盖诊断保留原文，并明确环境、框架和生成问题的不同处理动作。"""

from orchestrator.oracle_diagnostics import build_oracle_diagnostics


def test_diagnostics_preserve_all_gaps_with_actionable_status() -> None:
    warnings = [
        "接口 IF-REST-001 的“请求为null”未执行：@RequestBody 会被框架拦截",
        "接口 IF-REST-002 的“O32失败”需 downstream_response setup/teardown fixture",
        "接口 IF-REST-003 的“配置为空”需 application_config setup/teardown fixture",
        "接口 IF-REST-004 的“缓存不存在”需 service_registry setup/teardown fixture",
        "接口 IF-REST-004 缺少异常处理器，无法证明 HTTP 响应",
        "接口 IF-REST-002 断言格式不合法",
    ]
    result = build_oracle_diagnostics(warnings)
    assert [item["message"] for item in result] == warnings
    assert [item["status"] for item in result] == [
        "framework_boundary",
        "control_required",
        "control_required",
        "control_required",
        "response_evidence_missing",
        "candidate_invalid",
    ]
    assert result[1]["control_kinds"] == ["downstream_response"]
    assert result[1]["scenario"] == "O32失败"
    assert all(item["action"] for item in result)
