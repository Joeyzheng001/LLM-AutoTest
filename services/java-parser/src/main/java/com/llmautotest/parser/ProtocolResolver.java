// 对应 R2 风险应对：协议解析策略可插拔，新增协议不改主流程（docs/09 第三章）。
package com.llmautotest.parser;

import java.nio.file.Path;

/** 协议解析策略接口：Spring Controller / Dubbo / gRPC 等各协议实现此接口。 */
public interface ProtocolResolver {

    /** 本策略支持的协议标识（rest / dubbo / grpc / graphql / soap / mq）。 */
    String supportedProtocol();

    /** 判断给定源码文件是否属于本协议（扫描阶段的过滤器）。 */
    boolean supports(Path sourceFile);

    /** 解析结果与调用约定在 M0 契约定稿（schemas/contracts/）后接入。 */
}
