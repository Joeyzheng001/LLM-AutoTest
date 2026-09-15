---
kind: external_dependency
name: Java 代码解析服务（基于 JavaParser）
slug: java-parser
category: external_dependency
category_hints:
    - framework_behavior
scope:
    - '**'
source_files:
    - .rules/java-code-style.md
    - services/java-parser/pom.xml
    - services/java-parser/src/main/java/com/llmautotest/parser/ProtocolResolver.java
---

独立 Java 21 + Maven 微服务，提供 REST API 供 orchestrator 调用以扫描仓库并还原接口契约。
- 解析能力基于 JavaParser，对 Dubbo / gRPC / 自定义注解等协议通过可插拔 `ProtocolResolver` 接口实现，新增协议不改主流程。
- 对外契约定义在 `schemas/contracts/`（如 `java-parser.openapi.yaml`），REST 层只做协议转换与校验，扫描与还原逻辑下沉到解析服务层。
- 多仓库扫描并发度受控可配置，所有对 orchestrator 的服务调用必须设置连接与读取超时。