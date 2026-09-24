// 对应契约 ScanJob：多仓库源码扫描 → code_inventory（F-CODE-01~03，R-CODE-03）。
// 四态口径：ok / empty（X-CODE-02 流程继续）/ truncated（X-CODE-03）/ error（X-CODE-01）。
package com.llmautotest.parser.job;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.RepositoryInput;
import com.llmautotest.parser.contract.ScanRequest;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;
import org.springframework.stereotype.Component;

/** 扫描 job 执行器：目录遍历 + 文件计数 + 编码探测（不做语法分析）+ C-09 双分支 diff。 */
@Component
public class ScanJobRunner {

    private static final Duration GIT_TIMEOUT = Duration.ofSeconds(30);

    private final ParserProperties properties;

    public ScanJobRunner(ParserProperties properties) {
        this.properties = properties;
    }

    /** 执行扫描，返回契约 ScanJob 产物载荷（含 code_inventory）。 */
    public Map<String, Object> run(ScanRequest request) throws IOException {
        List<Map<String, Object>> repositories = new ArrayList<>();
        for (RepositoryInput repo : request.repositories()) {
            repositories.add(scanRepository(repo));
        }
        Map<String, Object> codeInventory = new LinkedHashMap<>();
        codeInventory.put("schema_version", "1.0");
        codeInventory.put("job_id", request.job_id());
        codeInventory.put("repositories", repositories);
        return codeInventory;
    }

