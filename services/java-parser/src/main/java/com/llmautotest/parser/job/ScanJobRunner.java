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
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.springframework.stereotype.Component;

/** 扫描 job 执行器：目录遍历 + 文件计数 + 编码探测（不做语法分析）+ C-09 双分支 diff。 */
@Component
public class ScanJobRunner {

    /** 根提交没有父提交时用作 diff 左端的空树（git 的空 tree 对象）。 */
    private static final String EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";

    /** `@@ -a,b +c,d @@`：捕获旧/新两侧的起始行与行数（行数可省略，默认 1）。 */
    private static final Pattern HUNK_HEADER =
            Pattern.compile("^@@ -(\\d+)(?:,(\\d+))? \\+(\\d+)(?:,(\\d+))? @@");

    private final ParserProperties properties;

    /** git 调用统一入口（超时可配、并发排空管道、失败消息带命令名）。 */
    private final GitRunner git;

    public ScanJobRunner(ParserProperties properties) {
        this.properties = properties;
        this.git = new GitRunner(properties.gitTimeoutSeconds());
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
        Path root = Path.of(repo.path());
        // 路径基数校验：git diff 输出的是**仓库 toplevel** 相对路径，而 files 列表是 root
        // 相对路径。root 若不是 toplevel（把仓库的子目录配成仓库），两者基准不同 → 变更清单
        // 永远匹配不上候选 → 静默 0 接口。宁停不猜。
        String topLevelError = topLevelMismatch(root);
        if (topLevelError != null) {
            entry.put("status", "error");
            entry.put("file_count", 0);
            entry.put("files", List.of());
            entry.put("error", Map.of("code", "REPO_PATH_NOT_TOPLEVEL", "message", topLevelError));
            return entry;
        }
        ChangeRange range = null;
        if (!repo.commits().isEmpty()) {
            // C-09 扩展：指定提交号 → 由这几个提交推出 base..target 单一区间。
            entry.put("commits", repo.commits());
            try {
                range = commitsRange(root, repo.commits());
            } catch (IOException | InterruptedException ex) {
                if (ex instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                entry.put("status", "error");
                entry.put("file_count", 0);
                entry.put("files", List.of());
                entry.put("error", Map.of(
                        "code", "COMMIT_DIFF_FAILED",
                        "message", "提交号圈定变更范围失败（非 Git 仓库、提交不存在，或它们不在"
                                + "同一条提交链上——行号只在单一版本上有意义，拒绝近似）："
                                + String.join(",", repo.commits())
                                + "；原因：" + reasonOf(ex),
                        "details", Map.of("stderr", ex.getMessage() == null ? "" : ex.getMessage())));
                return entry;
            }
        } else if (repo.baseline() != null) {
            // C-09：指定基线分支 → diff 失败归仓库级 X-CODE-01 宁停不猜（合法路径检查前）
            entry.put("baseline_branch", repo.baseline());
            try {
                range = branchesRange(root, repo.baseline(), repo.branch());
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
        if (range != null) {
            entry.put("base_ref", range.base());
            entry.put("target_ref", range.target());
            // 解析成提交号：extract 要按 target_sha 检出只读快照解析（否则候选行号与 diff
            // 行号不同版本）。解析失败时原样透传，让下游如实失败而不是静默降级。
            entry.put("base_sha", resolveSha(root, range.base()));
            entry.put("target_sha", resolveSha(root, range.target()));
            try {
                entry.put("changed_files", nameStatus(root, range));
                entry.put("changed_lines", lineRanges(root, range));
            } catch (IOException | InterruptedException ex) {
                if (ex instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                entry.put("status", "error");
                entry.put("file_count", 0);
                entry.put("files", List.of());
                entry.put("error", Map.of(
                        "code", "DIFF_RANGE_FAILED",
                        "message", "变更范围 diff 失败：" + range.base() + ".." + range.target(),
                        "details", Map.of("stderr", ex.getMessage() == null ? "" : ex.getMessage())));
                return entry;
            }
        }
        entry.put("is_fallback", repo.fallback());
        if (!Files.isDirectory(root)) {
            // X-CODE-01：仓库级硬错误（调用方按契约判失败）
            entry.put("status", "error");
            entry.put("file_count", 0);
            entry.put("error", Map.of(
                    "code", "REPO_PATH_NOT_FOUND",
                    "message", "仓库路径不存在或不是目录"));
            return entry;
        }

        // 文件清点必须在 target_sha 快照上做，与 ExtractJobRunner 同源（RepoSandbox）。
        // 只扫工作区会让 file_count 与实际提取的版本错位——工作区领先于配置提交时，
        // 下游完整性门禁拿两个版本的计数做等值比较，会误判成「接口成批缺失」并硬拦。
        // 注：上面的 nameStatus/lineRanges 是提交对提交的 diff，不受检出目录影响。
        String targetSha = (String) entry.get("target_sha");
        if (targetSha != null && !targetSha.isBlank()) {
            try {
                root = RepoSandbox.at(git, root, repo, targetSha);
                entry.put("snapshot", true);
                entry.put("snapshot_path", root.toString());
            } catch (IOException | InterruptedException ex) {
                if (ex instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                // 快照建不出来时如实报错，不静默回退工作区（否则又回到版本错位）。
                entry.put("status", "error");
                entry.put("file_count", 0);
                entry.put("files", List.of());
                entry.put("error", Map.of(
                        "code", "SNAPSHOT_FAILED",
                        "message", "按 target_sha 检出只读快照失败（" + targetSha + "）："
                                + reasonOf(ex)));
                return entry;
            }
        }

        List<Path> javaFiles = listSourceFiles(root, repo.includes());
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

    private List<Path> listSourceFiles(Path root, List<String> includes) throws IOException {
        try (Stream<Path> stream = Files.walk(root)) {
            return stream
                    .filter(Files::isRegularFile)
                    .filter(p -> {
                        String name = p.getFileName().toString();
                        return name.endsWith(".java") || name.endsWith(".proto");
                    })
                    .filter(p -> inScope(root.relativize(p), includes))
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        }
    }

    private static boolean inScope(Path relative, List<String> includes) {
        if (includes.isEmpty()) {
            return true;
        }
        // 规范化后比较：includes 是配置里的字符串（正斜杠语义），Path 在 Windows 上按
        // 反斜杠切分，Path.startsWith("a/b") 会把 "a/b" 当成单个段而永不命中。
        String normalized = relative.toString().replace('\\', '/');
        for (String include : includes) {
            if (normalized.startsWith(include.replace('\\', '/'))) {
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
                // 统一正斜杠：files[].path 会与 changed_files（git 文本输出，恒为正斜杠）
                // 及候选 code_location.file 做字符串比较，风格不一致会导致匹配失败。
                "path", relative.toString().replace('\\', '/'));
    }

    /**
     * 变更范围（单一 base..target 区间）。
     *
     * <p>**为什么必须是一个区间**：行号只在确定版本里有意义。早先按提交逐个 diff 再取并集，
     * 得到的是"各提交各自新版本"的混合，行号无法与任何单一版本的源码对齐，也就无法做
     * 行级精确判定。改为先由配置推出唯一的 {@code base} 与 {@code target}，再一次性 diff。
     */
    private record ChangeRange(String base, String target) {}

    /**
     * C-09：基线→被测分支。base 取 {@code merge-base(baseline, target)}（三点口径），
     * target 取配置里的 branch。
     */
    private ChangeRange branchesRange(Path root, String baseline, String target)
            throws IOException, InterruptedException {
        String base = runGit(root, "merge-base", baseline, target).trim();
        if (base.isEmpty()) {
            throw new IOException("merge-base 为空：" + baseline + " vs " + target);
        }
        return new ChangeRange(base, target);
    }

    /**
     * C-09 扩展：由多个提交号推出 base..target。
     *
     * <p>{@code base} = 拓扑最早那个提交的父提交（根提交用空树），{@code target} = 拓扑最晚那个。
     * **这些提交必须在同一条链上**，否则无法用一个区间表达——此时宁停不猜：行级判定要求
     * 「diff 的新版本」与「解析源码的版本」严格同一，跨版本映射行号会引入近似。
     * 报错信息会列出区间内多出来的提交，便于调用方补齐或改用基线模式。
     */
    private ChangeRange commitsRange(Path root, List<String> commits)
            throws IOException, InterruptedException {
        List<String> full = new ArrayList<>();
        for (String commit : commits) {
            full.add(runGit(root, "rev-parse", "--verify", commit + "^{commit}").trim());
        }
        Set<String> wanted = new LinkedHashSet<>(full);

        // rev-list --topo-order 越新的越靠前；只保留本次指定的提交，顺序即为链上的新旧顺序。
        List<String> revListArgs = new ArrayList<>(List.of("rev-list", "--topo-order"));
        revListArgs.addAll(full);
        String listed = runGit(root, revListArgs.toArray(String[]::new));
        List<String> ordered = new ArrayList<>();
        for (String line : listed.split("\\R")) {
            String sha = line.trim();
            if (wanted.contains(sha) && !ordered.contains(sha)) {
                ordered.add(sha);
            }
        }
        if (ordered.size() != wanted.size()) {
            throw new IOException("有提交无法在给定的 ref 集合中定位："
                    + String.join(",", commits) + "（实际解析到 " + ordered.size() + " 个）");
        }

        String target = ordered.get(0);
        String earliest = ordered.get(ordered.size() - 1);
        String base = parentOrEmptyTree(root, earliest);

        // 链上除了指定提交之外还有别的提交时拒绝：那会让"你选的范围"与"diff 的范围"不一致。
        String span = runGit(root, "rev-list", base + ".." + target);
        Set<String> spanSet = new LinkedHashSet<>();
        for (String line : span.split("\\R")) {
            if (!line.isBlank()) {
                spanSet.add(line.trim());
            }
        }
        if (!spanSet.equals(wanted)) {
            List<String> extra = new ArrayList<>(spanSet);
            extra.removeAll(wanted);
            throw new IOException("指定的提交不是一段连续区间（区间内还夹着 "
                    + extra.size() + " 个提交：" + String.join(",", extra)
                    + "）。请把它们一并列入，或改用基线分支模式");
        }
        return new ChangeRange(base, target);
    }

    /** 失败原因摘要（进 error.message，便于不翻 details 也能看懂）。 */
    private static String reasonOf(Exception ex) {
        String message = ex.getMessage() == null ? "" : ex.getMessage().replaceAll("\\s+", " ").trim();
        if (message.isEmpty()) {
            return ex.getClass().getSimpleName();
        }
        return message.length() > 300 ? message.substring(0, 300) + "…" : message;
    }

    /** 仓库路径不是 Git 顶层目录时返回原因，否则返回 null（非 Git 仓库不拦：由 diff 环节报错）。
     *
     * <p>为什么必须拦：{@code git diff} 的路径以**仓库顶层**为基准，`files` 列表以配置的
     * `path` 为基准。配了子目录时两套路径基数不同，变更清单里的 `module/.../A.java` 与
     * 候选里的 `A.java` 永远匹配不上 → 过滤后 0 接口，而且**不报任何错**。
     */
    private String topLevelMismatch(Path root) {
        if (!Files.isDirectory(root)) {
            return null; // 交给后续 REPO_PATH_NOT_FOUND
        }
        String top;
        try {
            top = runGit(root, "rev-parse", "--show-toplevel").trim();
        } catch (IOException | InterruptedException ex) {
            if (ex instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            return null; // 非 Git 仓库：diff 环节会如实报错
        }
        if (top.isEmpty()) {
            return null;
        }
        try {
            Path topPath = Path.of(top).toRealPath();
            Path rootPath = root.toRealPath();
            if (!topPath.equals(rootPath)) {
                return "仓库路径不是 Git 顶层目录：配置 " + rootPath + "，实际顶层 " + topPath
                        + "。git diff 的路径以顶层为基准，与源码列表基准不同，"
                        + "变更清单会匹配不上接口候选（静默 0 接口）。请把 path 指向顶层";
            }
        } catch (IOException ex) {
            return null; // 无法解析真实路径时不做判定，避免误拦
        }
        return null;
    }

    /** ref → 提交号（已解析过的 SHA / 空树原样透传，不抛错）。 */
    private String resolveSha(Path root, String ref) {
        try {
            String sha = runGit(root, "rev-parse", "--verify", ref + "^{commit}").trim();
            return sha.isEmpty() ? ref : sha;
        } catch (IOException | InterruptedException ex) {
            if (ex instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            return ref;
        }
    }

    private String parentOrEmptyTree(Path root, String commit) throws IOException, InterruptedException {        try {
            String parent = runGit(root, "rev-parse", "--verify", commit + "^^{commit}").trim();
            return parent.isEmpty() ? EMPTY_TREE : parent;
        } catch (IOException ex) {
            // 根提交：左端用空树，等价于"该提交引入的全部内容"
            return EMPTY_TREE;
        }
    }

    /** 变更文件清单（单一区间，每个路径只出现一次，不再出现「同时在 added 与 modified」）。 */
    private Map<String, Object> nameStatus(Path root, ChangeRange range)
            throws IOException, InterruptedException {
        return parseNameStatus(runGit(
                root, "diff", "--name-status", range.base(), range.target()));
    }

    /** 变更行区间：{路径: {"added": [[起,止]...], "removed": [[起,止]...]}}，行号为新版本。 */
    private Map<String, Object> lineRanges(Path root, ChangeRange range)
            throws IOException, InterruptedException {
        return parseUnifiedZero(runGit(
                root, "diff", "--unified=0", "--no-color", "--no-ext-diff",
                range.base(), range.target()));
    }

    /**
     * 解析 {@code git diff --unified=0} 的 hunk 头，得到每个文件的新/旧版本变更行区间。
     *
     * <p>纯删除的文件以旧路径登记（新版本不存在）。行数省略表示 1；行数为 0 表示该侧无行
     * （例如纯删除的新侧）——此时不登记区间，但仍要保留该路径的条目，否则「文件变了但取不到
     * 行号」会被下游误读成「没变」。
     */
    private static Map<String, Object> parseUnifiedZero(String stdout) {
        Map<String, Object> result = new LinkedHashMap<>();
        String oldPath = null;
        String newPath = null;
        String key = null;
        for (String line : stdout.split("\\R")) {
            if (line.startsWith("--- ")) {
                oldPath = stripDiffPrefix(line.substring(4).trim());
                continue;
            }
            if (line.startsWith("+++ ")) {
                newPath = stripDiffPrefix(line.substring(4).trim());
                key = newPath != null ? newPath : oldPath;
                if (key != null && !result.containsKey(key)) {
                    Map<String, Object> ranges = new LinkedHashMap<>();
                    ranges.put("added", new ArrayList<List<Integer>>());
                    ranges.put("removed", new ArrayList<List<Integer>>());
                    result.put(key, ranges);
                }
                continue;
            }
            if (!line.startsWith("@@") || key == null) {
                continue;
            }
            Matcher matcher = HUNK_HEADER.matcher(line);
            if (!matcher.find()) {
                continue;
            }
            addRange(result, key, "removed", matcher.group(1), matcher.group(2));
            addRange(result, key, "added", matcher.group(3), matcher.group(4));
        }
        return result;
    }

    private static void addRange(
            Map<String, Object> result, String key, String side, String start, String count) {
        int lines = count == null ? 1 : Integer.parseInt(count);
        if (lines <= 0) {
            return;
        }
        int from = Integer.parseInt(start);
        List<List<Integer>> bucket = bucketOf(result, key, side);
        bucket.add(List.of(from, from + lines - 1));
    }

    @SuppressWarnings("unchecked")
    private static List<List<Integer>> bucketOf(Map<String, Object> result, String key, String side) {
        Map<String, Object> ranges = (Map<String, Object>) result.get(key);
        return (List<List<Integer>>) ranges.get(side);
    }

    /** {@code a/x} → {@code x}；{@code /dev/null} → null。 */
    private static String stripDiffPrefix(String value) {
        if (value.isEmpty() || "/dev/null".equals(value)) {
            return null;
        }
        if (value.startsWith("a/") || value.startsWith("b/")) {
            return value.substring(2);
        }
        return value;
    }

    /**
     * 解析 {@code git diff --name-status} 输出。
     *
     * <p>状态分类：A/C→added；M/T→modified；D→removed；R(ename)→旧路径 removed + 新路径 added。
     * 单一区间下每个路径只出现一次，不会同时落进 added 与 modified。
     */
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

    /**
     * 执行 git 并返回 stdout；非 0 退出码抛 IOException（stderr 作消息）。
     *
     * <p>实现委托 {@link GitRunner}（两条管道并发排空 + 可配超时 + 消息带命令名）。
     */
    private String runGit(Path root, String... args) throws IOException, InterruptedException {
        return git.run(root, args);
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
