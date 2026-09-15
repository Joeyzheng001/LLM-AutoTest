// 健康检查端点（docker-compose 编排依赖）。
package com.llmautotest.parser;

import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** 健康检查与健康信息。 */
@RestController
public class HealthController {

    /** 容器健康检查端点。 */
    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "java-parser");
    }
}
