// 对应契约异步 job 模式：POST 受理（幂等 job_id）→ 后台执行 → GET 轮询至终态。
package com.llmautotest.parser.job;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.ApiError;
import com.llmautotest.parser.contract.JobAccepted;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/** 内存 job 注册表 + 受控并发执行器（scan-concurrency 线程池）。 */
@Component
public class JobManager {

    private static final Logger LOG = LoggerFactory.getLogger(JobManager.class);

    private final Map<String, JobRecord> jobs = new ConcurrentHashMap<>();
    private final ExecutorService executor;

    public JobManager(ParserProperties properties) {
        this.executor = Executors.newFixedThreadPool(properties.scanConcurrency());
    }

    /** 受理 job（同 kind+job_id 重复提交幂等返回既有记录；
     * kind 隔离 scan/extract——联调发现的真 bug：裸 job_id 键使两类 job 互相顶掉）。 */
    public JobAccepted submit(JobKind kind, String jobId, JobTask task) {
        String key = kind.prefix() + jobId;
        JobRecord record = jobs.computeIfAbsent(key, id -> {
            JobRecord fresh = new JobRecord();
            executor.execute(() -> run(fresh, task));
            return fresh;
        });
        return JobAccepted.queued(jobId);
    }

    /** 查询 job（不存在返回 null，由端点转 404）。 */
    public JobRecord get(JobKind kind, String jobId) {
        return jobs.get(kind.prefix() + jobId);
    }

    private void run(JobRecord record, JobTask task) {
        record.markRunning();
        try {
            record.markCompleted(task.execute());
        } catch (Exception ex) {
            // 契约纪律：稳定错误码 + 可理解信息，堆栈只进日志不进响应体
            LOG.error("job 执行失败", ex);
            record.markFailed(ApiError.of("INTERNAL_ERROR", "job 执行失败，详见服务日志"));
        }
    }

    /** job 任务：返回契约产物载荷（Map 形态与 openapi 契约字段一一对应）。 */
    @FunctionalInterface
    public interface JobTask {
        Map<String, Object> execute() throws Exception;
    }

    /** job 类别（幂等键隔离：scan 与 extract 可共用同一 job_id）。 */
    public enum JobKind {
        SCAN("scan:"),
        EXTRACT("extract:");

        private final String prefix;

        JobKind(String prefix) {
            this.prefix = prefix;
        }

        String prefix() {
            return prefix;
        }
    }
}
