// ScanJobRunner 单测：四态口径 + 编码跳过 + 截断 + 模块归属（F-CODE-01~03）。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.RepositoryInput;
import com.llmautotest.parser.contract.ScanRequest;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
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
