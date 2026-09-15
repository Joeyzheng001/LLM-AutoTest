// 应用配置（docs/09 第三章：配置不硬编码）。
package com.llmautotest.parser;

import org.springframework.boot.context.properties.ConfigurationProperties;

/** 服务运行配置，从环境变量读取。 */
@ConfigurationProperties(prefix = "parser")
public record ParserProperties(int scanConcurrency, int maxFilesPerRepo) {

    /** 多仓库扫描并发度必须受控可配置（默认 4）；截断上限必须为正（默认 20000，X-CODE-03）。 */
    public ParserProperties {
        if (scanConcurrency <= 0) {
            throw new IllegalArgumentException("scanConcurrency must be positive");
        }
        if (maxFilesPerRepo <= 0) {
            throw new IllegalArgumentException("maxFilesPerRepo must be positive");
        }
    }
}
