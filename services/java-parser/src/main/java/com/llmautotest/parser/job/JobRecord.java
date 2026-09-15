// 对应契约 ScanJob / ExtractJob 轮询产物：job 运行记录（内存态，job 生命周期内可见）。
package com.llmautotest.parser.job;

import com.llmautotest.parser.contract.ApiError;
import com.llmautotest.parser.contract.JobStatus;
import java.util.Map;

/** 单个异步 job 的状态与产物（写方仅 JobManager 执行线程，读方为轮询端点）。 */
public final class JobRecord {

    private volatile JobStatus status = JobStatus.queued;
    private volatile Map<String, Object> result;
    private volatile ApiError error;

    /** 置运行中。 */
    public void markRunning() {
        status = JobStatus.running;
    }

    /** 置完成并挂产物（code_inventory / candidates 等契约载荷）。 */
    public void markCompleted(Map<String, Object> payload) {
        this.result = payload;
        status = JobStatus.completed;
    }

    /** 置失败并挂错误体（稳定错误码，不泄漏堆栈）。 */
    public void markFailed(ApiError failure) {
        this.error = failure;
        status = JobStatus.failed;
    }

    public JobStatus status() {
        return status;
    }

    /** 完成态产物（未完成时为 null）。 */
    public Map<String, Object> result() {
        return result;
    }

    /** 失败态错误体（未失败时为 null）。 */
    public ApiError error() {
        return error;
    }
}
