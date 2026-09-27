// 对应契约 /api/v1/scan-jobs（F-CODE-01~03：POST 受理 + GET 轮询）。
package com.llmautotest.parser;

import com.llmautotest.parser.contract.JobAccepted;
import com.llmautotest.parser.contract.JobStatus;
import com.llmautotest.parser.contract.ScanRequest;
import com.llmautotest.parser.job.JobManager;
import com.llmautotest.parser.job.JobRecord;
import com.llmautotest.parser.job.ScanJobRunner;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 扫描 job 端点：POST 202 受理 → GET 轮询（completed 含 code_inventory）。 */
@RestController
@RequestMapping("/api/v1/scan-jobs")
public class ScanApiController {

    private final JobManager jobs;
    private final ScanJobRunner runner;

    public ScanApiController(JobManager jobs, ScanJobRunner runner) {
        this.jobs = jobs;
        this.runner = runner;
    }

    /** 创建扫描任务（repositories 为空 → 400 INVALID_REQUEST）。 */
    @PostMapping
    public ResponseEntity<JobAccepted> create(@RequestBody ScanRequest request) {
        if (request.repositories() == null || request.repositories().isEmpty()) {
            throw new InvalidRequestException("repositories 不能为空");
        }
        // 指纹取整个请求体：任一输入（提交号/基线/仓库路径）变化即视为新请求，
        // 覆盖旧记录并重新扫描，避免重跑拿到上一次的产物。
        JobAccepted accepted =
                jobs.submit(
                        JobManager.JobKind.SCAN,
                        request.job_id(),
                        JobManager.fingerprintOf(request),
                        () -> runner.run(request));
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(accepted);
    }

    /** 查询 job 状态与产物（不存在 → 404 JOB_NOT_FOUND）。 */
    @GetMapping("/{jobId}")
    public ResponseEntity<Map<String, Object>> get(@PathVariable String jobId) {
        JobRecord record = requireJob(jobId);
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("job_id", jobId);
        view.put("status", record.status());
        if (record.status() == JobStatus.completed && record.result() != null) {
            view.put("code_inventory", record.result());
        }
        if (record.status() == JobStatus.failed && record.error() != null) {
            view.put("error", record.error());
        }
        return ResponseEntity.ok(view);
    }

    private JobRecord requireJob(String jobId) {
        JobRecord record = jobs.get(JobManager.JobKind.SCAN, jobId);
        if (record == null) {
            throw new JobNotFoundException(jobId);
        }
        return record;
    }

    /** 请求非法（400，契约 Error 体）。 */
    static final class InvalidRequestException extends RuntimeException {
        InvalidRequestException(String message) {
            super(message);
        }
    }

    /** job 不存在（404，契约 Error 体）。 */
    static final class JobNotFoundException extends RuntimeException {
        JobNotFoundException(String jobId) {
            super("job 不存在：" + jobId);
        }
    }
}
