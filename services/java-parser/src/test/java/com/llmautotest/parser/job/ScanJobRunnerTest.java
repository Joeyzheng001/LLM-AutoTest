// ScanJobRunner 单测：四态口径 + 编码跳过 + 截断 + 模块归属（F-CODE-01~03）。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.RepositoryInput;
import com.llmautotest.parser.contract.ScanRequest;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ScanJobRunnerTest {

    @TempDir
    Path tempDir;

    private final ScanJobRunner runner = new ScanJobRunner(ParserProperties.of(2, 20000));

    @Test
    void scansOkRepositoryWithModuleLayout() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("user-service"));
        Files.createDirectories(repo.resolve("api/src/main/java"));
        Files.writeString(repo.resolve("api/src/main/java/A.java"), "class A {}\n");
        Files.writeString(repo.resolve("B.java"), "class B {}\n");

        Map<String, Object> inventory = runner.run(request(repo, false, List.of()));

        @SuppressWarnings("unchecked")
        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        assertThat(entry.get("file_count")).isEqualTo(2);
        assertThat(entry.get("is_fallback")).isEqualTo(false);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> files = (List<Map<String, Object>>) entry.get("files");
        // B.java 在仓库根下（模块取仓库名）；A.java 在 Maven 模块 api 下
        assertThat(files).extracting(f -> f.get("module")).containsExactly("user-service", "api");
        assertThat(files).extracting(f -> f.get("path"))
                .containsExactly("B.java", "api/src/main/java/A.java");
    }

    @Test
    void emptyRepositoryReportsEmptyStatus() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("empty-repo"));

        Map<String, Object> inventory = runner.run(request(repo, false, List.of()));

        assertThat(onlyRepository(inventory).get("status")).isEqualTo("empty");
        assertThat(onlyRepository(inventory).get("file_count")).isEqualTo(0);
    }

    @Test
    void missingPathReportsErrorWithStableCode() throws IOException {
        RepositoryInput repo = new RepositoryInput(
                "ghost", tempDir.resolve("nope").toString(), "main", null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-x", List.of(repo)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("error");
        @SuppressWarnings("unchecked")
        Map<String, Object> error = (Map<String, Object>) entry.get("error");
        assertThat(error.get("code")).isEqualTo("REPO_PATH_NOT_FOUND");
    }

    @Test
    void overCapBecomesTruncatedWithScope() throws IOException {
        ScanJobRunner capped = new ScanJobRunner(ParserProperties.of(2, 1));
        Path repo = Files.createDirectories(tempDir.resolve("big"));
        Files.writeString(repo.resolve("A.java"), "class A {}\n");
        Files.writeString(repo.resolve("B.java"), "class B {}\n");

        Map<String, Object> inventory = capped.run(request(repo, false, List.of()));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("truncated");
        assertThat(entry.get("file_count")).isEqualTo(1);
        assertThat((List<Object>) entry.get("truncated_scope")).containsExactly((Object) ".");
    }

    @Test
    void invalidUtf8FileRecordedAsEncodingSkipped() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("legacy"));
        Files.writeString(repo.resolve("Good.java"), "class Good {}\n");
        // 0xFF 是非法 UTF-8 单字节（R-CODE-03：记录但跳过）
        Files.write(repo.resolve("Bad.java"), new byte[] {(byte) 0xFF, (byte) 0xFE, 'x'});

        Map<String, Object> inventory = runner.run(request(repo, false, List.of()));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        assertThat((List<Object>) entry.get("encoding_skipped")).containsExactly((Object) "Bad.java");
    }

    @Test
    void includePathsLimitScanScope() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("scoped"));
        Files.createDirectories(repo.resolve("src/main/java"));
        Files.createDirectories(repo.resolve("test"));
        Files.writeString(repo.resolve("src/main/java/In.java"), "class In {}\n");
        Files.writeString(repo.resolve("test/Out.java"), "class Out {}\n");
        Files.writeString(repo.resolve("Root.java"), "class Root {}\n");

        Map<String, Object> inventory =
                runner.run(request(repo, false, List.of("src/main/java")));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        assertThat((List<?>) entry.get("truncated_scope")).isNull();
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> files = (List<Map<String, Object>>) entry.get("files");
        assertThat(files).extracting(f -> f.get("path")).containsExactly("src/main/java/In.java");
    }

    @Test
    void isFallbackAndJobIdPassthrough() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("fallback"));
        Files.writeString(repo.resolve("A.java"), "class A {}\n");

        Map<String, Object> inventory = runner.run(request(repo, true, List.of()));

        assertThat(inventory.get("schema_version")).isEqualTo("1.0");
        assertThat(inventory.get("job_id")).isEqualTo("job-1");
        assertThat(onlyRepository(inventory).get("is_fallback")).isEqualTo(true);
        // C-09 向后兼容：未指定基线分支 → 不产出 baseline_branch/changed_files 键
        assertThat(onlyRepository(inventory).get("changed_files")).isNull();
        assertThat(onlyRepository(inventory).get("baseline_branch")).isNull();
    }

    // ---- C-09 双分支 diff ----

    @Test
    void baselineDiffProducesChangedFiles() throws Exception {
        Path repo = gitRepoWithFeatureBranch();
        RepositoryInput input = new RepositoryInput(
                "diffed", repo.toString(), "feature", "main", null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-d", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        assertThat(entry.get("baseline_branch")).isEqualTo("main");
        @SuppressWarnings("unchecked")
        Map<String, Object> changed = (Map<String, Object>) entry.get("changed_files");
        assertThat((List<Object>) changed.get("added")).containsExactly((Object) "C.java");
        assertThat((List<Object>) changed.get("modified")).containsExactly((Object) "A.java");
        assertThat((List<Object>) changed.get("removed")).containsExactly((Object) "B.java");
    }

    @Test
    void baselineDiffOnNonGitRepoReportsError() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("plain"));
        Files.writeString(repo.resolve("A.java"), "class A {}\n");
        RepositoryInput input = new RepositoryInput(
                "plain", repo.toString(), "main", "main", null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-e", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("error");
        assertThat(entry.get("file_count")).isEqualTo(0);
        @SuppressWarnings("unchecked")
        Map<String, Object> error = (Map<String, Object>) entry.get("error");
        assertThat(error.get("code")).isEqualTo("BRANCH_DIFF_FAILED");
    }

    @Test
    void baselineDiffOnUnknownRefReportsError() throws Exception {
        Path repo = gitRepoWithFeatureBranch();
        RepositoryInput input = new RepositoryInput(
                "diffed", repo.toString(), "feature", "no-such-ref", null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-f", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("error");
        @SuppressWarnings("unchecked")
        Map<String, Object> error = (Map<String, Object>) entry.get("error");
        assertThat(error.get("code")).isEqualTo("BRANCH_DIFF_FAILED");
    }

    // ---- C-09 扩展：按提交号圈定变更范围 ----

    @Test
    void commitDiffProducesUnionOfChangedFiles() throws Exception {
        Path repo = gitRepoWithMultipleCommits();
        // 按提交粒度圈定：HEAD~1（加 A）+ HEAD（加 B）的 diff 并集，与分支 diff 无关
        RepositoryInput input = new RepositoryInput(
                "multi", repo.toString(), "main", null, null, null, List.of("HEAD~1", "HEAD"));

        Map<String, Object> inventory = runner.run(new ScanRequest("job-m", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        @SuppressWarnings("unchecked")
        Map<String, Object> changed = (Map<String, Object>) entry.get("changed_files");
        assertThat((List<Object>) changed.get("added")).containsExactly("A.java", "B.java");
        assertThat((List<Object>) changed.get("modified")).isEmpty();
        assertThat((List<Object>) changed.get("removed")).isEmpty();
        // 未指定基线分支 → 不产出 baseline_branch；产出提交号清单
        assertThat(entry.get("baseline_branch")).isNull();
        assertThat((List<Object>) entry.get("commits")).containsExactly("HEAD~1", "HEAD");
    }

    @Test
    void commitDiffOnUnknownCommitReportsError() throws Exception {
        Path repo = gitRepoWithMultipleCommits();
        RepositoryInput input = new RepositoryInput(
                "multi", repo.toString(), "main", null, null, null, List.of("no-such-commit"));

        Map<String, Object> inventory = runner.run(new ScanRequest("job-n", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("error");
        @SuppressWarnings("unchecked")
        Map<String, Object> error = (Map<String, Object>) entry.get("error");
        assertThat(error.get("code")).isEqualTo("COMMIT_DIFF_FAILED");
    }

    @Test
    void changedLinesReportTargetVersionRanges() throws Exception {
        Path repo = gitRepoWithFeatureBranch();
        RepositoryInput input = new RepositoryInput(
                "diffed", repo.toString(), "feature", "main", false, null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-cl", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        @SuppressWarnings("unchecked")
        Map<String, Object> lines = (Map<String, Object>) entry.get("changed_lines");
        // 改一行 → 两侧各一行；基线模式 base 取 merge-base
        assertThat(ranges(lines, "A.java", "added")).containsExactly(List.of(1, 1));
        assertThat(ranges(lines, "A.java", "removed")).containsExactly(List.of(1, 1));
        assertThat(ranges(lines, "C.java", "added")).containsExactly(List.of(1, 1));
        // 删除的文件以旧路径登记，新侧无行（仍保留条目，避免被下游读成"没变"）
        assertThat(ranges(lines, "B.java", "added")).isEmpty();
        assertThat(ranges(lines, "B.java", "removed")).containsExactly(List.of(1, 1));
        // 行区间与变更清单的路径口径必须**完全一致**
        @SuppressWarnings("unchecked")
        Map<String, Object> changed = (Map<String, Object>) entry.get("changed_files");
        Set<String> paths = new LinkedHashSet<>();
        for (String key : List.of("added", "modified", "removed")) {
            @SuppressWarnings("unchecked")
            List<String> bucket = (List<String>) changed.get(key);
            paths.addAll(bucket);
        }
        assertThat(lines.keySet()).isEqualTo(paths);
        assertThat(entry.get("base_ref")).isNotNull();
        assertThat((String) entry.get("target_ref")).isEqualTo("feature");
    }

    @Test
    void changedLinesResolveContiguousCommitsToOneRange() throws Exception {
        Path repo = gitRepoWithMultipleCommits();
        RepositoryInput input = new RepositoryInput(
                "multi", repo.toString(), "main", null, null, null, List.of("HEAD~1", "HEAD"));

        Map<String, Object> inventory = runner.run(new ScanRequest("job-cl2", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("ok");
        // 根提交无父 → base 退化为空树，两个提交都是整文件新增
        @SuppressWarnings("unchecked")
        Map<String, Object> lines = (Map<String, Object>) entry.get("changed_lines");
        assertThat(ranges(lines, "A.java", "added")).containsExactly(List.of(1, 1));
        assertThat(ranges(lines, "B.java", "added")).containsExactly(List.of(1, 1));
    }

    @Test
    void changedLinesRejectNonContiguousCommits() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("gapped"));
        runGit(repo, "init", "-b", "main");
        Files.writeString(repo.resolve("A.java"), "class A {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c1");
        Files.writeString(repo.resolve("B.java"), "class B {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c2-gap");
        Files.writeString(repo.resolve("C.java"), "class C {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c3");
        // 只指定 c1 与 c3：区间里夹着 c2，语义上无法用一个 base..target 表达
        RepositoryInput input = new RepositoryInput(
                "gapped", repo.toString(), "main", null, null, null, List.of("HEAD~2", "HEAD"));

        Map<String, Object> inventory = runner.run(new ScanRequest("job-cl3", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("error");
        @SuppressWarnings("unchecked")
        Map<String, Object> error = (Map<String, Object>) entry.get("error");
        assertThat(error.get("code")).isEqualTo("COMMIT_DIFF_FAILED");
        assertThat((String) error.get("message")).contains("连续区间");
        assertThat((String) error.get("message")).contains("原因");
    }

    @Test
    void largeDiffOutputDoesNotDeadlockOnPipeBuffer() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("big-diff"));
        runGit(repo, "init", "-b", "main");
        StringBuilder before = new StringBuilder();
        for (int i = 0; i < 6000; i++) {
            before.append("class Line").append(i).append(" {}\n");
        }
        Files.writeString(repo.resolve("Big.java"), before.toString());
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "base");
        runGit(repo, "checkout", "-b", "feature");
        StringBuilder after = new StringBuilder();
        for (int i = 0; i < 6000; i++) {
            after.append("class Line").append(i).append("Changed {}\n");
        }
        Files.writeString(repo.resolve("Big.java"), after.toString());
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "feature");
        RepositoryInput input = new RepositoryInput(
                "big", repo.toString(), "feature", "main", false, null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-big", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        // 回归：diff 输出约 200KB，超过管道缓冲。若先 waitFor 再读流，子进程阻塞在写、
        // 父进程等到 30s 超时 → status 变 error。必须并发排空两条管道。
        assertThat(entry.get("status")).isEqualTo("ok");
        @SuppressWarnings("unchecked")
        Map<String, Object> lines = (Map<String, Object>) entry.get("changed_lines");
        assertThat(ranges(lines, "Big.java", "added")).containsExactly(List.of(1, 6000));
    }

    // ---- 路径基数与提交号解析（评审 §5 / #50 前置） ----

    @Test
    void repoPathMustBeGitTopLevel() throws Exception {
        Path repo = gitRepoWithFeatureBranch();
        Path sub = Files.createDirectories(repo.resolve("api"));
        // 配成仓库的子目录：git diff 的路径以 toplevel 为基准，与源码列表基准不同
        RepositoryInput input = new RepositoryInput(
                "sub", sub.toString(), "feature", "main", false, null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-top", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        assertThat(entry.get("status")).isEqualTo("error");
        @SuppressWarnings("unchecked")
        Map<String, Object> error = (Map<String, Object>) entry.get("error");
        // 不拦的话会静默 0 接口（变更清单永远匹配不上候选）
        assertThat(error.get("code")).isEqualTo("REPO_PATH_NOT_TOPLEVEL");
        assertThat((String) error.get("message")).contains("不是 Git 顶层目录");
    }

    @Test
    void targetShaAndBaseShaAreResolved() throws Exception {
        Path repo = gitRepoWithFeatureBranch();
        String targetSha = runGitCapture(repo, "rev-parse", "feature^{commit}").trim();
        String baseSha = runGitCapture(repo, "merge-base", "main", "feature").trim();
        RepositoryInput input = new RepositoryInput(
                "diffed", repo.toString(), "feature", "main", false, null, null);

        Map<String, Object> inventory = runner.run(new ScanRequest("job-sha", List.of(input)));

        Map<String, Object> entry = onlyRepository(inventory);
        // extract 要按 target_sha 检出快照解析，行号才能与 changed_lines 同版本
        assertThat(entry.get("target_sha")).isEqualTo(targetSha);
        assertThat(entry.get("base_sha")).isEqualTo(baseSha);
    }

    @SuppressWarnings("unchecked")
    private static List<List<Integer>> ranges(Map<String, Object> lines, String path, String side) {
        Map<String, Object> entry = (Map<String, Object>) lines.get(path);
        return entry == null ? List.of() : (List<List<Integer>>) entry.get(side);
    }

    /** 建真实 git 仓库：main（A.java + B.java）→ feature（改 A + 删 B + 加 C）。 */
    private Path gitRepoWithFeatureBranch() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("diffed"));
        runGit(repo, "init", "-b", "main");
        Files.writeString(repo.resolve("A.java"), "class A {}\n");
        Files.writeString(repo.resolve("B.java"), "class B {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "base");
        runGit(repo, "checkout", "-b", "feature");
        Files.writeString(repo.resolve("A.java"), "class A { int x; }\n");
        Files.delete(repo.resolve("B.java"));
        Files.writeString(repo.resolve("C.java"), "class C {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "feature");
        return repo;
    }

    /** 建真实 git 仓库：main 上两个提交——HEAD~1 加 A.java，HEAD 加 B.java。 */
    private Path gitRepoWithMultipleCommits() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("multi-commit"));
        runGit(repo, "init", "-b", "main");
        Files.writeString(repo.resolve("A.java"), "class A {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c1-add-A");
        Files.writeString(repo.resolve("B.java"), "class B {}\n");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c2-add-B");
        return repo;
    }

    private static void runGit(Path repo, String... args) throws Exception {
        runGitCapture(repo, args);
    }

    /** 同 runGit，但返回 stdout（需要读提交号时用）。 */
    private static String runGitCapture(Path repo, String... args) throws Exception {
        List<String> cmd = new ArrayList<>();
        cmd.add("git");
        cmd.add("-C");
        cmd.add(repo.toString());
        // 测试环境无全局身份时 commit 会拒绝；命令级 -c 不污染全局配置
        cmd.add("-c");
        cmd.add("user.email=test@example.com");
        cmd.add("-c");
        cmd.add("user.name=scan-test");
        cmd.addAll(List.of(args));
        Process process = new ProcessBuilder(cmd).redirectErrorStream(true).start();
        String output = new String(
                process.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        if (!process.waitFor(30, TimeUnit.SECONDS) || process.exitValue() != 0) {
            throw new AssertionError("git 失败：" + String.join(" ", args) + "\n" + output);
        }
        return output;
    }

    private ScanRequest request(Path repo, boolean fallback, List<String> includes) {
        RepositoryInput input = new RepositoryInput(
                repo.getFileName().toString(), repo.toString(), "main", fallback, includes);
        return new ScanRequest("job-1", List.of(input));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> onlyRepository(Map<String, Object> inventory) {
        List<Map<String, Object>> repositories =
                (List<Map<String, Object>>) inventory.get("repositories");
        assertThat(repositories).hasSize(1);
        return repositories.get(0);
    }
}
