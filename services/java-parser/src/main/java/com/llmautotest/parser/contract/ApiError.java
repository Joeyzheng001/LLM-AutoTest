// 对应契约 Error（docs/09 第七章：三栈统一错误结构，稳定错误码，不泄漏堆栈）。
package com.llmautotest.parser.contract;

import java.util.Map;

/** 统一错误响应体。 */
public record ApiError(String code, String message, String node_id, Map<String, Object> details) {

    /** 最小构造（无关联节点）。 */
    public static ApiError of(String code, String message) {
        return new ApiError(code, message, null, null);
    }
}