    private Map<String, Object> scanRepository(RepositoryInput repo) throws IOException {
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("name", repo.name());
        entry.put("path", repo.path());
        entry.put("branch", repo.branch());
        if (!repo.commits().isEmpty()) {
            // C-09 扩展：指定提交号 → 按提交 diff 并集圈定变更范围（优先于基线分支）。
            entry.put("commits", repo.commits());
            try {
                entry.put("changed_files", diffCommits(Path.of(repo.path()), repo.commits()));
            } catch (IOException | InterruptedException ex) {
                if (ex instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                entry.put("status", "error");
                entry.put("file_count", 0);
                entry.put("files", List.of());
                entry.put("error", Map.of(
                        "code", "COMMIT_DIFF_FAILED",
                        "message", "提交号比对失败（非 Git 仓库或提交不存在）："
                                + String.join(",", repo.commits()),
                        "details", Map.of("stderr", ex.getMessage() == null ? "" : ex.getMessage())));
                return entry;
            }
        } else if (repo.baseline() != null) {
            // C-09：指定基线分支 → diff 失败归仓库级 X-CODE-01 宁停不猜（合法路径检查前）
            entry.put("baseline_branch", repo.baseline());
            try {
                entry.put("changed_files", diffBranches(Path.of(repo.path()), repo.baseline(), repo.branch()));
            } catch (IOException | InterruptedException ex) {
                if (ex instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                entry.put("status", "error");
                entry.put("file_count", 0);
                entry.put("files", List.of());
                entry.put("error", Map.of(
                        "code", "BRANCH_DIFF_FAILED",
                        "message", "基线分支比对失败（非 Git 仓库或 ref 不存在）："
                                + repo.baseline() + "..." + repo.branch(),
                        "details", Map.of("stderr", ex.getMessage() == null ? "" : ex.getMessage())));
                return entry;
            }
        }
        entry.put("is_fallback", repo.fallback());
        Path root = Path.of(repo.path());
        if (!Files.isDirectory(root)) {
            // X-CODE-01：仓库级硬错误（调用方按契约判失败）
            entry.put("status", "error");
            entry.put("file_count", 0);
            entry.put("error", Map.of(
                    "code", "REPO_PATH_NOT_FOUND",
                    "message", "仓库路径不存在或不是目录"));
            return entry;
        }

        List<Path> javaFiles = listJavaFiles(root, repo.includes());
        List<String> encodingSkipped = new ArrayList<>();
        for (Path file : javaFiles) {
            if (!isDecodableUtf8(file)) {
                // R-CODE-03：异常编码记录但跳过（不中断整体）
                encodingSkipped.add(root.relativize(file).toString());
            }
        }

        List<Map<String, Object>> files = new ArrayList<>();
        boolean truncated = javaFiles.size() > properties.maxFilesPerRepo();
        List<Path> scanned = truncated ? javaFiles.subList(0, properties.maxFilesPerRepo()) : javaFiles;
        for (Path file : scanned) {
            files.add(sourceFileEntry(repo.name(), root.relativize(file)));
        }

        entry.put("status", statusOf(javaFiles.size(), truncated));
        entry.put("file_count", scanned.size());
        if (truncated) {
            // X-CODE-03：注明截断后实际扫描范围
            entry.put("truncated_scope", repo.includes().isEmpty() ? List.of(".") : repo.includes());
        }
        if (!encodingSkipped.isEmpty()) {
            entry.put("encoding_skipped", encodingSkipped);
        }
        entry.put("files", files);
        return entry;
    }

    private static String statusOf(int total, boolean truncated) {
        if (total == 0) {
            return "empty"; // X-CODE-02：空仓库如实落盘，流程继续
        }
        return truncated ? "truncated" : "ok";
    }

    private List<Path> listJavaFiles(Path root, List<String> includes) throws IOException {
        try (Stream<Path> stream = Files.walk(root)) {
            return stream
                    .filter(Files::isRegularFile)
                    .filter(p -> p.getFileName().toString().endsWith(".java"))
                    .filter(p -> inScope(root.relativize(p), includes))
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        }
    }

    private static boolean inScope(Path relative, List<String> includes) {
        if (includes.isEmpty()) {
            return true;
        }
        for (String include : includes) {
            if (relative.startsWith(include)) {
                return true;
            }
        }
        return false;
    }

    private static Map<String, Object> sourceFileEntry(String repo, Path relative) {
        // F-CODE-03：模块取 Maven 结构第一层（无模块结构时取顶层目录名；根下文件记仓库名）
        String module = relative.getNameCount() > 1
                ? relative.getName(0).toString()
                : repo;
        return Map.of(
                "repo", repo,
                "module", module,
                "path", relative.toString());
    }

    /**
     * C-09：基线→被测分支变更文件清单（git diff --name-status，merge-base 口径，不动工作区）。
     *
     * <p>状态分类：A/C→added；M/T→modified；D→removed；R(ename)→旧路径 removed + 新路径 added。
     * 参数经 ProcessBuilder 直传不经 shell，分支名无注入面。
     */
    private Map<String, Object> diffBranches(Path root, String baseline, String target)
            throws IOException, InterruptedException {
        return parseNameStatus(runGitNameStatus(root, "diff", "--name-status", baseline + "..." + target));
    }

    /**
     * C-09 扩展：按多个提交号圈定变更范围（每个提交相对其父提交的 diff 并集）。
     *
     * <p>一个分支常承载多个功能改造，按分支 diff 会圈进无关改动；此处对每个提交
     * 分别取 {@code git diff <commit>^ <commit>} 再合并去重，使变更范围精确到提交粒度。
     * 根提交无父提交时退化为 {@code git show --name-status} 取该提交全量变更。
     */
    private Map<String, Object> diffCommits(Path root, List<String> commits)
            throws IOException, InterruptedException {
        Map<String, Object> merged = emptyNameStatus();
        for (String commit : commits) {
            Map<String, Object> one;
            try {
                one = parseNameStatus(runGitNameStatus(root, "diff", "--name-status", commit + "^", commit));
            } catch (IOException ex) {
                // 根提交没有父提交：退化为 git show 该提交的完整变更
                one = parseNameStatus(runGitNameStatus(root, "show", "--name-status", "--format=", commit));
            }
            mergeNameStatus(merged, one);
        }
        return merged;
    }

    private static Map<String, Object> emptyNameStatus() {
        Map<String, Object> changed = new LinkedHashMap<>();
        changed.put("added", new ArrayList<String>());
        changed.put("modified", new ArrayList<String>());
        changed.put("removed", new ArrayList<String>());
        return changed;
    }

    private static Map<String, Object> parseNameStatus(String stdout) {
        List<String> added = new ArrayList<>();
        List<String> modified = new ArrayList<>();
        List<String> removed = new ArrayList<>();
        for (String line : stdout.split("\\R+")) {
            if (line.isBlank()) {
                continue;
            }
            String[] parts = line.split("\\t");
            if (parts.length < 2) {
                continue;
            }
            char kind = parts[0].charAt(0);
            switch (kind) {
                case 'A', 'C' -> added.add(parts[1]);
                case 'M', 'T' -> modified.add(parts[1]);
                case 'D' -> removed.add(parts[1]);
                case 'R' -> { // rename：旧路径 removed + 新路径 added（文件级语义）
                    removed.add(parts[1]);
                    if (parts.length >= 3) {
                        added.add(parts[2]);
                    }
                }
                default -> {
                    // 未知状态码归 modified（保守：变更文件不遗漏）
                    modified.add(parts[1]);
                }
            }
        }
        Map<String, Object> changed = new LinkedHashMap<>();
        changed.put("added", added);
        changed.put("modified", modified);
        changed.put("removed", removed);
        return changed;
    }

    private static void mergeNameStatus(Map<String, Object> into, Map<String, Object> from) {
        mergeUnique((List<String>) into.get("added"), (List<String>) from.get("added"));
        mergeUnique((List<String>) into.get("modified"), (List<String>) from.get("modified"));
        mergeUnique((List<String>) into.get("removed"), (List<String>) from.get("removed"));
    }

    private static void mergeUnique(List<String> into, List<String> from) {
        for (String value : from) {
            if (!into.contains(value)) {
                into.add(value);
            }
        }
    }

    private String runGitNameStatus(Path root, String... args) throws IOException, InterruptedException {
        List<String> command = new ArrayList<>();
        command.add("git");
        command.add("-C");
        command.add(root.toString());
        command.addAll(List.of(args));
        Process process = new ProcessBuilder(command).start();
        String stdout;
        try {
            if (!process.waitFor(GIT_TIMEOUT.toSeconds(), TimeUnit.SECONDS)) {
                process.destroyForcibly();
                throw new IOException("git 超时（>" + GIT_TIMEOUT.toSeconds() + "s）");
            }
            if (process.exitValue() != 0) {
                throw new IOException(new String(process.getErrorStream().readAllBytes(), StandardCharsets.UTF_8).trim());
            }
            stdout = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        } finally {
            process.getInputStream().close();
            process.getErrorStream().close();
        }
        return stdout;
    }

    private static boolean isDecodableUtf8(Path file) {
        try {
            byte[] bytes = Files.readAllBytes(file);
            StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes));
            return true;
        } catch (IOException ex) {
            return false;
        }
    }
}
