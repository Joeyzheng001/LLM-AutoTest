// ExtractJobRunner 单测：Spring 注解静态定位 + 告警口径（R-REV-02 第①步）。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;

import com.llmautotest.parser.ParserProperties;
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

    private final ExtractJobRunner runner = new ExtractJobRunner(ParserProperties.of(2, 20000));

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
    void keepsCompleteLongMethodAndSnippet() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("long-service"));
        String source = "@RestController class LongController {\n"
                + "@PostMapping(\"/long\") String trial(@RequestBody String body) {\n"
                + "String padding = \"" + "x".repeat(12000) + "\";\n"
                + "return \"TAIL_MARKER\" + body;\n}}";
        Files.writeString(repo.resolve("LongController.java"), source);
        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-long", List.of(input(repo, false)), null));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);
        assertThat((String) candidates.get(0).get("method_body"))
                .hasSizeGreaterThan(12000).contains("TAIL_MARKER");
        assertThat((String) candidates.get(0).get("snippet"))
                .hasSizeGreaterThan(12000).contains("TAIL_MARKER");
        assertThat(candidates.get(0).get("method_body_truncated")).isEqualTo(false);
    }

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
    void bareMappingAnnotationMapsToClassPrefix() throws IOException {
        // 回归锁定：联调发现的真 bug——裸 @GetMapping/@PostMapping（无路径成员）
        // 应映射到类级前缀，而非漏产候选
        Path repo = Files.createDirectories(tempDir.resolve("svc"));
        Files.writeString(repo.resolve("UserController.java"), """
                @RestController
                @RequestMapping("/api/v1/users")
                class UserController {
                    @GetMapping
                    String list() { return "[]"; }

                    @PostMapping
                    String create() { return "ok"; }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-bare", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).extracting(c -> c.get("signature"))
                .containsExactlyInAnyOrder("GET /api/v1/users", "POST /api/v1/users");
    }

    @Test
    void recordSyntaxParsesUnderJava21Level() throws IOException {
        // 回归锁定：联调发现——JavaParser 默认语言级别低于 16，record 解析失败告警
        Path repo = Files.createDirectories(tempDir.resolve("svc"));
        Files.writeString(repo.resolve("User.java"), """
                package com.demo.user;

                public record User(long id, String name) {
                }
                """);
        Files.writeString(repo.resolve("UserController.java"), """
                @RestController
                @RequestMapping("/api/v1/users")
                class UserController {
                    @GetMapping("/{id}")
                    User get(long id) { return null; }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-record", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<String> warnings = (List<String>) payload.get("warnings");
        assertThat(warnings).noneMatch(w -> w.contains("User.java"));
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
                "job-1", List.of(input(repo, false)), List.of("rest", "unknown")));

        List<?> warnings = (List<?>) payload.get("warnings");
        assertThat(warnings).anyMatch(w -> String.valueOf(w).contains("unknown"));
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

    @Test
    void extractsRequestBodyFieldsForStrongTypedBody() throws IOException {
        Path repo = Files.createDirectories(tempDir.resolve("mail"));
        Path mailDir = Files.createDirectories(repo.resolve("com/demo/mail"));
        Files.writeString(mailDir.resolve("MailSendRequest.java"), """
                package com.demo.mail;

                import java.util.List;

                /** 邮件发送请求。 */
                public class MailSendRequest {

                    /** 调用方请求流水号。 */
                    private String requestId;

                    /** 收件人。 */
                    private List<String> to;

                    /** 是否按 HTML 正文发送。 */
                    private Boolean html;
                }
                """);
        Files.writeString(mailDir.resolve("MailController.java"), """
                package com.demo.mail;

                import org.springframework.web.bind.annotation.*;

                @RestController
                @RequestMapping("/mail")
                public class MailController {

                    @PostMapping("/send")
                    public Object send(@RequestBody MailSendRequest request) {
                        return null;
                    }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-mail", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> body = (List<Map<String, Object>>) candidates.get(0).get("request_body");
        assertThat(body).isNotNull();
        assertThat(body).extracting(f -> f.get("name")).containsExactly("requestId", "to", "html");
        assertThat(body).extracting(f -> f.get("type")).containsExactly("String", "List<String>", "Boolean");
        assertThat((String) body.get(0).get("description")).isEqualTo("调用方请求流水号。");
    }

    @Test
    void extractsRequestBodyFieldsInMavenMultiModuleLayout() throws IOException {
        // 回归锁定：真实仓库是 Maven 多模块布局（<module>/src/main/java/<package>），
        // 源码根非仓库根，符号解析须索引 src/main/java 才能定位 @RequestBody 复杂类型。
        Path repo = Files.createDirectories(tempDir.resolve("maven-multi"));
        Path biz = Files.createDirectories(repo.resolve("icm-biz/src/main/java/com/demo/mail"));
        Files.writeString(biz.resolve("MailSendRequest.java"), """
                package com.demo.mail;

                import java.util.List;

                /** 邮件发送请求。 */
                public class MailSendRequest {

                    /**
                     * 调用方请求流水号。
                     */
                    private String requestId;

                    /**
                     * 收件人。
                     */
                    private List<String> to;
                }
                """);
        Files.writeString(biz.resolve("MailController.java"), """
                package com.demo.mail;

                import org.springframework.web.bind.annotation.*;

                @RestController
                @RequestMapping("/mail")
                public class MailController {
                    @PostMapping("/send")
                    public Object send(@RequestBody MailSendRequest request) { return null; }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-maven", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> body = (List<Map<String, Object>>) candidates.get(0).get("request_body");
        assertThat(body).isNotNull();
        assertThat(body).extracting(f -> f.get("name")).containsExactly("requestId", "to");
        assertThat(body).extracting(f -> f.get("type")).containsExactly("String", "List<String>");
        // 多行 Javadoc 的 `*` 前缀与换行应被结构化提取剥离
        assertThat((String) body.get(0).get("description")).isEqualTo("调用方请求流水号。");
    }

    @Test
    void transparentParamSkipsFieldExtraction() throws IOException {
        // 动态类型但方法体无 getter 访问（透传给 service）→ 提取不到字段，返回 null
        Path repo = Files.createDirectories(tempDir.resolve("dynamic"));
        Files.writeString(repo.resolve("DepositController.java"), """
                @RestController
                @RequestMapping("/deposit")
                class DepositController {
                    @PostMapping("/trial")
                    Object trial(@RequestBody JSONObject param) { return service.trial(param); }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-dynamic", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);
        assertThat(candidates.get(0).get("request_body")).isNull();
    }

    @Test
    void extractsDynamicFieldsFromGetterCalls() throws IOException {
        // 动态类型 + 方法体内 getter 访问点 → 提取字符串字段名 + getter 推断类型
        Path repo = Files.createDirectories(tempDir.resolve("dyn-getter"));
        Files.writeString(repo.resolve("RiskController.java"), """
                @RestController
                @RequestMapping("/risk")
                class RiskController {
                    @PostMapping("/trial")
                    Object trial(@RequestBody JSONObject param) {
                        String serialNo = param.getString("serialNo");
                        int count = param.getInteger("count");
                        String requestId = param.getString("requestId");
                        return null;
                    }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-dyn-getter", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> body = (List<Map<String, Object>>) candidates.get(0).get("request_body");
        assertThat(body).isNotNull();
        assertThat(body).extracting(f -> f.get("name")).containsExactly("serialNo", "count", "requestId");
        assertThat(body).extracting(f -> f.get("type")).containsExactly("String", "Integer", "String");
    }

    @Test
    void extractsDynamicFieldsFromConstantReference() throws IOException {
        // 动态类型 + 常量引用（param.getString(ACCOUNT_NO)）→ 解析 static final 常量值
        Path repo = Files.createDirectories(tempDir.resolve("dyn-const"));
        Files.writeString(repo.resolve("DepositController.java"), """
                @RestController
                @RequestMapping("/deposit")
                class DepositController {
                    private static final String ACCOUNT_NO = "accountNo";

                    @PostMapping("/trial")
                    Object trial(@RequestBody JSONObject param) {
                        if (param.getString(ACCOUNT_NO) == null) { throw new RuntimeException(); }
                        return null;
                    }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-dyn-const", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> body = (List<Map<String, Object>>) candidates.get(0).get("request_body");
        assertThat(body).isNotNull();
        assertThat(body).extracting(f -> f.get("name")).containsExactly("accountNo");
        assertThat(body).extracting(f -> f.get("type")).containsExactly("String");
    }

    @Test
    void extractsDynamicFieldsFromSameClassPrivateMethod() throws IOException {
        // 动态类型 + 透传给本类私有方法（validateBusinessParams）→ 追踪目标方法体内 getter
        Path repo = Files.createDirectories(tempDir.resolve("dyn-same-class"));
        Files.writeString(repo.resolve("DepositController.java"), """
                @RestController
                @RequestMapping("/deposit")
                class DepositController {
                    private static final String ACCOUNT_NO = "accountNo";

                    @PostMapping("/trial")
                    Object trial(@RequestBody JSONObject param) {
                        String serialNo = param.getString("serialNo");
                        validateBusinessParams(param);
                        return null;
                    }

                    private void validateBusinessParams(JSONObject param) {
                        if (param.getString(ACCOUNT_NO) == null) { throw new RuntimeException(); }
                    }
                }
                """);

        Map<String, Object> payload = runner.run(new ExtractRequest(
                "job-dyn-same-class", List.of(input(repo, false)), null));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> body = (List<Map<String, Object>>) candidates.get(0).get("request_body");
        assertThat(body).isNotNull();
        // 本方法直接 getter（serialNo）+ 透传本类私有方法的 getter（accountNo）都应提取
        assertThat(body).extracting(f -> f.get("name")).containsExactly("serialNo", "accountNo");
    }

    @Test
    void candidateCarriesDependencyFilesForScopeFiltering() throws IOException {
        Path repo = tempDir.resolve("dep-repo");
        writeJava(repo, "com/demo/DemandDepositRuleController.java", """
                package com.demo;

                import org.springframework.beans.factory.annotation.Autowired;
                import org.springframework.web.bind.annotation.*;

                @RestController
                @RequestMapping("/demandDepositRule")
                public class DemandDepositRuleController {

                    @Autowired
                    private DemandDepositRuleService demandDepositRuleService;

                    @PostMapping("/query")
                    public Response<List<DemandDepositRule>> query(@RequestBody DemandDepositRuleQuery request) {
                        return demandDepositRuleService.listRule(request);
                    }
                }
                """);
        writeJava(repo, "com/demo/DemandDepositRuleService.java",
                "package com.demo; public interface DemandDepositRuleService {}");
        writeJava(repo, "com/demo/DemandDepositRuleServiceImpl.java",
                "package com.demo; public class DemandDepositRuleServiceImpl"
                        + " implements DemandDepositRuleService {}");
        writeJava(repo, "com/demo/DemandDepositRuleQuery.java",
                "package com.demo; public class DemandDepositRuleQuery {}");
        writeJava(repo, "com/demo/DemandDepositRule.java",
                "package com.demo; public class DemandDepositRule {}");

        Map<String, Object> payload =
                runner.run(new ExtractRequest("job-deps", List.of(input(repo, false)), null));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> candidates = (List<Map<String, Object>>) payload.get("candidates");
        assertThat(candidates).hasSize(1);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> related =
                (List<Map<String, Object>>) candidates.get(0).get("related_files");
        // Controller 自身 + 字段注入（含接口 → 实现类一跳）+ 方法参数 + 返回类型泛型实参，
        // 都要落成文件路径：调用方据此判定「依赖改了、Controller 没改」的接口也算本次变更。
        assertThat(related)
                .extracting(item -> item.get("file"))
                .contains(
                        "com/demo/DemandDepositRuleController.java",
                        "com/demo/DemandDepositRuleService.java",
                        "com/demo/DemandDepositRuleServiceImpl.java",
                        "com/demo/DemandDepositRuleQuery.java",
                        "com/demo/DemandDepositRule.java");
        // 角色按命名约定判定：实现类是 service，查询对象是 dto
        assertThat(related)
                .filteredOn(item -> "service".equals(item.get("role")))
                .extracting(item -> item.get("file"))
                .contains("com/demo/DemandDepositRuleServiceImpl.java");
        assertThat(related)
                .filteredOn(item -> "dto".equals(item.get("role")))
                .extracting(item -> item.get("file"))
                .contains("com/demo/DemandDepositRuleQuery.java");
        // 仓库外的类型（Response/List）不编造路径
        assertThat(related)
                .noneMatch(item -> String.valueOf(item.get("file")).contains("Response.java"));
    }

    private static void writeJava(Path repo, String relative, String source) throws IOException {
        Path file = repo.resolve(relative);
        Files.createDirectories(file.getParent());
        Files.writeString(file, source);
    }

    private RepositoryInput input(Path repo, boolean fallback) {
        return new RepositoryInput(
                repo.getFileName().toString(), repo.toString(), "main", fallback, null);
    }
}
