// 应用配置（docs/09 第三章：配置不硬编码）。
package com.llmautotest.parser;

import org.springframework.boot.context.properties.ConfigurationProperties;

/** 服务运行配置，从环境变量读取。 */
@ConfigurationProperties(prefix = "parser")
public record ParserProperties(int scanConcurrency, int maxFilesPerRepo, int gitTimeoutSeconds) {

    /** git 子进程默认墙钟上限（秒）。
     *
     * <p>原先硬编码 30s。超时是**所有** git 命令共用的上限，冷启动 / 大仓库首次 diff
     * 可能真的触顶；一旦触顶会表现为"扫描失败"，而原实现不说是哪条命令超时，无法诊断。
     * 所以改为可配 + 消息带命令名。
     */
    public static final int DEFAULT_GIT_TIMEOUT_SECONDS = 30;

    /** 多仓库扫描并发度必须受控可配置（默认 4）；截断上限必须为正（默认 20000，X-CODE-03）。
     * git 超时缺省或非正数时回落默认值（配置文件未提供该键时 Spring 传 0）。 */
    public ParserProperties {
        if (scanConcurrency <= 0) {
            throw new IllegalArgumentException("scanConcurrency must be positive");
        }
        if (maxFilesPerRepo <= 0) {
            throw new IllegalArgumentException("maxFilesPerRepo must be positive");
        }
        if (gitTimeoutSeconds <= 0) {
            gitTimeoutSeconds = DEFAULT_GIT_TIMEOUT_SECONDS;
        }
    }

    /** 兼容工厂（git 超时取默认值；测试与只关心前两项的调用点）。
     *
     * <p>刻意用**静态工厂**而不是第二个构造器：多构造器会让 Spring 无法判定
     * 构造器绑定（报 "No default constructor found"）。
     */
    public static ParserProperties of(int scanConcurrency, int maxFilesPerRepo) {
        return new ParserProperties(scanConcurrency, maxFilesPerRepo, DEFAULT_GIT_TIMEOUT_SECONDS);
    }
}
