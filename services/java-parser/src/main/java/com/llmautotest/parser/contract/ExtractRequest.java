// 对应契约 ExtractRequest（R-REV-02 第①步静态特征定位请求）。
package com.llmautotest.parser.contract;

import java.util.List;

/** POST /api/v1/extract-jobs 请求体。 */
public record ExtractRequest(String job_id, List<RepositoryInput> repositories, List<String> protocols) {
}
