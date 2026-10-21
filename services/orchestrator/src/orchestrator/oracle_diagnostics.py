"""Reconcile generation history with final source designs; report system outcomes."""

from __future__ import annotations

import re
from typing import Any

from common.oracle_conditions import required_control_kinds


def build_oracle_diagnostics(
    warnings: list[str], oracles: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep original history while separating repaired, replaced and pending items."""
    diagnostics: list[dict[str, Any]] = []
    for message in warnings:
        interface = re.search(r"IF-[A-Z]+-\d{3}", message)
        interface_id = interface.group(0) if interface else ""
        scenario_match = re.search(r"“([^”]+)”", message)
        scenario = scenario_match.group(1) if scenario_match else ""
        related = [item for item in oracles or [] if item.get("interface_id") == interface_id]
        exact = [item for item in related if scenario and item.get("scenario") == scenario]
        normal = [item for item in related if item.get("category") == "normal"
                  and not required_control_kinds(item)
                  and item.get("design_status") != "pending_expectation"]
        dto = [item for item in related if "DTO" in str(item.get("scenario", ""))
               and item.get("preconditions") and item.get("assertions")]
        controls = [kind for kind in ("downstream_response", "application_config", "service_registry") if kind in message]
        matched = exact
        summary = f"{interface_id} {scenario}" if scenario else message
        if "正常返回预期待补充" in message:
            summary = f"{interface_id} 普通有效请求设计尚未形成"
        elif message.startswith("以下接口未形成"):
            summary = "尚无有效测试设计的接口：" + "、".join(dict.fromkeys(re.findall(r"IF-[A-Z]+-\d{3}", message)))
        if "正常返回预期待补充" in message and normal:
            status = "resolved"
            matched = normal
            summary = f"{interface_id} 正常请求设计已保留"
            action = f"已按最终产物核对：保留 {len(normal)} 条正常设计。历史补查告警已归入处理记录；执行结果另行统计。"
        elif "DTO 参数校验预期由源码规则生成" in message and dto:
            status = "replaced"
            matched = dto
            summary = f"{interface_id} DTO 候选已由源码规则设计替代"
            action = f"已绑定 {len(dto)} 条 DTO 校验设计，原重复候选已合并，不再计为缺失用例。"
        elif "已隔离未证实的响应断言" in message:
            status = "response_evidence_missing"
            summary = f"{interface_id} {scenario}已保留可证断言，部分字段未验证"
            action = message.split("”", 1)[-1] if "”" in message else message
        elif exact and all(item.get("design_status") != "pending_expectation" for item in exact) and any(token in message for token in ("校验失败", "无可证断言", "已自动修复")):
            status = "resolved"
            summary = f"{interface_id} {scenario}已保留"
            action = "已在最终产物找到对应的已校验设计；候选失败作为过程记录保存，未覆盖实际执行结论。"
        elif "框架拦截" in message or "HTTP 框架边界" in message:
            status = "framework_boundary"
            matched = [item for item in related if item.get("oracle_basis") == "spring_required_body"]
            action = "已将 HTTP 请求体绑定边界与 Service 内部空值分支分别处理。" if matched else "已识别框架绑定边界，当前产物尚无对应的有效请求设计。"
        elif controls or any(token in message for token in ("fixture", "mock 计划", "复合执行器环境配置", "Spring 服务注册表")):
            status = "control_required"
            action = "当前任务没有满足该场景的运行控制条件，系统未发送该场景请求；已保留的设计与原始候选分别留痕。"
        elif any(token in message for token in ("不能保证 Service", "异常触发依赖服务", "只有通用 catch", "数据库记录", "数据库查询结果")):
            status = "execution_pending"
            action = "已识别为异常触发或数据状态条件缺口；未把参数存在当作条件已满足，也未计为接口执行失败。"
        elif exact and any(item.get("design_status") == "pending_expectation" for item in exact):
            status = "response_evidence_missing"
            summary = f"{interface_id} {scenario}的源码设计已保留"
            action = "系统已保留可核验的条件与处理出口；响应断言未通过校验，未生成可用于缺陷判断的执行请求。"
        elif any(token in message for token in ("异常处理器", "HTTP 响应", "运行实测", "实测契约", "响应映射缺口")):
            status = "response_evidence_missing"
            action = "本轮未取得可核验的 HTTP 响应映射，系统未把内部异常直接换成猜测的响应预期；原始模型返回和失败原因已保留。"
        else:
            status = "candidate_invalid"
            action = "本轮候选未通过源码契约校验，系统已隔离未经确认的预期并保存原始返回；该记录不代表被测接口执行失败。"
        diagnostics.append({
            "status": status, "interface_id": interface_id, "scenario": scenario,
            "summary": summary, "message": message, "action": action,
            "oracle_ids": [str(item["oracle_id"]) for item in matched if item.get("oracle_id")],
            "control_kinds": controls,
        })
    return diagnostics
