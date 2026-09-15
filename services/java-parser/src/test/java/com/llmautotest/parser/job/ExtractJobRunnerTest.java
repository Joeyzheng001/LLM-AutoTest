// ExtractJobRunner 单测：Spring 注解静态定位 + 告警口径（R-REV-02 第①步）。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;

import com.llmautotest.parser.contract.ExtractRequest;
import com.llmautotest.parser.contract.RepositoryInput;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ExtractJobRunnerTest {

    @TempDir
    Path tempDir;

    private final ExtractJobRunner runner = new ExtractJobRunner();

    private static final String CONTROLLER_SOURCE = """
            package com.demo.user;

            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/v1")
            public class UserController {

                @GetMapping("/users")
                public String getUser(@RequestParam Long id) {
                    return "user-" + id;
                }

                @PostMapping(value = "/users")
                public String createUser(@RequestBody String body) {
                    return body;
                }

                @RequestMapping(value = "/users/search", method = RequestMethod.POST)
                public String search() {
                    return "ok";
                }

                public String notAnEndpoint() {
                    return "ignored";
                }
            }
            """;

    @Test
    void extractsRestCandidatesWithSignatureAndLocation() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("user-service"));
        Files.createDirectories(repo.resolve("src/main/java/com/demo"));
        Files.writeString(repo.resolve("src/main/java/com/demo/UserController.java"), CONTROLLER_SOURCE);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).extracting(c -> c.get("signature")).containsExactlyInAnyOrder(
                "GET /api/v1/users",
                "POST /api/v1/users",
                "POST /api/v1/users/search");
        for (Map<String, Object> candidate : candidates) {
            assertThat(candidate.get("protocol")).isEqualTo("rest");
            assertThat(candidate.get("confidence")).isEqualTo(0.9);
            assertThat(candidate.get("is_fallback")).isEqualTo(false);
            assertThat((String) candidate.get("snippet")).contains("public String");
            @SuppressWarnings("unchecked")
            Map<String, Object> location = (Map<String, Object>) candidate.get("code_location");
            assertThat(location.get("repo")).isEqualTo("user-service");
            assertThat((int) location.get("start_line")).isPositive();
            assertThat((int) location.get("end_line"))
                    .isGreaterThanOrEqualTo((int) location.get("start_line"));
        }
        // 非端点方法不产候选
        assertThat(candidates).hasSize(3);
        // 候选编号程序分配（CAND-###）
        assertThat(candidates).extracting(c -> c.get("candidate_id"))
                .containsExactly("CAND-001", "CAND-002", "CAND-003");
    }

    @Test
    void parseFailureRecordedAsXRev01Warning() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("broken"));
        Files.writeString(repo.resolve("Broken.java"), "class Broken {{{");
        Files.writeString(repo.resolve("Plain.java"), "class Plain {}\n");

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(input(repo, false)), null));

        List<?> warnings = (List<?>) payload.get("warnings");
        assertThat(warnings).anyMatch(w -> String.valueOf(w).startsWith("X-REV-01：Broken.java"));
        assertThat((List<?>) payload.get("candidates")).isEmpty();
    }

    @Test
    void belowMinCandidatesWarnsXRev04() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("tiny"));
        Files.writeString(repo.resolve("One.java"), """
                @RestController
                class One {
                    @GetMapping("/one")
                    String one() { return ""; }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(input(repo, false)), null));

        assertThat((List<?>) payload.get("candidates")).hasSize(1);
        List<?> warnings = (List<?>) payload.get("warnings");
        assertThat(warnings).anyMatch(w -> String.valueOf(w).startsWith("X-REV-04"));
    }

    @Test
    void requestedUnsupportedProtocolWarnsHonestly() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("mixed"));
        Files.writeString(repo.resolve("Plain.java"), "class Plain {}\n");

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(input(repo, false)), List.of("rest", "grpc", "mq")));

        List<?> warnings = (List<?>) payload.get("warnings");
        assertThat(warnings).anyMatch(w -> String.valueOf(w).contains("grpc"));
        assertThat(warnings).anyMatch(w -> String.valueOf(w).contains("mq"));
    }

    @Test
    void fallbackMarkerPassesThrough() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("demo-fallback"));
        Files.writeString(repo.resolve("Demo.java"), """
                @RestController
                class Demo {
                    @GetMapping("/demo")
                    String demo() { return ""; }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(input(repo, true)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);
        assertThat(candidates.get(0).get("is_fallback")).isEqualTo(true);
    }

    @Test
    void missingRepoPathWarnsAndSkips() throws IOException {
        RepositoryInput ghost = new RepositoryInput(
                "ghost", tempDir.resolve("nope").toString(), "main", null, null);

        Map<String, Object> payload = runner.run(new ExtractRequest("job-1", List.of(ghost), null));

        List<?> warnings = (List<?>) payload.get("warnings");
        assertThat(warnings).anyMatch(w -> String.valueOf(w).contains("ghost"));
        assertThat((List<?>) payload.get("candidates")).isEmpty();
    }

    private RepositoryInput input(Path repo, boolean fallback) {
        return new RepositoryInput(
                repo.getFileName().toString(), repo.toString(), "main", fallback, null);
    }
}
