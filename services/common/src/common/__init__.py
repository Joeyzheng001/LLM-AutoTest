"""LLM-AutoTest 共享核心包：产物管理、节点状态机、LLM 客户端、Prompt 加载、错误码、安全。

由 orchestrator 与 test-executor 共同依赖；不包含任何服务入口（api/runner/__main__ 留在各服务内）。
"""
