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
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ScanJobRunnerTest {

    @TempDir
    Path tempDir;

    private final ScanJobRunner runner = new ScanJobRunner(new ParserProperties(2, 20000));

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
        ScanJobRunner capped = new ScanJobRunner(new ParserProperties(2, 1));
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
