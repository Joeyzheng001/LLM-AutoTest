// 对应契约 JobStatus 枚举（queued / running / completed / failed）。
package com.llmautotest.parser.contract;

/** 异步 job 状态机（终态为 completed / failed）。 */
public enum JobStatus {
    queued,
    running,
    completed,
    failed
}
