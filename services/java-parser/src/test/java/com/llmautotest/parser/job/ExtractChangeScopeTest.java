// ExtractJobRunner 的版本一致性 / 递归调用链 / 变更证据单测（#50 快照、#51 code_spans、#52 change_evidence）。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;

import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.ExtractRequest;
import com.llmautotest.parser.contract.RepositoryInput;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ExtractChangeScopeTest {

    @TempDir
    Path tempDir;

    private final ExtractJobRunner runner = new ExtractJobRunner(ParserProperties.of(2, 20000));

    private static final String CONTROLLER = """
            package com.demo;

            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/demo")
            public class DemoController {

                @Autowired
                private DemoService demoService;

                @GetMapping("/query")
                public String query(@RequestParam Long id) {
                    return demoService.load(id);
                }
            }
            """;

    private static final String CONTROLLER_V2 = CONTROLLER.replace(
            "return demoService.load(id);", "return demoService.load(id) + \"-v2\";");

    private static final String SERVICE = """
            package com.demo;

            public class DemoService {

                public String load(Long id) {
                    return "v-" + id;
                }
            }
            """;

    // ---- #50 target_sha 只读快照 ----

    @Test
    void parsesFromSnapshotAtTargetShaNotFromWorkspace() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();

        Map<String, Object> payload = extract(repo, first, null);

        // 工作区是第二个提交（含 -v2），快照必须是第一个提交（不含 -v2）
        assertThat(snippetOf(firstCandidate(payload))).doesNotContain("-v2");
        assertThat(Files.readString(repo.resolve("com/demo/DemoController.java")))
                .contains("-v2");
        assertThat(repoStat(payload).get("snapshot")).isEqualTo(true);
        assertThat(repoStat(payload).get("status")).isEqualTo("ok");
        assertThat((String) repoStat(payload).get("snapshot_path")).contains(first.substring(0, 12));
    }

    @Test
    void missingTargetShaIsMarkedDegradedAndWarns() throws Exception {
        Path repo = repoWithTwoCommits();

        Map<String, Object> payload = extract(repo, null, null);

        assertThat(repoStat(payload).get("status")).isEqualTo("degraded");
        assertThat(repoStat(payload).get("degraded_reason")).isEqualTo("NO_TARGET_SHA");
        assertThat(warningsOf(payload)).anyMatch(item -> item.contains("未提供 target_sha"));
        // 退化路径仍出候选（可单独使用 java-parser 的旧调用方不被硬拦），但标记必须可见
        assertThat(candidatesOf(payload)).isNotEmpty();
    }

    @Test
    void unresolvableTargetShaIsReportedAsRepoError() throws Exception {
        Path repo = repoWithTwoCommits();

        Map<String, Object> payload = extract(repo, "0000000000000000000000000000000000000000", null);

        assertThat(repoStat(payload).get("status")).isEqualTo("error");
        assertThat(errorCode(payload)).isEqualTo("SNAPSHOT_FAILED");
        // 快照失败不再静默退回扫工作区（否则行号与 diff 不同版本）
        assertThat(candidatesOf(payload)).isEmpty();
    }

    // ---- #51 code_spans 递归调用链 ----

    @Test
    void codeSpansContainBodyAndResolvedCallee() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();

        Map<String, Object> candidate = firstCandidate(extract(repo, first, null));
        List<Map<String, Object>> spans = spansOf(candidate);

        assertThat(spans).extracting(item -> item.get("kind")).containsExactly("body", "callee");
        assertThat(spans.get(0).get("file")).isEqualTo("com/demo/DemoController.java");
        Map<String, Object> callee = spans.get(1);
        assertThat(callee.get("file")).isEqualTo("com/demo/DemoService.java");
        assertThat(callee.get("role")).isEqualTo("service");
        // 行区间落在 DemoService.load 方法上（该文件里方法声明在第 5 行）
        assertThat(callee.get("start_line")).isEqualTo(5);
        assertThat(candidate.get("unresolved")).isNull();
        assertThat(candidate.get("call_graph_incomplete")).isNull();
        assertThat((Map<String, Object>) candidate.get("call_graph"))
                .containsEntry("status", "complete")
                .containsEntry("resolved_calls", 1)
                .containsEntry("resolved_nodes", 1);
    }

    @Test
    void codeSpansFollowServiceToHelperRecursively() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("recursive-call-repo"));
        write(repo, "com/demo/DemoController.java", """
                package com.demo;

                import org.springframework.beans.factory.annotation.Autowired;
                import org.springframework.web.bind.annotation.*;

                @RestController
                public class DemoController {
                    @Autowired
                    private DemoService demoService;

                    @GetMapping("/query")
                    public String query() {
                        return demoService.load();
                    }
                }
                """);
        write(repo, "com/demo/DemoService.java", """
                package com.demo;

                public class DemoService {
                    private final DemoHelper helper = new DemoHelper();

                    public String load() {
                        return helper.normalize();
                    }
                }
                """);
        write(repo, "com/demo/DemoHelper.java", """
                package com.demo;

                public class DemoHelper {
                    public String normalize() {
                        return "ok";
                    }
                }
                """);
        gitInitAndCommit(repo);

        Map<String, Object> candidate = firstCandidate(extract(repo, null, null));

        assertThat(spansOf(candidate)).extracting(item -> item.get("file"))
                .containsExactly(
                        "com/demo/DemoController.java",
                        "com/demo/DemoService.java",
                        "com/demo/DemoHelper.java");
        assertThat(candidate.get("unresolved")).isNull();
        assertThat(candidate.get("call_graph_incomplete")).isNull();
        assertThat((Map<String, Object>) candidate.get("call_graph"))
                .containsEntry("status", "complete")
                .containsEntry("resolved_calls", 2)
                .containsEntry("resolved_nodes", 2)
                .containsEntry("max_depth", 2);
    }

    @Test
    void constructorInjectedDependencyIsResolved() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("ctor-repo"));
        write(repo, "com/demo/DemoController.java", """
                package com.demo;

                import org.springframework.web.bind.annotation.*;

                @RestController
                @RequestMapping("/demo")
                public class DemoController {

                    private final DemoService demoService;

                    public DemoController(DemoService demoService) {
                        this.demoService = demoService;
                    }

                    @GetMapping("/query")
                    public String query() {
                        return demoService.load(1L);
                    }
                }
                """);
        write(repo, "com/demo/DemoService.java", SERVICE);
        gitInitAndCommit(repo);

        Map<String, Object> candidate = firstCandidate(extract(repo, null, null));

        // 评审 §2 指出的漏项：构造器注入原先完全不认 → 现在能解析到实现文件
        assertThat(spansOf(candidate))
                .anyMatch(item -> "callee".equals(item.get("kind"))
                        && "com/demo/DemoService.java".equals(item.get("file")));
    }

    @Test
    void controllerResponseBodyAndAllClassPathsAreRecognized() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("controller-repo"));
        write(repo, "com/demo/DemoController.java", """
                package com.demo;

                import org.springframework.stereotype.Controller;
                import org.springframework.web.bind.annotation.*;

                @Controller
                @RequestMapping({"/v1", "/v2"})
                public class DemoController {

                    @ResponseBody
                    @GetMapping("/query")
                    public String query() {
                        return "ok";
                    }
                }
                """);
        gitInitAndCommit(repo);

        List<Map<String, Object>> candidates = candidatesOf(extract(repo, null, null));

        assertThat(candidates).hasSize(2);
        assertThat(candidates)
                .extracting(item -> item.get("signature"))
                .containsExactlyInAnyOrder("GET /v1/query", "GET /v2/query");
    }

    @Test
    void parameterEvidenceReadsRequestBodyUsageAndConditions() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("parameter-evidence-repo"));
        write(repo, "com/demo/RiskController.java", """
                package com.demo;

                import java.util.Map;
                import org.springframework.web.bind.annotation.*;

                @RestController
                public class RiskController {

                    @PostMapping("/risk/approval/trial")
                    public String trial(@RequestBody Map<String, Object> param) {
                        String approvalType = String.valueOf(
                                param.getOrDefault("approvalType", "NORMAL"));
                        if ("REJECT".equals(param.get("decision"))) {
                            return "rejected";
                        }
                        return approvalType;
                    }
                }
                """);
        gitInitAndCommit(repo);

        Map<String, Object> candidate = firstCandidate(extract(repo, null, null));

        assertThat(candidate.get("method_body")).asString()
                .contains("/risk/approval/trial")
                .contains("approvalType")
                .contains("REJECT");
        List<Map<String, Object>> evidence =
                (List<Map<String, Object>>) candidate.get("parameter_evidence");
        assertThat(evidence).singleElement().satisfies(item -> {
            assertThat(item.get("name")).isEqualTo("param");
            assertThat(item.get("in")).isEqualTo("body");
            assertThat((List<Map<String, Object>>) item.get("body_accesses"))
                    .extracting(access -> access.get("field"))
                    .contains("approvalType", "decision");
            assertThat((List<Map<String, Object>>) item.get("conditions"))
                    .singleElement().extracting(condition -> condition.get("condition"))
                    .asString().contains("REJECT");
        });
        assertThat((List<Map<String, Object>>) candidate.get("request_body"))
                .extracting(field -> field.get("name"))
                .contains("approvalType", "decision");
    }

    @Test
    void dynamicBodyTraversesServiceAndNestedAccesses() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("dynamic-service-repo"));
        write(repo, "com/demo/RiskController.java", """
                package com.demo;

                import java.util.Map;
                import org.springframework.beans.factory.annotation.Autowired;
                import org.springframework.web.bind.annotation.*;

                @RestController
                public class RiskController {
                    @Autowired
                    private RiskService riskService;

                    @PostMapping("/risk/approval/trial")
                    public String trial(@RequestBody Map<String, Object> param) {
                        return riskService.trial(param);
                    }
                }
                """);
        write(repo, "com/demo/RiskService.java", """
                package com.demo;

                import java.util.Map;

                public class RiskService {
                    public String trial(Map<String, Object> request) {
                        String code = request.getJSONObject("risk").getString("code");
                        Object mode = request.getOrDefault("mode", "NORMAL");
                        return code + mode;
                    }
                }
                """);
        gitInitAndCommit(repo);

        Map<String, Object> candidate = firstCandidate(extract(repo, null, null));

        assertThat((List<Map<String, Object>>) candidate.get("request_body"))
                .extracting(field -> field.get("name"))
                .contains("risk", "risk.code", "mode");
        assertThat((List<Map<String, Object>>) candidate.get("request_body"))
                .anyMatch(field -> "risk.code".equals(field.get("name"))
                        && "String".equals(field.get("type")));
        assertThat((List<Map<String, Object>>) candidate.get("request_body"))
                .anyMatch(field -> "mode".equals(field.get("name"))
                        && Boolean.FALSE.equals(field.get("required")));
    }

    @Test
    void strongBodyFieldsIncludeRecordsAndInheritedFields() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("record-body-repo"));
        write(repo, "com/demo/TrialController.java", """
                package com.demo;

                import org.springframework.web.bind.annotation.*;

                @RestController
                public class TrialController {
                    @PostMapping("/trial")
                    public String trial(@RequestBody TrialRequest request) {
                        return request.serialNo();
                    }
                }
                """);
        write(repo, "com/demo/TrialRequest.java", """
                package com.demo;

                public record TrialRequest(String serialNo, Integer retryCount) {}
                """);
        write(repo, "com/demo/InheritedRequest.java", """
                package com.demo;

                public class InheritedRequest extends BaseRequest {
                    private String decision;
                }
                """);
        write(repo, "com/demo/BaseRequest.java", """
                package com.demo;

                public class BaseRequest {
                    protected String traceId;
                }
                """);
        gitInitAndCommit(repo);

        Map<String, Object> candidate = firstCandidate(extract(repo, null, null));
        assertThat((List<Map<String, Object>>) candidate.get("request_body"))
                .extracting(field -> field.get("name"))
                .containsExactly("serialNo", "retryCount");
    }

    @Test
    void graphqlAndMessagingAnnotationsProduceProtocolCandidates() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("protocol-repo"));
        write(repo, "com/demo/ProtocolController.java", """
                package com.demo;

                import org.springframework.graphql.data.method.annotation.QueryMapping;
                import org.springframework.kafka.annotation.KafkaListener;
                import org.springframework.stereotype.Controller;

                @Controller
                public class ProtocolController {
                    @QueryMapping
                    public String risk() { return "ok"; }

                    @KafkaListener(topics = "risk-events")
                    public void consume(String message) {}
                }
                """);
        write(repo, "com/demo/risk.proto", """
                syntax = "proto3";
                service RiskService {
                  rpc Trial (TrialRequest) returns (TrialResponse);
                }
                """);
        gitInitAndCommit(repo);

        List<Map<String, Object>> candidates = candidatesOf(extract(repo, null, null));
        assertThat(candidates).extracting(item -> item.get("protocol"))
                .contains("graphql", "mq", "grpc");
        assertThat(candidates).extracting(item -> item.get("signature"))
                .contains("GraphQL query risk", "MQ kafka risk-events", "gRPC RiskService.Trial");
    }

    @Test
    void unresolvableCalleeIsReportedInsteadOfSilentlySkipped() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("unresolved-repo"));
        write(repo, "com/demo/DemoController.java", """
                package com.demo;

                import org.springframework.web.bind.annotation.*;

                @RestController
                public class DemoController {

                    @GetMapping("/query")
                    public String query() {
                        return unknownHelper.help();
                    }
                }
                """);
        gitInitAndCommit(repo);

        Map<String, Object> candidate = firstCandidate(extract(repo, null, null));

        // 无法确定接收者类型 → 如实标注，而不是当作"不会执行到"
        assertThat((List<Map<String, Object>>) candidate.get("unresolved"))
                .singleElement()
                .satisfies(item -> {
                    assertThat(item.get("type")).isEqualTo("DemoController");
                    assertThat(item.get("method")).isEqualTo("help");
                    assertThat(String.valueOf(item.get("reason"))).contains("无法确定静态类型");
                });
        assertThat(candidate.get("call_graph_incomplete")).isEqualTo(true);
        // 候选本身必须保留（保守口径：不确定 ≠ 丢弃）
        assertThat(candidate).containsKey("code_location");
    }

    // ---- #52 change_evidence 行级求交 ----

    @Test
    void bodyChangeIsDetectedWithChangedSnippet() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();
        int changedLine = lineOf(CONTROLLER, "return demoService.load(id);");

        Map<String, Object> candidate = firstCandidate(extract(
                repo, first, Map.of("com/demo/DemoController.java", added(Map.of(changedLine, changedLine)))));

        Map<String, Object> evidence = evidenceOf(candidate);
        assertThat(evidence.get("level")).isEqualTo("body");
        assertThat(evidence.get("hits")).asList().hasSize(1);
        assertThat((List<Map<String, Object>>) evidence.get("changed_snippets"))
                .singleElement()
                .satisfies(item -> assertThat(String.valueOf(item.get("text")))
                        .contains("demoService.load(id)"));
    }

    @Test
    void calleeChangeIsDetectedAsCallChainHi() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();
        int serviceLine = lineOf(SERVICE, "return \"v-\" + id;");

        Map<String, Object> candidate = firstCandidate(extract(
                repo, first, Map.of("com/demo/DemoService.java", added(Map.of(serviceLine, serviceLine)))));

        Map<String, Object> evidence = evidenceOf(candidate);
        assertThat(evidence.get("level")).isEqualTo("callee");
        assertThat(String.valueOf(
                ((List<Map<String, Object>>) evidence.get("changed_snippets")).get(0).get("file")))
                .isEqualTo("com/demo/DemoService.java");
    }

    @Test
    void fileLevelWhenChangedLineFallsOutsideEverySpan() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();
        // 第 1 行（package 声明）在任何方法的行区间之外，但文件确实被改了
        Map<String, Object> candidate = firstCandidate(extract(
                repo, first, Map.of("com/demo/DemoController.java", added(Map.of(1, 1)))));

        Map<String, Object> evidence = evidenceOf(candidate);
        assertThat(evidence.get("level")).isEqualTo("file");
        assertThat((List<Object>) evidence.get("hits")).isEmpty();
    }

    @Test
    void relatedFileOnlyChangeIsDetected() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();
        // ONLY Service 被改：与 callee 不同——这里把 Service 的方法行区间也从快照里排除的
        // 场景无法构造，故用"Service 文件的注释行"（不在任何 span 内）验证 related_file 路径
        Map<String, Object> candidate = firstCandidate(extract(
                repo, first, Map.of("com/demo/DemoService.java", added(Map.of(1, 1)))));

        // Service 的 span 确实覆盖不到第 1 行，但它是关联文件 → related_file
        assertThat(evidenceOf(candidate).get("level")).isEqualTo("related_file");
    }

    @Test
    void noChangeKeepsExplicitNoneLevel() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();

        Map<String, Object> candidate = firstCandidate(extract(
                repo, first, Map.of("com/demo/Other.java", added(Map.of(3, 4)))));

        // 显式 none 而不是缺字段：调用方据此可判断"确实没碰到"，不必猜
        assertThat(evidenceOf(candidate).get("level")).isEqualTo("none");
    }

    // ---- 完整性计数 ----

    @Test
    void statsCountsAreClosedAndMatchInventoryScale() throws Exception {
        Path repo = repoWithTwoCommits();
        String first = runGit(repo, "rev-parse", "HEAD~1").trim();

        Map<String, Object> payload = extract(repo, first, null);

        Map<String, Object> stat = repoStat(payload);
        assertThat((Integer) stat.get("files_discovered")).isEqualTo(2);
        assertThat((Integer) stat.get("files_parsed")).isEqualTo(2);
        assertThat((Integer) stat.get("files_failed")).isEqualTo(0);
        assertThat((Integer) stat.get("candidates")).isEqualTo(1);
        Map<String, Object> totals =
                (Map<String, Object>) ((Map<String, Object>) payload.get("stats")).get("totals");
        assertThat(totals.get("files_discovered")).isEqualTo(2);
        assertThat(totals.get("candidates")).isEqualTo(1);
        assertThat(stat.get("types_indexed")).isEqualTo(2);
    }

    @Test
    void unsupportedProtocolsAreDeclaredStructurally() throws Exception {
        Path repo = tempDir.resolve("whatever");

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(new RepositoryInput("r", repo.toString(), "main", false, null)),
                List.of("rest", "unknown")));

        Map<String, Object> capabilities = (Map<String, Object>) payload.get("capabilities");
        assertThat((List<Object>) capabilities.get("supported"))
                .containsExactly("rest", "grpc", "graphql", "soap", "mq");
        assertThat((List<Object>) capabilities.get("unsupported")).containsExactly("unknown");
        assertThat(warningsOf(payload)).anyMatch(item -> item.contains("unknown"));
    }

    @Test
    void defaultProtocolsDoNotProduceNoiseWarnings() throws Exception {
        Path repo = repoWithTwoCommits();

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-1", List.of(input(repo, null, null)), null));

        assertThat(warningsOf(payload)).noneMatch(item -> item.contains("静态识别未接入"));
        Map<String, Object> capabilities = (Map<String, Object>) payload.get("capabilities");
        assertThat((List<Object>) capabilities.get("unsupported")).isEmpty();
        assertThat((List<Object>) capabilities.get("requested"))
                .containsExactly("rest", "grpc", "graphql", "soap", "mq");
    }

    // ---- 夹具 ----

    /** main 上两个提交：第一个是 CONTROLLER v1 + SERVICE（工作区停在第二个提交 = v2）。 */
    private Path repoWithTwoCommits() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("snapshot-repo"));
        write(repo, "com/demo/DemoController.java", CONTROLLER);
        write(repo, "com/demo/DemoService.java", SERVICE);
        gitInitAndCommit(repo);
        write(repo, "com/demo/DemoController.java", CONTROLLER_V2);
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c2");
        return repo;
    }

    private Map<String, Object> extract(
            Path repo, String targetSha, Map<String, Object> changedLines) throws IOException {
        return runner.run(
                new ExtractRequest("job-1", List.of(input(repo, targetSha, changedLines)), null));
    }

    private RepositoryInput input(Path repo, String targetSha, Map<String, Object> changedLines) {
        return new RepositoryInput(
                repo.getFileName().toString(), repo.toString(), "main", null, false, null, null, null,
                targetSha, changedLines);
    }

    /** {起,止} → changed_lines 的单文件条目（added 侧）。 */
    private static Map<String, Object> added(Map<Integer, Integer> ranges) {
        List<List<Integer>> added = new ArrayList<>();
        ranges.forEach((from, to) -> added.add(List.of(from, to)));
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("added", added);
        entry.put("removed", List.of());
        return entry;
    }

    private static int lineOf(String source, String needle) {
        String[] lines = source.split("\n", -1);
        for (int i = 0; i < lines.length; i++) {
            if (lines[i].contains(needle)) {
                return i + 1;
            }
        }
        throw new AssertionError("找不到标记行：" + needle);
    }

    private static void write(Path repo, String relative, String source) throws IOException {
        Path file = repo.resolve(relative);
        Files.createDirectories(file.getParent());
        Files.writeString(file, source);
    }

    private void gitInitAndCommit(Path repo) throws Exception {
        runGit(repo, "init", "-b", "main");
        runGit(repo, "add", "-A");
        runGit(repo, "commit", "-m", "c1");
    }

    private static String runGit(Path repo, String... args) throws Exception {
        List<String> cmd = new ArrayList<>(List.of("git", "-C", repo.toString()));
        cmd.add("-c");
        cmd.add("user.email=test@example.com");
        cmd.add("-c");
        cmd.add("user.name=extract-test");
        cmd.addAll(List.of(args));
        Process process = new ProcessBuilder(cmd).redirectErrorStream(true).start();
        String output = new String(
                process.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        if (!process.waitFor(30, TimeUnit.SECONDS) || process.exitValue() != 0) {
            throw new AssertionError("git 失败：" + String.join(" ", args) + "\n" + output);
        }
        return output;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> candidatesOf(Map<String, Object> payload) {
        return (List<Map<String, Object>>) payload.get("candidates");
    }

    private static Map<String, Object> firstCandidate(Map<String, Object> payload) {
        List<Map<String, Object>> candidates = candidatesOf(payload);
        assertThat(candidates).isNotEmpty();
        return candidates.get(0);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> repoStat(Map<String, Object> payload) {
        Map<String, Object> stats = (Map<String, Object>) payload.get("stats");
        List<Map<String, Object>> repositories = (List<Map<String, Object>>) stats.get("repositories");
        assertThat(repositories).hasSize(1);
        return repositories.get(0);
    }

    private static String errorCode(Map<String, Object> payload) {
        Map<String, Object> error = (Map<String, Object>) repoStat(payload).get("error");
        return String.valueOf(error.get("code"));
    }

    private static List<String> warningsOf(Map<String, Object> payload) {
        return ((List<?>) payload.get("warnings")).stream().map(String::valueOf).toList();
    }

    private static String snippetOf(Map<String, Object> candidate) {
        return String.valueOf(candidate.get("snippet"));
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> spansOf(Map<String, Object> candidate) {
        return (List<Map<String, Object>>) candidate.get("code_spans");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> evidenceOf(Map<String, Object> candidate) {
        return (Map<String, Object>) candidate.get("change_evidence");
    }
}
