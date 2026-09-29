// JobManager 幂等语义：同请求复用、输入变化必须重跑。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.JobStatus;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/** 幂等键 = kind + job_id + 请求内容指纹：防重复执行，但绝不复用过期产物。 */
class JobManagerTest {

    private static final ParserProperties PROPERTIES = ParserProperties.of(2, 20000);

    @Test
    void sameFingerprintReusesRecordWithoutRerun() throws Exception {
        JobManager jobs = new JobManager(PROPERTIES);
        AtomicInteger runs = new AtomicInteger();
        JobManager.JobTask task =
                () -> {
                    runs.incrementAndGet();
                    return Map.of("job_id", "job-same");
                };

        jobs.submit(JobManager.JobKind.SCAN, "job-same", "fp-1", task);
        JobRecord first = awaitTerminal(jobs, JobManager.JobKind.SCAN, "job-same");

        jobs.submit(JobManager.JobKind.SCAN, "job-same", "fp-1", task);
        JobRecord second = awaitTerminal(jobs, JobManager.JobKind.SCAN, "job-same");

        assertThat(second).isSameAs(first);
        assertThat(runs).hasValue(1);
    }

    @Test
    void changedFingerprintRerunsInsteadOfReusingStaleResult() throws Exception {
        JobManager jobs = new JobManager(PROPERTIES);
        AtomicInteger runs = new AtomicInteger();

        // 首跑：模拟真实踩坑现场——提交号还没配上，产物里没有变更清单
        jobs.submit(
                JobManager.JobKind.SCAN,
                "job-rerun",
                "fp-old",
                () -> {
                    runs.incrementAndGet();
                    return Map.of("job_id", "job-rerun", "marker", "stale");
                });
        JobRecord stale = awaitTerminal(jobs, JobManager.JobKind.SCAN, "job-rerun");

        // 输入变化（配好了提交号）：必须真的重跑并返回新产物，不能沿用旧的
        jobs.submit(
                JobManager.JobKind.SCAN,
                "job-rerun",
                "fp-new",
                () -> {
                    runs.incrementAndGet();
                    return Map.of("job_id", "job-rerun", "marker", "fresh");
                });
        JobRecord fresh = awaitTerminal(jobs, JobManager.JobKind.SCAN, "job-rerun");

        assertThat(fresh).isNotSameAs(stale);
        assertThat(fresh.result()).containsEntry("marker", "fresh");
        assertThat(runs).hasValue(2);
    }

    @Test
    void scanAndExtractShareJobIdWithoutCollision() throws Exception {
        JobManager jobs = new JobManager(PROPERTIES);

        jobs.submit(
                JobManager.JobKind.SCAN, "shared", "fp", () -> Map.of("job_id", "shared", "kind", "scan"));
        jobs.submit(
                JobManager.JobKind.EXTRACT,
                "shared",
                "fp",
                () -> Map.of("job_id", "shared", "kind", "extract"));

        assertThat(awaitTerminal(jobs, JobManager.JobKind.SCAN, "shared").result())
                .containsEntry("kind", "scan");
        assertThat(awaitTerminal(jobs, JobManager.JobKind.EXTRACT, "shared").result())
                .containsEntry("kind", "extract");
    }

    @Test
    void fingerprintTracksInputChanges() {
        assertThat(JobManager.fingerprintOf("a")).isEqualTo(JobManager.fingerprintOf("a"));
        assertThat(JobManager.fingerprintOf("a")).isNotEqualTo(JobManager.fingerprintOf("b"));
    }

    private static JobRecord awaitTerminal(JobManager jobs, JobManager.JobKind kind, String jobId)
            throws InterruptedException {
        for (int attempt = 0; attempt < 500; attempt++) {
            JobRecord record = jobs.get(kind, jobId);
            if (record != null
                    && (record.status() == JobStatus.completed || record.status() == JobStatus.failed)) {
                return record;
            }
            Thread.sleep(10);
        }
        throw new AssertionError("job 未在预期时间内进入终态：" + jobId);
    }
}
