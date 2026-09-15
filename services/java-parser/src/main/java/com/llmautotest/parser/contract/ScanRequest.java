// 对应契约 ScanRequest（F-CODE-01~03 多仓库扫描请求）。
package com.llmautotest.parser.contract;

import java.util.List;

/** POST /api/v1/scan-jobs 请求体。 */
public record ScanRequest(String job_id, List<RepositoryInput> repositories) {
}
