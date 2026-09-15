// 对应契约 JobAccepted / JobStatus（异步 job 模式：POST 受理 → GET 轮询）。
package com.llmautotest.parser.contract;

/** 202 受理响应。 */
public record JobAccepted(String job_id, JobStatus status) {

    /** 受理即排队。 */
    public static JobAccepted queued(String jobId) {
        return new JobAccepted(jobId, JobStatus.queued);
    }
}
