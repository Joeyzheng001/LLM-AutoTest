package com.demo.fallback;

import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

/**
 * 兜底演示仓库（D5）：结构极简，仅当真实仓库解析失败时顶上。
 * 结果单独标注 is_fallback=true、不计入 G3、与真实仓库分列——不掩盖真实结论。
 */
@RestController
public class PingController {

    @GetMapping("/api/v1/ping/{token}")
    public Map<String, String> ping(@PathVariable String token) {
        return Map.of("pong", token);
    }
}
