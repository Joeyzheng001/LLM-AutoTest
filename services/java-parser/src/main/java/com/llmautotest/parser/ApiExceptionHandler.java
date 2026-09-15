// 对应契约 Error 结构：端点异常统一转契约错误体（docs/09 第七章，不泄漏堆栈）。
package com.llmautotest.parser;

import com.llmautotest.parser.contract.ApiError;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 控制器层统一异常 → 契约错误响应（400 / 404 / 500）。 */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger LOG = LoggerFactory.getLogger(ApiExceptionHandler.class);

    /** 请求非法 → 400 INVALID_REQUEST。 */
    @ExceptionHandler(ScanApiController.InvalidRequestException.class)
    public ResponseEntity<ApiError> invalidRequest(ScanApiController.InvalidRequestException ex) {
        return ResponseEntity.badRequest().body(ApiError.of("INVALID_REQUEST", ex.getMessage()));
    }

    /** job 不存在 → 404 JOB_NOT_FOUND。 */
    @ExceptionHandler(ScanApiController.JobNotFoundException.class)
    public ResponseEntity<ApiError> jobNotFound(ScanApiController.JobNotFoundException ex) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(ApiError.of("JOB_NOT_FOUND", ex.getMessage()));
    }

    /** 未预期异常 → 500 INTERNAL_ERROR（堆栈只进日志）。 */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiError> unexpected(Exception ex) {
        LOG.error("未预期异常", ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(ApiError.of("INTERNAL_ERROR", "服务内部错误，详见日志"));
    }
}
