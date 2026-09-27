// 对应契约异步 job 模式：POST 受理（幂等 job_id）→ 后台执行 → GET 轮询至终态。
package com.llmautotest.parser.job;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.ApiError;
import com.llmautotest.parser.contract.JobAccepted;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;
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

    /**
     * 受理 job（幂等键 = kind + job_id + 请求内容指纹）。
     *
     * <p>同 kind+job_id+同指纹重复提交 → 复用既有记录，不重复执行（真正的幂等）。
     * <p>指纹变化（重跑时改了提交号/基线/仓库）→ 覆盖旧记录并重新执行。**必须重跑**：
     * 沿用旧结果会让上游拿到过期产物，实测踩过一次——提交号配好后重跑，
     * changed_files 仍返回首跑的空值，下游据此判定「仓库未配置变更范围」并拦停流水线。
     * <p>kind 隔离 scan/extract——联调发现的真 bug：裸 job_id 键使两类 job 互相顶掉。
     */
    public JobAccepted submit(JobKind kind, String jobId, String fingerprint, JobTask task) {
        String key = kind.prefix() + jobId;
        AtomicReference<JobRecord> started = new AtomicReference<>();
        // 提交动作放在 compute 之外：ConcurrentHashMap 的 compute 持有桶锁，
        // 不应在锁内做 execute（避免线程池繁忙时阻塞其它 job 的查询）。
        jobs.compute(
                key,
                (id, existing) -> {
                    if (existing != null && existing.fingerprint().equals(fingerprint)) {
                        return existing; // 同请求重复提交：幂等复用
                    }
                    JobRecord fresh = new JobRecord(fingerprint);
                    started.set(fresh);
                    return fresh; // 输入已变：覆盖旧记录
                });
        JobRecord record = started.get();
        if (record != null) {
            executor.execute(() -> run(record, task));
        }
        return JobAccepted.queued(jobId);
    }

    /** 请求内容指纹（SHA-256）：只覆盖影响产物的输入，用于判断既有结果是否仍有效。 */
    public static String fingerprintOf(Object payload) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = String.valueOf(payload).getBytes(StandardCharsets.UTF_8);
            return HexFormat.of().formatHex(digest.digest(bytes));
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 不可用", ex);
        }
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
