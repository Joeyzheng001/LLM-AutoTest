// 对应契约 RepositoryInput（07 遗留事项 #1：路径 + 分支；C-09：基线分支可选）。
package com.llmautotest.parser.contract;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/** 扫描/提取请求的仓库输入项。 */
public record RepositoryInput(
        String name,
        String path,
        String branch,
        @JsonProperty("baseline_branch") String baselineBranch,
        @JsonProperty("is_fallback") Boolean isFallback,
        @JsonProperty("include_paths") List<String> includePaths) {

    /** 兼容旧 5 参构造（无基线分支，C-09 前调用点）。 */
    public RepositoryInput(
            String name, String path, String branch, Boolean isFallback, List<String> includePaths) {
        this(name, path, branch, null, isFallback, includePaths);
    }

    /** 基线分支缺省为不比对（null/空白均视为未指定）。 */
    public String baseline() {
        return baselineBranch == null || baselineBranch.isBlank() ? null : baselineBranch;
    }

    /** 兜底标记缺省为 false（Jackson 反序列化 null 时归一）。 */
    public boolean fallback() {
        return isFallback != null && isFallback;
    }

    /** include_paths 缺省为空（全仓库扫描）。 */
    public List<String> includes() {
        return includePaths == null ? List.of() : includePaths;
    }
}
