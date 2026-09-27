// 对应契约 ScanJob / ExtractJob 轮询产物：job 运行记录（内存态，job 生命周期内可见）。
package com.llmautotest.parser.job;

import com.llmautotest.parser.contract.ApiError;
import com.llmautotest.parser.contract.JobStatus;
import java.util.Map;

/** 单个异步 job 的状态与产物（写方仅 JobManager 执行线程，读方为轮询端点）。 */
public final class JobRecord {

    /**
     * 受理时的请求内容指纹。
     *
     * 幂等判据不能只看 job_id：同一任务重跑时输入会变（改了提交号/基线/仓库），
     * 若沿用旧记录就会把上一次的产物当成新结果返回（实测踩过：提交号配好后重跑，
     * changed_files 仍是首跑的旧值，导致下游判定「仓库未配置变更范围」）。
     */
    private final String fingerprint;

    private volatile JobStatus status = JobStatus.queued;
    private volatile Map<String, Object> result;
    private volatile ApiError error;

    public JobRecord(String fingerprint) {
        this.fingerprint = fingerprint;
    }

    /** 受理时的请求内容指纹（用于判断既有结果是否仍对应当前输入）。 */
    public String fingerprint() {
        return fingerprint;
    }

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
