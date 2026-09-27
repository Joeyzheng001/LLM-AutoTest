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
        @JsonProperty("include_paths") List<String> includePaths,
        @JsonProperty("commits") List<String> commits,
        @JsonProperty("workspace_revision") String workspaceRevision) {

    /** 兼容旧 5 参构造（无基线分支，C-09 前调用点）。 */
    public RepositoryInput(
            String name, String path, String branch, Boolean isFallback, List<String> includePaths) {
        this(name, path, branch, null, isFallback, includePaths, null, null);
    }

    /** 兼容旧 6 参构造（基线分支，无提交号）。 */
    public RepositoryInput(
            String name, String path, String branch, String baselineBranch,
            Boolean isFallback, List<String> includePaths) {
        this(name, path, branch, baselineBranch, isFallback, includePaths, null, null);
    }

    /** 兼容 7 参构造（提交号，无工作区修订）。 */
    public RepositoryInput(
            String name, String path, String branch, String baselineBranch,
            Boolean isFallback, List<String> includePaths, List<String> commits) {
        this(name, path, branch, baselineBranch, isFallback, includePaths, commits, null);
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

    /** 提交号缺省为空（未指定时不圈定变更范围，退回基线分支 diff 或全仓）。 */
    public List<String> commits() {
        return commits == null ? List.of() : commits;
    }
}
