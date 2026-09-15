// 对应契约 RepositoryInput（07 遗留事项 #1：路径 + 分支输入口径）。
package com.llmautotest.parser.contract;

import com.fasterxml.jackson.annotation.JsonProperty;

/** 扫描/提取请求的仓库输入项。 */
public record RepositoryInput(
        String name,
        String path,
        String branch,
        @JsonProperty("is_fallback") Boolean isFallback,
        @JsonProperty("include_paths") java.util.List<String> includePaths) {

    /** 兜底标记缺省为 false（Jackson 反序列化 null 时归一）。 */
    public boolean fallback() {
        return isFallback != null && isFallback;
    }

    /** include_paths 缺省为空（全仓库扫描）。 */
    public java.util.List<String> includes() {
        return includePaths == null ? java.util.List.of() : includePaths;
    }
}
