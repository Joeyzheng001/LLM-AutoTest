// 对应契约 /api/v1/extract-jobs（R-REV-02 第①步：POST 受理 + GET 轮询）。
package com.llmautotest.parser;

import com.llmautotest.parser.contract.JobAccepted;
import com.llmautotest.parser.contract.JobStatus;
import com.llmautotest.parser.contract.ExtractRequest;
import com.llmautotest.parser.job.ExtractJobRunner;
import com.llmautotest.parser.job.JobManager;
import com.llmautotest.parser.job.JobRecord;
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

/** 候选提取 job 端点：POST 202 受理 → GET 轮询（completed 含 candidates + warnings）。 */
@RestController
@RequestMapping("/api/v1/extract-jobs")
public class ExtractApiController {

    private final JobManager jobs;
    private final ExtractJobRunner runner;

    public ExtractApiController(JobManager jobs, ExtractJobRunner runner) {
        this.jobs = jobs;
        this.runner = runner;
    }

    /** 创建候选提取任务（repositories 为空 → 400）。 */
    @PostMapping
    public ResponseEntity<JobAccepted> create(@RequestBody ExtractRequest request) {
        if (request.repositories() == null || request.repositories().isEmpty()) {
            throw new ScanApiController.InvalidRequestException("repositories 不能为空");
        }
        JobAccepted accepted = jobs.submit(JobManager.JobKind.EXTRACT, request.job_id(), () -> runner.run(request));
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(accepted);
    }

    /** 查询 job 状态与产物（不存在 → 404）。 */
    @GetMapping("/{jobId}")
    public ResponseEntity<Map<String, Object>> get(@PathVariable String jobId) {
        JobRecord record = requireJob(jobId);
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("job_id", jobId);
        view.put("status", record.status());
        if (record.status() == JobStatus.completed && record.result() != null) {
            view.putAll(record.result());
        }
        if (record.status() == JobStatus.failed && record.error() != null) {
            view.put("error", record.error());
        }
        return ResponseEntity.ok(view);
    }

    private JobRecord requireJob(String jobId) {
        JobRecord record = jobs.get(JobManager.JobKind.EXTRACT, jobId);
        if (record == null) {
            throw new ScanApiController.JobNotFoundException(jobId);
        }
        return record;
    }
}
