// 对应契约 ExtractJob：静态特征定位接口候选（R-REV-02 第①步，供 orchestrator LLM 语义解析）。
// 当前实现 Spring 注解 REST/GraphQL/SOAP/MQ 与 .proto gRPC 识别；无法识别的协议
// 通过 capabilities/warnings 如实告警（不虚构候选）。
//
// 版本一致性（评审 §1）：候选**必须**在 target_sha 的只读快照上解析。此前直接扫工作区
// （Files.walk(repo.path())），而 diff 在 .git 对象库上算 —— 工作区切错分支 / 有未提交
// 改动时，候选行号与 changed_lines 行号不属于同一版本，行级判定与"变动代码"全都错位
// 且**静默**。现在改为 git worktree add --detach 检出 target_sha 后解析；拿不到快照则
// 如实标记（不再静默降级扫工作区）。
package com.llmautotest.parser.job;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseProblemException;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.RecordDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.ArrayInitializerExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.MarkerAnnotationExpr;
import com.github.javaparser.ast.expr.MemberValuePair;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.github.javaparser.ast.expr.ThisExpr;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.type.Type;
import com.github.javaparser.ast.stmt.IfStmt;
import com.github.javaparser.resolution.declarations.ResolvedReferenceTypeDeclaration;
import com.github.javaparser.resolution.types.ResolvedType;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;
import com.llmautotest.parser.ParserProperties;
import com.llmautotest.parser.contract.ExtractRequest;
import com.llmautotest.parser.contract.RepositoryInput;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Stream;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/** 候选提取 job 执行器：JavaParser 语法树 + Spring 注解静态定位。 */
@Component
public class ExtractJobRunner {

    private static final Logger LOG = LoggerFactory.getLogger(ExtractJobRunner.class);

    /** 快照目录名前缀（建在仓库的**兄弟目录**，不污染仓库工作区与 git status）。 */
    private static final String SNAPSHOT_PREFIX = ".llm-autotest-snapshot-";

    /** 快照创建的进程内互斥：同一 (仓库, 提交) 并发建 worktree 会互相打架。 */
    private static final Object SNAPSHOT_LOCK = new Object();

    /** 长方法另附参数访问位置索引；方法原文始终完整保留。 */
    private static final int METHOD_CONTEXT_THRESHOLD = 8000;

    /** 动态 body 字段最多跨越的调用层数；超过后保留 unresolved/forwarded 证据，不无限展开。 */
    private static final int MAX_FIELD_TRACE_DEPTH = 3;

    /** 调用链最多递归层数；到达上限时显式标记 partial，不把未扫描部分伪装成已确认。 */
    private static final int MAX_CALL_GRAPH_DEPTH = 64;

    /** 每候选未解析调用点最多记录条数（只做如实标注，不做无限膨胀）。 */
    private static final int MAX_UNRESOLVED_PER_CANDIDATE = 20;

    /** 本协议能力声明：常见 Java 注解协议 + .proto 静态识别。 */
    private static final List<String> SUPPORTED_PROTOCOLS =
            List.of("rest", "grpc", "graphql", "soap", "mq");

    private static final List<String> ALL_PROTOCOLS =
            List.of("rest", "grpc", "graphql", "soap", "mq");

    /** git 调用统一入口（超时可配；worktree 快照按 target_sha 检出）。 */
    private final GitRunner git;

    /** Spring 注入配置（当前只用 git 超时）。 */
    public ExtractJobRunner(ParserProperties properties) {
        this.git = new GitRunner(properties.gitTimeoutSeconds());
    }

    /** 语言级别对齐项目 Java 21，record 等语法可解析。 */
    private static final ParserConfiguration.LanguageLevel LANGUAGE_LEVEL =
            ParserConfiguration.LanguageLevel.JAVA_21;

    /** 为指定仓库构建带符号解析的解析器（解析 @RequestBody 复杂类型的字段）。
     * 每仓库独立实例：JavaParserTypeSolver 以仓库源码根为索引，可跨模块解析类。 */
    private static JavaParser parserFor(Path root) {
        CombinedTypeSolver typeSolver = new CombinedTypeSolver();
        typeSolver.add(new ReflectionTypeSolver(false));
        // 扁平布局：源码包目录直接在仓库根下（如测试样例）
        typeSolver.add(new JavaParserTypeSolver(root));
        // Maven/Gradle 多模块：源码在 <module>/src/<sourceSet>/java 下，逐个索引
        indexSourceRoots(typeSolver, root);
        ParserConfiguration config = new ParserConfiguration()
                .setLanguageLevel(LANGUAGE_LEVEL)
                .setSymbolResolver(new JavaSymbolSolver(typeSolver));
        return new JavaParser(config);
    }

    /** 把仓库内所有 Maven/Gradle 风格源码根（src/main/java、src/test/java 等）
     * 加入类型解析器，使符号解析能跨模块定位 @RequestBody 复杂类型。 */
    private static void indexSourceRoots(CombinedTypeSolver typeSolver, Path root) {
        try (Stream<Path> stream = Files.walk(root)) {
            stream.filter(Files::isDirectory)
                    .filter(ExtractJobRunner::isSourceRoot)
                    .forEach(dir -> typeSolver.add(new JavaParserTypeSolver(dir)));
        } catch (IOException ex) {
            // 索引失败不阻断候选提取：字段提取是增强项，降级为无字段明细
            LOG.debug("源码根索引失败（字段提取降级）：{}", ex.getMessage());
        }
    }

    /** 源码根判定：<module>/src/<sourceSet>/java 约定根（Maven/Gradle 多模块）。 */
    private static boolean isSourceRoot(Path dir) {
        if (!dir.getFileName().toString().equals("java")) {
            return false;
        }
        Path sourceSet = dir.getParent();
        Path src = sourceSet == null ? null : sourceSet.getParent();
        return src != null && src.getFileName().toString().equals("src");
    }

    /** 静态定位置信度：注解命中为确定性强特征（X-REQ-04 置信度标记）。 */
    private static final double STATIC_CONFIDENCE = 0.9;

    /** R-REV-02 候选数底线（低于则告警提示换兜底仓库，X-REV-04）。 */
    private static final int MIN_CANDIDATES = 5;

    private static final Map<String, String> MAPPING_ANNOTATIONS = Map.of(
            "GetMapping", "GET",
            "PostMapping", "POST",
            "PutMapping", "PUT",
            "DeleteMapping", "DELETE",
            "PatchMapping", "PATCH");

    private static final Map<String, String> GRAPHQL_ANNOTATIONS = Map.of(
            "QueryMapping", "query",
            "MutationMapping", "mutation",
            "SubscriptionMapping", "subscription",
            "SchemaMapping", "field");

    private static final Map<String, String> SOAP_ANNOTATIONS = Map.of(
            "WebMethod", "soap",
            "PayloadRoot", "soap");

    private static final Map<String, String> MQ_ANNOTATIONS = Map.of(
            "KafkaListener", "kafka",
            "RabbitListener", "rabbit",
            "JmsListener", "jms",
            "SqsListener", "sqs",
            "RocketMQMessageListener", "rocketmq");

    private static final Pattern PROTO_SERVICE =
            Pattern.compile("\\bservice\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*\\{");
    private static final Pattern PROTO_RPC = Pattern.compile(
            "\\brpc\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*\\(\\s*([A-Za-z_][A-Za-z0-9_.]*)\\s*\\)"
                    + "\\s*returns\\s*\\(\\s*([A-Za-z_][A-Za-z0-9_.]*)\\s*\\)");

    /** 动态类型（JSONObject/Map）getter 方法名 → 推断字段类型。
     * 供「动态类型字段访问点提取」在无法拿到类定义时，从 param.getXxx("字段") 反推字段与类型。 */
    private static final Map<String, String> JSON_GETTER_TYPE = Map.ofEntries(
            Map.entry("getString", "String"),
            Map.entry("getInteger", "Integer"),
            Map.entry("getInt", "Integer"),
            Map.entry("getLong", "Long"),
            Map.entry("getBoolean", "Boolean"),
            Map.entry("getDouble", "Double"),
            Map.entry("getFloat", "Float"),
            Map.entry("getBigDecimal", "BigDecimal"),
            Map.entry("getDate", "String"),
            Map.entry("getJSONObject", "object"),
            Map.entry("getJSONArray", "array"),
            Map.entry("getObject", "object"),
            Map.entry("getList", "array"),
            Map.entry("getOrDefault", "unknown"),
            Map.entry("get", "unknown"));

    /** 执行提取，返回契约 ExtractJob 产物载荷（candidates + warnings + stats + capabilities）。
     *
     * <p>stats 是**完整性闭环**的数据源：orchestrator 用它比对"发现文件数 == 解析成功数 +
     * 解析失败数"以及与 code_inventory 的文件数是否一致。任何"少读了文件"都必须在此可见，
     * 否则会静默变成"没发现接口"。
     */
    public Map<String, Object> run(ExtractRequest request) throws IOException {
        List<Map<String, Object>> candidates = new ArrayList<>();
        List<String> warnings = new ArrayList<>();
        List<Map<String, Object>> repoStats = new ArrayList<>();
        List<String> requested = request.protocols() == null || request.protocols().isEmpty()
                ? List.copyOf(SUPPORTED_PROTOCOLS)
                : List.copyOf(request.protocols());

        for (RepositoryInput repo : request.repositories()) {
            repoStats.add(extractOne(repo, candidates, warnings, requested));
        }

        List<String> unsupported = requested.stream()
                .filter(protocol -> !SUPPORTED_PROTOCOLS.contains(protocol))
                .toList();
        if (!unsupported.isEmpty()) {
            // 如实声明能力边界：请求了未接入的协议 → 告警 + 结构化声明（不虚构候选）
            for (String protocol : unsupported) {
                warnings.add("协议 " + protocol + " 的静态识别未接入（当前仅支持 rest），相关候选缺失");
            }
        }
        if (candidates.size() < MIN_CANDIDATES) {
            warnings.add("X-REV-04：候选数 " + candidates.size() + " 低于底线 " + MIN_CANDIDATES
                    + "，建议换用兜底仓库");
        }

        // 候选排序后统一编号（CAND-###，编号由静态侧确定、语义侧归一复核）
        candidates.sort(Comparator.comparing(c -> candidateSortKey(c)));
        for (int i = 0; i < candidates.size(); i++) {
            candidates.get(i).put("candidate_id", String.format("CAND-%03d", i + 1));
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("candidates", candidates);
        payload.put("warnings", warnings);
        payload.put("stats", statsPayload(repoStats, candidates.size()));
        payload.put("capabilities", capabilitiesPayload(requested, unsupported));
        return payload;
    }

    private static String candidateSortKey(Map<String, Object> candidate) {
        Map<String, Object> location = locationOf(candidate);
        return location.get("repo") + "/" + location.get("file") + ":" + location.get("start_line");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> locationOf(Map<String, Object> candidate) {
        return (Map<String, Object>) candidate.get("code_location");
    }

    /** 汇总统计：逐仓库明细 + 总数（供 orchestrator 做闭环校验）。 */
    private static Map<String, Object> statsPayload(
            List<Map<String, Object>> repoStats, int totalCandidates) {
        int discovered = 0;
        int parsed = 0;
        int failed = 0;
        for (Map<String, Object> stat : repoStats) {
            discovered += intOf(stat.get("files_discovered"));
            parsed += intOf(stat.get("files_parsed"));
            failed += intOf(stat.get("files_failed"));
        }
        Map<String, Object> totals = new LinkedHashMap<>();
        totals.put("candidates", totalCandidates);
        totals.put("files_discovered", discovered);
        totals.put("files_parsed", parsed);
        totals.put("files_failed", failed);
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("repositories", repoStats);
        payload.put("totals", totals);
        return payload;
    }

    /** 协议能力声明（支持的 / 本次请求的 / 请求但未接入的），如实落盘而非只留在日志里。 */
    private static Map<String, Object> capabilitiesPayload(
            List<String> requested, List<String> unsupported) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("supported", SUPPORTED_PROTOCOLS);
        payload.put("known", ALL_PROTOCOLS);
        payload.put("requested", requested);
        payload.put("unsupported", unsupported);
        return payload;
    }

    private static int intOf(Object value) {
        return value instanceof Number number ? number.intValue() : 0;
    }

    /** 单仓库提取：解析根 = target_sha 只读快照（拿不到则如实标记，不静默扫工作区）。 */
    private Map<String, Object> extractOne(
            RepositoryInput repo, List<Map<String, Object>> candidates,
            List<String> warnings, List<String> requestedProtocols) {
        Map<String, Object> stat = new LinkedHashMap<>();
        stat.put("name", repo.name());
        stat.put("workspace", repo.path());
        Path workspace = Path.of(repo.path());
        if (!Files.isDirectory(workspace)) {
            // 仓库级缺失：告警 + 跳过该仓库（提取阶段不终止整体，由调用方按 stats 决策）
            warnings.add("仓库 " + repo.name() + " 路径不存在：" + repo.path() + "，跳过候选提取");
            stat.put("status", "error");
            stat.put("error", Map.of(
                    "code", "REPO_PATH_NOT_FOUND",
                    "message", "仓库路径不存在或不是目录"));
            stat.put("files_discovered", 0);
            stat.put("files_parsed", 0);
            stat.put("files_failed", 0);
            stat.put("candidates", 0);
            return stat;
        }

        Path root = workspace;
        stat.put("snapshot", false);
        String targetSha = repo.targetSha();
        if (targetSha == null || targetSha.isBlank()) {
            // 没有目标提交 → 行号无法保证与 diff 同版本。如实标记 degraded（调用方必拦），
            // 而不是悄悄扫工作区给出"看起来能用但行号错位"的候选。
            warnings.add("仓库 " + repo.name() + " 未提供 target_sha：候选在工作区上解析，"
                    + "行号可能与变更行区间不同版本（行级判定不可靠）");
            stat.put("status", "degraded");
            stat.put("degraded_reason", "NO_TARGET_SHA");
        } else {
            stat.put("target_sha", targetSha);
            try {
                root = RepoSandbox.at(git, workspace, repo, targetSha);
                stat.put("snapshot", true);
                stat.put("snapshot_path", root.toString());
                stat.put("status", "ok");
            } catch (IOException | InterruptedException ex) {
                if (ex instanceof InterruptedException) {
                    Thread.currentThread().interrupt();
                }
                warnings.add("仓库 " + repo.name() + " 的 target 快照检出失败：" + reasonOf(ex));
                stat.put("status", "error");
                stat.put("error", Map.of(
                        "code", "SNAPSHOT_FAILED",
                        "message", "按 target_sha 检出只读快照失败（" + targetSha + "）："
                                + reasonOf(ex)));
                stat.put("files_discovered", 0);
                stat.put("files_parsed", 0);
                stat.put("files_failed", 0);
                stat.put("candidates", 0);
                return stat;
            }
        }

        int before = candidates.size();
        try {
            extractRepository(root, repo, candidates, warnings, stat, requestedProtocols);
        } catch (IOException ex) {
            warnings.add("仓库 " + repo.name() + " 源码遍历失败：" + ex.getMessage());
            stat.put("status", "error");
            stat.put("error", Map.of(
                    "code", "SOURCE_WALK_FAILED",
                    "message", "源码遍历失败：" + ex.getMessage()));
        }
        stat.put("candidates", candidates.size() - before);
        return stat;
    }

    private static String reasonOf(Exception ex) {
        String message = ex.getMessage() == null ? "" : ex.getMessage().replaceAll("\\s+", " ").trim();
        return message.isEmpty() ? ex.getClass().getSimpleName() : message;
    }

    /**
     * 相对路径规范化为正斜杠。
     *
     * <p>{@code Path.toString()} 在 Windows 上产出反斜杠（{@code a\b\C.java}），而
     * {@code changed_files} 来自 {@code git diff --name-status} 的文本输出，恒为正斜杠
     * （{@code a/b/C.java}）。下游 {@code _filter_candidates_by_scope} 是字符串精确匹配，
     * 两种风格永不相等 → 候选被全部过滤 → 误报 X-REV-04「变更范围内无接口候选」。
     * 这里统一成 git 口径，跨平台一致。
     */
    private static String normalizeRelative(Path relative) {
        return relative.toString().replace('\\', '/');
    }

    // 快照建立与清理见 RepoSandbox：scan 与 extract 必须用同一份实现数同一版本的文件，
    // 否则工作区领先于配置提交时两边计数不等，会被完整性门禁误判为「接口成批缺失」。

    private void extractRepository(
            Path root, RepositoryInput repo,
            List<Map<String, Object>> candidates, List<String> warnings,
            Map<String, Object> stat, List<String> requestedProtocols) throws IOException {
        JavaParser parser = parserFor(root);
        List<Path> sourceFiles;
        try (Stream<Path> stream = Files.walk(root)) {
            sourceFiles = stream
                    .filter(Files::isRegularFile)
                    .filter(ExtractJobRunner::isSupportedSourceFile)
                    .filter(p -> inScope(root.relativize(p), repo.includes()))
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        }
        List<Path> javaFiles = sourceFiles.stream()
                .filter(path -> path.getFileName().toString().endsWith(".java"))
                .toList();
        stat.put("files_discovered", sourceFiles.size());

        // 第一遍：建索引（简单类名 → 相对路径、类型#方法 → 定义行区间）。
        // 索引必须**先完整建好**才能解析候选的一跳调用：被调方法可能在文件序里更靠后。
        // 刻意不保留 AST（大仓库会把堆吃满），所以解析两遍、逐文件释放。
        MethodIndex index = buildIndex(root, javaFiles, parser);
        stat.put("types_indexed", index.classIndex().size());
        stat.put("methods_indexed", index.methods().size());

        // 第二遍：提取候选（含 code_spans 与变更证据）。
        int parsed = 0;
        int failed = 0;
        for (Path file : sourceFiles) {
            String relative = normalizeRelative(root.relativize(file));
            try {
                String source = Files.readString(file, StandardCharsets.UTF_8);
                if (relative.endsWith(".proto")) {
                    extractProtoFile(source, root, repo, relative, candidates, requestedProtocols);
                } else {
                    extractFile(source, root, repo, relative, candidates, parser, index,
                            requestedProtocols);
                }
                parsed++;
            } catch (ParseProblemException ex) {
                // X-REV-01：单文件解析失败记录跳过 + 原因，不中断整体
                failed++;
                warnings.add("X-REV-01：" + relative + " 解析失败跳过：" + ex.getMessage());
            } catch (IOException ex) {
                failed++;
                warnings.add("X-REV-01：" + relative + " 读取失败跳过：" + ex.getMessage());
            }
        }
        stat.put("files_parsed", parsed);
        stat.put("files_failed", failed);
    }

    private static boolean isSupportedSourceFile(Path file) {
        String name = file.getFileName().toString();
        return name.endsWith(".java") || name.endsWith(".proto");
    }

    /** 从 proto service/rpc 声明提取 gRPC 接口，行号直接锚定 target 快照。 */
    private static void extractProtoFile(
            String source, Path root, RepositoryInput repo, String relative,
            List<Map<String, Object>> candidates, List<String> requestedProtocols) {
        if (!requestedProtocols.contains("grpc")) {
            return;
        }
        String currentService = null;
        int serviceBraceDepth = 0;
        String[] lines = source.split("\\R", -1);
        int braceDepth = 0;
        for (int index = 0; index < lines.length; index++) {
            String line = lines[index];
            Matcher service = PROTO_SERVICE.matcher(line);
            if (service.find()) {
                currentService = service.group(1);
                serviceBraceDepth = braceDepth + count(line, '{') - count(line, '}');
            }
            if (currentService != null) {
                Matcher rpc = PROTO_RPC.matcher(line);
                if (rpc.find() && braceDepth >= serviceBraceDepth - 1) {
                    int lineNumber = index + 1;
                    Map<String, Object> location = new LinkedHashMap<>();
                    location.put("repo", repo.name());
                    location.put("file", relative);
                    location.put("start_line", lineNumber);
                    location.put("end_line", lineNumber);
                    Map<String, Object> candidate = new LinkedHashMap<>();
                    candidate.put("protocol", "grpc");
                    candidate.put("signature", "gRPC " + currentService + "." + rpc.group(1));
                    candidate.put("code_location", location);
                    candidate.put("snippet", line.trim());
                    candidate.put("method_body", line.trim());
                    candidate.put("method_body_truncated", false);
                    candidate.put("method_body_char_count", line.length());
                    candidate.put("is_fallback", repo.fallback());
                    candidate.put("confidence", 0.95);
                    candidate.put("related_files", List.of(
                            Map.of("repo", repo.name(), "file", relative, "role", "proto",
                                    "start_line", lineNumber, "end_line", lineNumber)));
                    List<Map<String, Object>> spans = List.of(
                            span(relative, "proto", "body", lineNumber, lineNumber));
                    candidate.put("code_spans", spans);
                    Map<String, Object> evidence = buildChangeEvidence(
                            root, repo, relative, spans,
                            (List<Map<String, Object>>) candidate.get("related_files"));
                    if (evidence != null) {
                        candidate.put("change_evidence", evidence);
                    }
                    candidates.add(candidate);
                }
            }
            braceDepth += count(line, '{') - count(line, '}');
            if (currentService != null && braceDepth < serviceBraceDepth) {
                currentService = null;
            }
        }
    }

    private static int count(String text, char value) {
        int count = 0;
        for (int index = 0; index < text.length(); index++) {
            if (text.charAt(index) == value) {
                count++;
            }
        }
        return count;
    }

    private void extractFile(
            String source, Path root, RepositoryInput repo, String relative,
            List<Map<String, Object>> candidates, JavaParser parser, MethodIndex index,
            List<String> requestedProtocols) {
        ParseResult<CompilationUnit> result = parser.parse(source);
        if (!result.isSuccessful() || result.getResult().isEmpty()) {
            // 解析失败路径：转交外层 catch 统一记 X-REV-01 告警（单文件跳过不中断）
            throw new ParseProblemException(result.getProblems());
        }
        CompilationUnit unit = result.getResult().get();
        for (ClassOrInterfaceDeclaration cls : unit.findAll(ClassOrInterfaceDeclaration.class)) {
            boolean restController = cls.getAnnotationByName("RestController").isPresent();
            boolean classResponseBody = cls.getAnnotationByName("ResponseBody").isPresent();
            boolean controller = cls.getAnnotationByName("Controller").isPresent();
            boolean protocolClass = cls.getAnnotationByName("WebService").isPresent()
                    || cls.getAnnotationByName("Endpoint").isPresent()
                    || cls.getAnnotationByName("GrpcService").isPresent()
                    || cls.getMethods().stream().anyMatch(ExtractJobRunner::hasSpecialProtocolAnnotation);
            if (!restController && !controller && !classResponseBody && !protocolClass) {
                continue;
            }
            // 类级 @RequestMapping 前缀（方法路径拼接基础）；数组路径必须全部展开。
            Optional<AnnotationExpr> classMapping = cls.getAnnotationByName("RequestMapping");
            List<String> prefixes = classMapping
                    .map(ExtractJobRunner::annotationPaths)
                    .orElse(List.of(""));
            if (prefixes.isEmpty()) {
                prefixes = List.of("");
            }
            // 注入点：@Autowired/@Resource 字段 **+ 构造器参数**（评审 §2：构造器注入原先漏掉）
            Map<String, String> injected = injectedTypes(cls);
            for (MethodDeclaration method : cls.getMethods()) {
                boolean methodResponseBody = method.getAnnotationByName("ResponseBody").isPresent();
                if (!restController && !classResponseBody && !methodResponseBody && !protocolClass) {
                    continue;
                }
                for (String prefix : prefixes) {
                    for (Map.Entry<String, String> mapping : MAPPING_ANNOTATIONS.entrySet()) {
                        if (!requestedProtocols.contains("rest")) {
                            break;
                        }
                        for (AnnotationExpr ann : annotationsNamed(method, mapping.getKey())) {
                            for (String path : annotationPaths(ann)) {
                                candidates.add(candidate(
                                        root, repo, relative, cls, method, mapping.getValue(),
                                        joinPath(prefix, path), unit, index, injected, parser));
                            }
                        }
                    }
                    for (Map.Entry<String, String> mapping : GRAPHQL_ANNOTATIONS.entrySet()) {
                        if (!requestedProtocols.contains("graphql")) {
                            break;
                        }
                        for (AnnotationExpr ann : annotationsNamed(method, mapping.getKey())) {
                            candidates.add(candidateForProtocol(
                                    root, repo, relative, cls, method, "graphql",
                                    graphqlSignature(mapping.getValue(), ann, method), unit, index,
                                    injected, parser));
                        }
                    }
                    for (Map.Entry<String, String> mapping : SOAP_ANNOTATIONS.entrySet()) {
                        if (!requestedProtocols.contains("soap")) {
                            break;
                        }
                        for (AnnotationExpr ann : annotationsNamed(method, mapping.getKey())) {
                            candidates.add(candidateForProtocol(
                                    root, repo, relative, cls, method, "soap",
                                    "SOAP " + annotationName(ann, method.getNameAsString()),
                                    unit, index, injected, parser));
                        }
                    }
                    for (Map.Entry<String, String> mapping : MQ_ANNOTATIONS.entrySet()) {
                        if (!requestedProtocols.contains("mq")) {
                            break;
                        }
                        for (AnnotationExpr ann : annotationsNamed(method, mapping.getKey())) {
                            candidates.add(candidateForProtocol(
                                    root, repo, relative, cls, method, "mq",
                                    "MQ " + mapping.getValue() + " "
                                            + annotationName(ann, method.getNameAsString()),
                                    unit, index, injected, parser));
                        }
                    }
                    for (AnnotationExpr ann : annotationsNamed(method, "RequestMapping")) {
                        if (!requestedProtocols.contains("rest")) {
                            break;
                        }
                        for (String httpMethod : requestMappingMethods(ann)) {
                            for (String path : annotationPaths(ann)) {
                                candidates.add(candidate(
                                        root, repo, relative, cls, method, httpMethod,
                                        joinPath(prefix, path), unit, index, injected, parser));
                            }
                        }
                    }
                }
            }
        }
    }

    private static boolean hasSpecialProtocolAnnotation(MethodDeclaration method) {
        return GRAPHQL_ANNOTATIONS.keySet().stream()
                        .anyMatch(name -> method.getAnnotationByName(name).isPresent())
                || SOAP_ANNOTATIONS.keySet().stream()
                        .anyMatch(name -> method.getAnnotationByName(name).isPresent())
                || MQ_ANNOTATIONS.keySet().stream()
                        .anyMatch(name -> method.getAnnotationByName(name).isPresent());
    }

    private static String graphqlSignature(
            String operation, AnnotationExpr annotation, MethodDeclaration method) {
        return "GraphQL " + operation + " " + annotationName(annotation, method.getNameAsString());
    }

    /** 从常见协议注解中读取 operation/value/path/destination 等字符串。 */
    private static String annotationName(AnnotationExpr annotation, String fallback) {
        if (annotation instanceof SingleMemberAnnotationExpr single) {
            List<String> values = stringArgs(single.getMemberValue());
            if (!values.isEmpty()) {
                return values.get(0);
            }
        }
        if (annotation instanceof NormalAnnotationExpr normal) {
            for (String member : List.of(
                    "name", "value", "path", "operationName", "destination", "topic",
                    "topics", "queue", "queues")) {
                for (MemberValuePair pair : normal.getPairs()) {
                    if (member.equals(pair.getNameAsString())) {
                        List<String> values = stringArgs(pair.getValue());
                        if (!values.isEmpty()) {
                            return values.get(0);
                        }
                    }
                }
            }
        }
        return fallback;
    }

    private static Map<String, Object> candidate(
            Path root, RepositoryInput repo, String relative, ClassOrInterfaceDeclaration controller,
            MethodDeclaration method, String httpMethod, String signature,
            CompilationUnit unit, MethodIndex index, Map<String, String> injected, JavaParser parser) {
        return candidateForProtocol(
                root, repo, relative, controller, method, "rest", httpMethod + " " + signature,
                unit, index, injected, parser);
    }

    private static Map<String, Object> candidateForProtocol(
            Path root, RepositoryInput repo, String relative, ClassOrInterfaceDeclaration controller,
            MethodDeclaration method, String protocol, String fullSignature,
            CompilationUnit unit, MethodIndex index, Map<String, String> injected, JavaParser parser) {
        Map<String, Object> location = new LinkedHashMap<>();
        location.put("repo", repo.name());
        location.put("file", relative);
        location.put("start_line", method.getBegin().map(p -> p.line).orElse(1));
        location.put("end_line", method.getEnd().map(p -> p.line).orElse(1));

        String snippet = method.toString();
        if (LOG.isDebugEnabled()) {
            LOG.debug("候选定位：{} {} ({}:{})", protocol, fullSignature, relative,
                    location.get("start_line"));
        }

        Map<String, Object> candidate = new LinkedHashMap<>();
        candidate.put("protocol", protocol);
        candidate.put("signature", fullSignature);
        candidate.put("code_location", location);
        candidate.put("controller_name", controller.getNameAsString());
        List<Map<String, Object>> controllerAnnotations = controller.getAnnotations().stream()
                .map(annotation -> {
                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("name", annotation.getNameAsString());
                    annotation.getBegin().ifPresent(position -> item.put("start_line", position.line));
                    annotation.getEnd().ifPresent(position -> item.put("end_line", position.line));
                    return item;
                })
                .toList();
        candidate.put("controller_annotations", controllerAnnotations);
        candidate.put("snippet", snippet);
        String methodBody = method.toString();
        candidate.put("method_body", methodBody);
        candidate.put("method_body_truncated", false);
        List<Map<String, Object>> parameterEvidence = extractParameterEvidence(method);
        if (!parameterEvidence.isEmpty()) {
            candidate.put("parameter_evidence", parameterEvidence);
        }
        candidate.put("method_body_char_count", method.toString().length());
        List<Map<String, Object>> methodContext = methodBodyContext(method, parameterEvidence);
        if (!methodContext.isEmpty()) {
            candidate.put("method_body_context", methodContext);
        }
        // D5 分列标记随候选透传（兜底演示仓库不计入真实结论）
        candidate.put("is_fallback", repo.fallback());
        candidate.put("confidence", STATIC_CONFIDENCE);
        // R-REV-04②：填充契约既有的 related_files（Controller 自身 + 注入字段 + 本方法
        // 签名引用的类型）。调用方据此按「本人或其关联文件被改」判定接口归属——依赖
        // 改了、Controller 文件没改的接口同样算本次变动。
        List<Map<String, Object>> relatedFiles = collectRelatedFiles(
                repo.name(), relative, controller, method, index.classIndex(), injected);
        candidate.put("related_files", relatedFiles);

        // code_spans（评审 §2/#51）：从接口方法递归展开仓内可解析调用链。
        // 只有确实存在未解析目标、动态接收者或递归深度上限时才标记 partial，普通
        // JSONObject.getString()/DTO getter 等外部叶子调用不再误报“调用链需复核”。
        CallGraphResult callGraph = collectCodeSpans(
                root, unit, controller, method, relative, injected, index, parser);
        candidate.put("code_spans", callGraph.spans());
        Map<String, Object> callGraphSummary = new LinkedHashMap<>();
        callGraphSummary.put("status", callGraph.incomplete() ? "partial" : "complete");
        callGraphSummary.put("resolved_calls", callGraph.resolvedCalls());
        callGraphSummary.put("resolved_nodes", Math.max(0, callGraph.spans().size() - 1));
        callGraphSummary.put("unresolved_calls", callGraph.unresolved().size());
        callGraphSummary.put("max_depth", callGraph.maxDepth());
        callGraphSummary.put("edges", callGraph.edges());
        candidate.put("call_graph", callGraphSummary);
        if (callGraph.incomplete()) {
            candidate.put("call_graph_incomplete", true);
        }
        if (!callGraph.unresolved().isEmpty()) {
            candidate.put("unresolved", callGraph.unresolved());
        }

        // 变更证据（#52）：把完整调用链 spans 与 changed_lines 求交，给出「本体改了 / 调用链改了 /
        // 仅文件级命中 / 仅关联文件命中」四级判定 + 实际变动代码片段。
        Map<String, Object> evidence = buildChangeEvidence(
                root, repo, relative, callGraph.spans(), relatedFiles);
        if (evidence != null) {
            candidate.put("change_evidence", evidence);
        }

        List<Map<String, Object>> requestBody = extractRequestBodyFields(
                root, relative, method, unit, parser, index, injected);
        if (requestBody != null) {
            candidate.put("request_body", requestBody);
        }
        return candidate;
    }

    /** 收集候选的关联文件（R-REV-04②）：Controller 自身 + 注入字段类型 + 本方法参数/返回类型。
     *
     * <p>这是「接口是否属于本次变更」的判据之一。变更清单是文件级的，只看 Controller 文件
     * 会漏掉「依赖（Service/DTO/Entity/Enum）改了、Controller 文件没改」的接口——而这类
     * 接口的行为确实随依赖一起变了。
     */
    private static List<Map<String, Object>> collectRelatedFiles(
            String repoName, String controllerFile, ClassOrInterfaceDeclaration controller,
            MethodDeclaration method, Map<String, String> classIndex,
            Map<String, String> injected) {
        // file → role：去重，Controller 自身先占位（依赖里同名文件不覆盖它的角色）
        Map<String, String> files = new LinkedHashMap<>();
        files.put(controllerFile, "controller");

        Set<String> typeNames = new LinkedHashSet<>(injected.values());
        for (Parameter param : method.getParameters()) {
            typeNames.addAll(simpleTypeNames(param.getType()));
        }
        typeNames.addAll(simpleTypeNames(method.getType()));

        for (String typeName : typeNames) {
            addRelated(files, classIndex, typeName);
            // 注入的是接口（XxxService）时，实现类（XxxServiceImpl）往往才是被改的那个文件。
            addRelated(files, classIndex, typeName + "Impl");
        }

        List<Map<String, Object>> related = new ArrayList<>();
        for (Map.Entry<String, String> entry : files.entrySet()) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("repo", repoName);
            item.put("file", entry.getKey());
            item.put("role", entry.getValue());
            related.add(item);
        }
        return related;
    }

    private static void addRelated(
            Map<String, String> files, Map<String, String> classIndex, String typeName) {
        String path = classIndex.get(typeName);
        if (path != null) {
            files.putIfAbsent(path, roleOf(typeName));
        }
    }

    /** 按命名约定判角色（契约枚举 controller/service/dto/proto/wsdl/listener/other）。 */
    private static String roleOf(String typeName) {
        if (typeName.endsWith("Service") || typeName.endsWith("ServiceImpl")
                || typeName.endsWith("Mapper")) {
            return "service";
        }
        if (typeName.endsWith("Query") || typeName.endsWith("Request")
                || typeName.endsWith("Response") || typeName.endsWith("Dto")
                || typeName.endsWith("DTO") || typeName.endsWith("Vo")
                || typeName.endsWith("VO")) {
            return "dto";
        }
        return "other";
    }

    /** 类型 / 方法索引（第一遍解析产出，供递归调用链解析）。 */
    private record MethodIndex(
            Map<String, String> classIndex,
            Set<String> ambiguousTypes,
            Map<String, List<Span>> methods,
            Map<String, List<MethodTarget>> methodTargets) {
    }

    /** 一段源码位置（仓库内相对路径 + 行区间，闭区间、1 基、同一提交版本）。 */
    private record Span(String file, int startLine, int endLine) {
    }

    /** 可重新加载的被调方法元数据：不长期持有 AST，避免大仓库扫描时堆内存膨胀。 */
    private record MethodTarget(
            String ownerType, String methodName, String file, int startLine, int endLine,
            List<String> parameterNames, int parameterCount, String returnType) {
    }

    /** 调用链解析结果：resolved 与 unresolved 必须同时输出，不能只给一个模糊提示。 */
    private record CallGraphResult(
            List<Map<String, Object>> spans,
            List<Map<String, Object>> edges,
            List<Map<String, Object>> unresolved,
            boolean incomplete,
            int resolvedCalls,
            int maxDepth) {
    }

    /** 递归遍历期间的可变状态，候选完成后转成不可变结果。 */
    private static final class CallGraphState {
        private final List<Map<String, Object>> spans;
        private final List<Map<String, Object>> edges = new ArrayList<>();
        private final List<Map<String, Object>> unresolved;
        private final Set<String> recordedUnresolved = new LinkedHashSet<>();
        private final Set<String> visited = new LinkedHashSet<>();
        private final Set<String> active = new LinkedHashSet<>();
        private int resolvedCalls;
        private int maxDepth;
        private boolean incomplete;

        private CallGraphState(
                List<Map<String, Object>> spans, List<Map<String, Object>> unresolved) {
            this.spans = spans;
            this.unresolved = unresolved;
        }
    }

    /** 动态 body 字段追踪队列项，parameterName 是当前方法中对应原始 body 的形参名。 */
    private record FieldTrace(
            MethodDeclaration method, CompilationUnit unit, String file,
            String parameterName, int depth) {
    }

    /** 动态字段访问链上的一段字段证据。 */
    private record FieldAccess(
            String path, String type, String operation, String defaultExpression, int line) {
    }

    /** 集合/容器类型：注入字段声明成 List<Xxx> 时，接收者类型应取 Xxx 而非 List。 */
    private static final Set<String> CONTAINER_TYPES =
            Set.of("List", "Set", "Map", "Collection", "Optional", "Iterable", "Queue", "Deque");

    /** 第一遍解析：建类型 / 方法索引（逐文件解析后立即释放 AST，避免大仓库堆溢出）。
     *
     * <p>索引必须**先完整建好**才能解析候选的一跳调用——被调方法可能在文件序里更靠后。
     * 代价是解析两遍（不保留 AST 的取舍）。
     */
    private static MethodIndex buildIndex(Path root, List<Path> javaFiles, JavaParser parser) {
        Map<String, String> classIndex = new LinkedHashMap<>();
        Set<String> ambiguous = new LinkedHashSet<>();
        Map<String, List<Span>> methods = new LinkedHashMap<>();
        Map<String, List<MethodTarget>> methodTargets = new LinkedHashMap<>();
        for (Path file : javaFiles) {
            String relative = normalizeRelative(root.relativize(file));
            CompilationUnit unit = parseOrNull(file, parser);
            if (unit == null) {
                continue;
            }
            for (ClassOrInterfaceDeclaration cls : unit.findAll(ClassOrInterfaceDeclaration.class)) {
                String typeName = cls.getNameAsString();
                String previous = classIndex.putIfAbsent(typeName, relative);
                if (previous != null && !previous.equals(relative)) {
                    // 同名类出现在多个文件：一跳解析不能猜（宁标 unresolved）
                    ambiguous.add(typeName);
                }
                for (MethodDeclaration method : cls.getMethods()) {
                    int start = method.getBegin().map(p -> p.line).orElse(1);
                    int end = method.getEnd().map(p -> p.line).orElse(start);
                    methods.computeIfAbsent(
                                    typeName + "#" + method.getNameAsString(),
                                    key -> new ArrayList<>())
                            .add(new Span(relative, start, end));
                    methodTargets.computeIfAbsent(
                                    typeName + "#" + method.getNameAsString(),
                                    key -> new ArrayList<>())
                            .add(new MethodTarget(
                                    typeName,
                                    method.getNameAsString(),
                                    relative,
                                    start,
                                    end,
                                    method.getParameters().stream()
                                            .map(Parameter::getNameAsString)
                                            .toList(),
                                    method.getParameters().size(),
                                    primaryTypeName(method.getType())));
                }
            }
        }
        return new MethodIndex(classIndex, ambiguous, methods, methodTargets);
    }

    /** 索引阶段解析文件；失败返回 null（候选阶段会再报一次 X-REV-01，不重复刷告警）。 */
    private static CompilationUnit parseOrNull(Path file, JavaParser parser) {
        try {
            ParseResult<CompilationUnit> result =
                    parser.parse(Files.readString(file, StandardCharsets.UTF_8));
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return null;
            }
            return result.getResult().get();
        } catch (IOException | RuntimeException ex) {
            LOG.debug("索引阶段解析失败：{}（{}）", file.getFileName(), ex.getMessage());
            return null;
        }
    }

    /** 注入点：字段名 → 类型简单名（{@code @Autowired}/{@code @Resource} 字段 + 构造器参数）。
     *
     * <p>构造器注入（评审 §2 指出的漏项）在 Spring 里与字段注入等价，一并纳入——此前只认
     * 字段注解，构造器注入的依赖会被整段漏掉。
     */
    private static Map<String, String> injectedTypes(ClassOrInterfaceDeclaration cls) {
        Map<String, String> injected = new LinkedHashMap<>();
        for (FieldDeclaration field : cls.getFields()) {
            if (field.getAnnotationByName("Autowired").isEmpty()
                    && field.getAnnotationByName("Resource").isEmpty()) {
                continue;
            }
            for (VariableDeclarator variable : field.getVariables()) {
                String typeName = primaryTypeName(variable.getType());
                if (typeName != null) {
                    injected.putIfAbsent(variable.getNameAsString(), typeName);
                }
            }
        }
        for (ConstructorDeclaration constructor : cls.getConstructors()) {
            for (Parameter param : constructor.getParameters()) {
                String typeName = primaryTypeName(param.getType());
                if (typeName != null) {
                    injected.putIfAbsent(param.getNameAsString(), typeName);
                }
            }
        }
        return injected;
    }

    /** 类型的主简单名（跳过 List/Set/Map 等容器包装）。 */
    private static String primaryTypeName(Type type) {
        for (String name : simpleTypeNames(type)) {
            if (!CONTAINER_TYPES.contains(name)) {
                return name;
            }
        }
        return null;
    }

    /** 候选的代码跨度（#51）：本体方法 + 仓内可解析调用链的全部方法定义。
     *
     * <p>为什么不只用 related_files：那是“类型级邻接”，本方法是行级可达。这里从接口
     * 方法开始递归解析同类方法、字段/构造器注入依赖、局部变量依赖和链式返回值，直到遇到
     * 循环、外部叶子或真正无法定位的调用。无法定位的调用保留在 unresolved，不能静默跳过。
     */
    private static CallGraphResult collectCodeSpans(
            Path root, CompilationUnit rootUnit, ClassOrInterfaceDeclaration controller,
            MethodDeclaration method, String controllerFile, Map<String, String> injected,
            MethodIndex index, JavaParser parser) {
        List<Map<String, Object>> spans = new ArrayList<>();
        List<Map<String, Object>> unresolved = new ArrayList<>();
        int start = method.getBegin().map(p -> p.line).orElse(1);
        int end = method.getEnd().map(p -> p.line).orElse(start);
        Map<String, Object> entrySpan = span(controllerFile, "controller", "body", start, end);
        entrySpan.put("method_name", method.getNameAsString());
        entrySpan.put("parameter_count", method.getParameters().size());
        spans.add(entrySpan);
        CallGraphState state = new CallGraphState(spans, unresolved);
        walkCallGraph(
                root, rootUnit, method, controllerFile, controller.getNameAsString(), injected,
                index, parser, 0, state);
        // resolveCallTarget 记录的未解析调用也意味着这条调用链不能宣称 complete。
        state.incomplete = state.incomplete || !unresolved.isEmpty();
        return new CallGraphResult(
                spans, state.edges, unresolved, state.incomplete, state.resolvedCalls, state.maxDepth);
    }

    /** 深度优先展开单个方法；visited 防止共享方法重复展开，active 防止递归环路。 */
    private static void walkCallGraph(
            Path root, CompilationUnit currentUnit, MethodDeclaration method, String file,
            String ownerType, Map<String, String> injected, MethodIndex index, JavaParser parser,
            int depth, CallGraphState state) {
        int start = method.getBegin().map(p -> p.line).orElse(1);
        String methodKey = file + ":" + start + ":" + ownerType;
        if (!state.visited.add(methodKey)) {
            return;
        }
        state.maxDepth = Math.max(state.maxDepth, depth);
        if (!state.active.add(methodKey)) {
            return;
        }
        try {
            Map<String, String> receiverTypes = declaredTypes(method, injected);
            for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
                MethodTarget target = resolveCallTarget(
                        root, currentUnit, method, ownerType, receiverTypes, call, index,
                        state.unresolved, state.recordedUnresolved, parser);
                if (target == null) {
                    continue;
                }
                state.resolvedCalls++;
                int targetDepth = depth + 1;
                state.maxDepth = Math.max(state.maxDepth, targetDepth);
                String role = roleOf(target.ownerType());
                Map<String, Object> edge = new LinkedHashMap<>();
                edge.put("caller_file", file);
                edge.put("caller_role", depth == 0 ? "controller" : roleOf(ownerType));
                edge.put("caller_method", method.getNameAsString());
                edge.put("caller_start_line", start);
                edge.put("caller_end_line", method.getEnd().map(p -> p.line).orElse(start));
                edge.put("callee_file", target.file());
                edge.put("callee_role", role);
                edge.put("callee_method", target.methodName());
                edge.put("callee_start_line", target.startLine());
                edge.put("callee_end_line", target.endLine());
                edge.put("depth", targetDepth);
                call.getBegin().ifPresent(begin -> edge.put("call_line", begin.line));
                state.edges.add(edge);
                if (!containsSpan(state.spans, target)) {
                    Map<String, Object> targetSpan = span(
                            target.file(), role, "callee", target.startLine(), target.endLine());
                    targetSpan.put("method_name", target.methodName());
                    targetSpan.put("parameter_count", target.parameterCount());
                    targetSpan.put("depth", targetDepth);
                    call.getBegin().ifPresent(begin -> targetSpan.put("call_line", begin.line));
                    targetSpan.put("caller_file", file);
                    state.spans.add(targetSpan);
                }
                String targetKey = target.file() + ":" + target.startLine() + ":" + target.ownerType();
                if (state.active.contains(targetKey)) {
                    // 已确认的递归环，不是“未解析”；链路边已经记录。
                    continue;
                }
                if (targetDepth >= MAX_CALL_GRAPH_DEPTH) {
                    state.incomplete = true;
                    addUnresolved(
                            state.unresolved, state.recordedUnresolved, target.ownerType(),
                            target.methodName(), target.file(), role,
                            "调用链达到静态展开深度上限 " + MAX_CALL_GRAPH_DEPTH);
                    continue;
                }
                MethodDeclaration targetMethod = methodForTarget(root, currentUnit, target, parser);
                if (targetMethod == null) {
                    state.incomplete = true;
                    addUnresolved(
                            state.unresolved, state.recordedUnresolved, target.ownerType(),
                            target.methodName(), target.file(), role,
                            "已定位方法元数据，但无法重新读取方法源码");
                    continue;
                }
                CompilationUnit targetUnit = targetMethod.findCompilationUnit().orElse(currentUnit);
                walkCallGraph(
                        root, targetUnit, targetMethod, target.file(), target.ownerType(),
                        declaredTypes(targetMethod, Map.of()), index, parser, targetDepth, state);
            }
        } finally {
            state.active.remove(methodKey);
        }
    }

    /** 解析一个调用表达式的静态接收者；仓外 getter/工具类作为已知叶子，不误报为未完成。 */
    private static MethodTarget resolveCallTarget(
            Path root, CompilationUnit currentUnit, MethodDeclaration caller, String ownerType,
            Map<String, String> receiverTypes, MethodCallExpr call, MethodIndex index,
            List<Map<String, Object>> unresolved, Set<String> recorded, JavaParser parser) {
        String methodName = call.getNameAsString();
        var scope = call.getScope().orElse(null);
        String targetOwner = ownerType;
        if (scope == null || scope instanceof ThisExpr) {
            targetOwner = ownerType;
        } else if (scope instanceof NameExpr name) {
            targetOwner = receiverTypes.get(name.getNameAsString());
            if (targetOwner == null) {
                if (Character.isUpperCase(name.getNameAsString().charAt(0))) {
                    return null; // 外部静态工具类（如 StringUtils）
                }
                addUnresolved(
                        unresolved, recorded, ownerType, methodName, null, null,
                        "接收者 " + name.getNameAsString() + " 无法确定静态类型");
                return null;
            }
        } else if (scope instanceof FieldAccessExpr field) {
            targetOwner = receiverTypes.get(field.getNameAsString());
            if (targetOwner == null) {
                addUnresolved(
                        unresolved, recorded, ownerType, methodName, null, null,
                        "字段接收者 " + field + " 无法确定静态类型");
                return null;
            }
        } else if (scope instanceof MethodCallExpr nested) {
            MethodTarget nestedTarget = resolveCallTarget(
                    root, currentUnit, caller, ownerType, receiverTypes, nested, index,
                    unresolved, recorded, parser);
            if (nestedTarget == null || nestedTarget.returnType() == null) {
                return null; // 外部 getter 的链式调用，不把叶子误算成业务未解析
            }
            targetOwner = nestedTarget.returnType();
        } else if (scope instanceof ObjectCreationExpr created) {
            targetOwner = primaryTypeName(created.getType());
        } else {
            addUnresolved(
                    unresolved, recorded, ownerType, methodName, null, null,
                    "复杂接收者（无法静态确定类型）：" + scope);
            return null;
        }
        if (targetOwner == null || !index.classIndex().containsKey(targetOwner)) {
            return null; // 参数 DTO、JSON、JDK 或第三方库方法是仓外叶子
        }
        return resolveTarget(targetOwner, methodName, index, unresolved, recorded, roleOf(targetOwner));
    }

    /** 当前方法内所有可静态确定的变量类型：注入字段、普通字段、形参和局部变量。 */
    private static Map<String, String> declaredTypes(
            MethodDeclaration method, Map<String, String> injected) {
        Map<String, String> result = new LinkedHashMap<>(injected);
        method.findAncestor(ClassOrInterfaceDeclaration.class)
                .ifPresent(owner -> {
                    for (FieldDeclaration field : owner.getFields()) {
                        for (VariableDeclarator variable : field.getVariables()) {
                            String type = primaryTypeName(variable.getType());
                            if (type != null) {
                                result.putIfAbsent(variable.getNameAsString(), type);
                            }
                        }
                    }
                });
        for (Parameter parameter : method.getParameters()) {
            String type = primaryTypeName(parameter.getType());
            if (type != null) {
                result.putIfAbsent(parameter.getNameAsString(), type);
            }
        }
        for (VariableDeclarator variable : method.findAll(VariableDeclarator.class)) {
            String type = primaryTypeName(variable.getType());
            if (type != null) {
                result.putIfAbsent(variable.getNameAsString(), type);
            }
        }
        return result;
    }

    /** 解析 (类型, 方法) → 定义位置；不唯一则记 unresolved 并返回 null。 */
    private static MethodTarget resolveTarget(
            String ownerType, String methodName, MethodIndex index,
            List<Map<String, Object>> unresolved, Set<String> recorded, String role) {
        if (index.ambiguousTypes().contains(ownerType)) {
            addUnresolved(unresolved, recorded, ownerType, methodName, null, role,
                    "同名类型出现在多个文件，无法确定唯一实现");
            return null;
        }
        if (!index.classIndex().containsKey(ownerType)) {
            addUnresolved(unresolved, recorded, ownerType, methodName, null, role,
                    "类型不在本仓库源码内（跨模块或外部依赖），无法静态定位");
            return null;
        }
        List<MethodTarget> direct = index.methodTargets().get(ownerType + "#" + methodName);
        if (direct != null && direct.size() == 1) {
            return direct.get(0);
        }
        // 注入接口（XxxService）时真正执行的是实现类（XxxServiceImpl）
        List<MethodTarget> viaImpl = index.methodTargets().get(ownerType + "Impl#" + methodName);
        if (viaImpl != null && viaImpl.size() == 1) {
            return viaImpl.get(0);
        }
        String reason = direct == null && viaImpl == null
                ? "类型内没有该方法（可能继承自父类/接口默认实现，1 跳静态分析未覆盖）"
                : "该方法在仓库内有多个定义，无法确定唯一实现";
        addUnresolved(unresolved, recorded, ownerType, methodName,
                index.classIndex().get(ownerType), role, reason);
        return null;
    }

    private static void addUnresolved(
            List<Map<String, Object>> unresolved, Set<String> recorded, String type,
            String methodName, String file, String role, String reason) {
        if (unresolved.size() >= MAX_UNRESOLVED_PER_CANDIDATE
                || !recorded.add(type + "#" + methodName + "|" + reason)) {
            return;
        }
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("type", type);
        entry.put("method", methodName);
        if (file != null) {
            entry.put("file", file);
        }
        if (role != null) {
            entry.put("role", role);
        }
        entry.put("reason", reason);
        unresolved.add(entry);
    }

    private static Map<String, Object> span(
            String file, String role, String kind, int startLine, int endLine) {
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("file", file);
        entry.put("role", role);
        entry.put("kind", kind);
        entry.put("start_line", startLine);
        entry.put("end_line", endLine);
        return entry;
    }

    private static boolean containsSpan(List<Map<String, Object>> spans, Span target) {
        for (Map<String, Object> existing : spans) {
            if (existing.get("file").equals(target.file())
                    && existing.get("start_line").equals(target.startLine())
                    && existing.get("end_line").equals(target.endLine())) {
                return true;
            }
        }
        return false;
    }

    private static boolean containsSpan(List<Map<String, Object>> spans, MethodTarget target) {
        return containsSpan(
                spans, new Span(target.file(), target.startLine(), target.endLine()));
    }

    /** 变更证据（#52）：code_spans ∩ changed_lines 求交，给出四级判定 + 实际变动代码片段。
     *
     * <p>判定优先级：{@code body}（本体方法改了）> {@code callee}（调用链上的代码改了）>
     * {@code file}（本体文件被改但改动行不在任何跨度内）> {@code related_file}（只有关联
     * 文件被改）> {@code none}（本次变更没碰到它）。
     *
     * <p>只有 {@code changed_lines} 存在时才产出（未圈定变更范围时返回 null，调用方退回
     * 文件级口径）。{@code added} 的行号是 target 版本，与快照源码同版本，可直接取片段。
     */
    private static Map<String, Object> buildChangeEvidence(
            Path root, RepositoryInput repo, String controllerFile,
            List<Map<String, Object>> spans, List<Map<String, Object>> relatedFiles) {
        Map<String, List<int[]>> added = addedRanges(repo);
        if (added.isEmpty()) {
            return null;
        }
        List<Map<String, Object>> hits = new ArrayList<>();
        List<Map<String, Object>> snippets = new ArrayList<>();
        String level = null;
        for (Map<String, Object> item : spans) {
            String file = String.valueOf(item.get("file"));
            List<int[]> changed = added.getOrDefault(file, List.of());
            if (changed.isEmpty()) {
                continue;
            }
            List<List<Integer>> hit = intersect(
                    changed, toInt(item.get("start_line")), toInt(item.get("end_line")));
            if (hit.isEmpty()) {
                continue;
            }
            String kind = String.valueOf(item.get("kind"));
            if ("body".equals(kind) || level == null) {
                level = "body".equals(kind) ? "body" : "callee";
            }
            Map<String, Object> hitEntry = new LinkedHashMap<>(item);
            hitEntry.put("changed", hit);
            hits.add(hitEntry);
            for (List<Integer> range : hit) {
                String text = readLines(
                        root, file, range.get(0), range.get(1));
                if (text == null) {
                    continue;
                }
                Map<String, Object> snippet = new LinkedHashMap<>();
                snippet.put("file", file);
                snippet.put("start_line", range.get(0));
                snippet.put("end_line", range.get(1));
                snippet.put("text", text);
                snippets.add(snippet);
            }
        }
        List<Map<String, Object>> sources = new ArrayList<>();
        if (level == null) {
            if (added.containsKey(controllerFile)) {
                level = "file";
                sources.add(Map.of("file", controllerFile, "role", "controller"));
            } else {
                for (Map<String, Object> related : relatedFiles) {
                    String file = String.valueOf(related.get("file"));
                    if (!controllerFile.equals(file) && added.containsKey(file)) {
                        level = "related_file";
                        sources.add(Map.of("file", file, "role", String.valueOf(related.get("role"))));
                    }
                }
                if (level == null) {
                    level = "none";
                }
            }
        }

        Map<String, Object> evidence = new LinkedHashMap<>();
        evidence.put("level", level);
        evidence.put("hits", hits);
        if (!sources.isEmpty()) {
            evidence.put("sources", sources);
        }
        evidence.put("changed_snippets", snippets);
        evidence.put("truncated", false);
        return evidence;
    }

    /** changed_lines（契约对象）→ {路径: 新增侧行区间}。行号是 target 版本。 */
    private static Map<String, List<int[]>> addedRanges(RepositoryInput repo) {
        Map<String, List<int[]>> result = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : repo.changedLines().entrySet()) {
            if (!(entry.getValue() instanceof Map<?, ?> ranges)) {
                continue;
            }
            if (!(ranges.get("added") instanceof List<?> items)) {
                continue;
            }
            List<int[]> parsed = new ArrayList<>();
            for (Object item : items) {
                if (item instanceof List<?> pair && pair.size() >= 2) {
                    parsed.add(new int[] {toInt(pair.get(0)), toInt(pair.get(1))});
                }
            }
            result.put(entry.getKey(), parsed);
        }
        return result;
    }

    private static List<List<Integer>> intersect(List<int[]> changed, int start, int end) {
        List<List<Integer>> hits = new ArrayList<>();
        for (int[] range : changed) {
            int from = Math.max(range[0], start);
            int to = Math.min(range[1], end);
            if (from <= to) {
                hits.add(List.of(from, to));
            }
        }
        return hits;
    }

    private static int toInt(Object value) {
        return value instanceof Number number ? number.intValue() : 0;
    }

    /** 完整读快照里的 [from, to] 行（1 基闭区间）；读不到返回 null。 */
    private static String readLines(Path root, String relative, int from, int to) {
        Path base = root.normalize();
        Path file = base.resolve(relative).normalize();
        if (!file.startsWith(base)) {
            return null; // 越出快照根：不读（防护，正常不会发生）
        }
        try {
            List<String> lines = Files.readAllLines(file, StandardCharsets.UTF_8);
            if (from < 1 || from > lines.size()) {
                return null;
            }
            int last = Math.min(to, lines.size());
            StringBuilder text = new StringBuilder();
            for (int line = from; line <= last; line++) {
                text.append(lines.get(line - 1)).append('\n');
            }
            return text.toString();
        } catch (IOException ex) {
            return null;
        }
    }

    /** 取类型的简单名（含泛型实参与数组元素）：只做名字级匹配，不需要符号求解。 */
    private static Set<String> simpleTypeNames(Type type) {
        Set<String> names = new LinkedHashSet<>();
        if (type.isClassOrInterfaceType()) {
            ClassOrInterfaceType declared = type.asClassOrInterfaceType();
            names.add(declared.getNameAsString());
            declared.getTypeArguments()
                    .ifPresent(args -> args.forEach(arg -> names.addAll(simpleTypeNames(arg))));
        } else if (type.isArrayType()) {
            names.addAll(simpleTypeNames(type.asArrayType().getComponentType()));
        } else if (type.isWildcardType()) {
            type.asWildcardType()
                    .getExtendedType()
                    .ifPresent(bound -> names.addAll(simpleTypeNames(bound)));
        }
        return names;
    }

    /** 提取方法 @RequestBody 参数字段（字段名 + 类型 + 描述）。
     * 优先强类型（类定义字段 AST 提取）；动态类型（JSONObject/Map）或解析失败时，
     * 追踪本方法、同类 helper 与注入 Service 的有限调用链，提取直接/嵌套 getter。
     * 两者都拿不到返回 null，不阻断候选提取，但 parameter_evidence 仍保留透传证据。 */
    private static List<Map<String, Object>> extractRequestBodyFields(
            Path root, String relative, MethodDeclaration method, CompilationUnit unit,
            JavaParser parser,
            MethodIndex index, Map<String, String> injected) {
        for (Parameter param : method.getParameters()) {
            if (param.getAnnotationByName("RequestBody").isEmpty()) {
                continue;
            }
            List<Map<String, Object>> strong = extractStrongTypedFields(param, root);
            if (strong != null) {
                return strong;
            }
            List<Map<String, Object>> dynamic = extractDynamicFields(
                    root, relative, method, param, unit, parser, index, injected);
            if (dynamic != null) {
                return dynamic;
            }
        }
        return null;
    }

    /** 提取参数在方法体中的真实使用点，供接口参数语义解析使用。 */
    private static List<Map<String, Object>> extractParameterEvidence(MethodDeclaration method) {
        List<Map<String, Object>> result = new ArrayList<>();
        for (Parameter param : method.getParameters()) {
            Map<String, Object> evidence = new LinkedHashMap<>();
            String name = param.getNameAsString();
            evidence.put("name", name);
            evidence.put("type", param.getType().asString());

            List<String> annotations = param.getAnnotations().stream()
                    .map(AnnotationExpr::getNameAsString)
                    .toList();
            if (!annotations.isEmpty()) {
                evidence.put("annotations", annotations);
            }
            String location = parameterLocation(annotations);
            if (location != null) {
                evidence.put("in", location);
            }
            Boolean required = parameterRequired(param);
            if (required != null) {
                evidence.put("required", required);
            }
            evidence.put("required_evidence", parameterRequiredEvidence(param));

            List<Map<String, Object>> accesses = new ArrayList<>();
            Set<String> seenAccesses = new LinkedHashSet<>();
            CompilationUnit methodUnit = method.findCompilationUnit().orElse(null);
            Map<String, String> constants = methodUnit == null
                    ? Map.of() : stringConstants(methodUnit);
            for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
                var scope = call.getScope().orElse(null);
                if (scope instanceof NameExpr scoped && name.equals(scoped.getNameAsString())) {
                    Map<String, Object> access = new LinkedHashMap<>();
                    access.put("operation", call.getNameAsString());
                    access.put("arguments", call.getArguments().stream()
                            .map(Expression::toString).toList());
                    call.getBegin().ifPresent(begin -> access.put("line", begin.line));
                    if (!call.getArguments().isEmpty()
                            && call.getArgument(0).isStringLiteralExpr()) {
                        access.put("field", call.getArgument(0).asStringLiteralExpr().getValue());
                    } else if (call.getArguments().isEmpty()) {
                        String getterField = getterFieldName(call.getNameAsString());
                        if (getterField != null) {
                            access.put("field", getterField);
                        }
                    }
                    if ("getOrDefault".equals(call.getNameAsString())
                            && call.getArguments().size() > 1) {
                        access.put("default_expression", call.getArgument(1).toString());
                    }
                    String accessKey = access.toString();
                    if (seenAccesses.add(accessKey)) {
                        accesses.add(access);
                    }
                }
                for (FieldAccess fieldAccess : accessChain(call, name, constants)) {
                    Map<String, Object> access = new LinkedHashMap<>();
                    access.put("operation", fieldAccess.operation());
                    access.put("field", fieldAccess.path());
                    access.put("type", fieldAccess.type());
                    if (fieldAccess.defaultExpression() != null) {
                        access.put("default_expression", fieldAccess.defaultExpression());
                    }
                    if (fieldAccess.line() > 0) {
                        access.put("line", fieldAccess.line());
                    }
                    String accessKey = access.toString();
                    if (seenAccesses.add(accessKey)) {
                        accesses.add(access);
                    }
                }
                if (call.getArguments().stream().anyMatch(arg ->
                        arg.isNameExpr() && name.equals(arg.asNameExpr().getNameAsString()))) {
                    Map<String, Object> access = new LinkedHashMap<>();
                    access.put("operation", "forwarded");
                    access.put("call", call.getNameAsString());
                    access.put("arguments", call.getArguments().stream()
                            .map(Expression::toString).toList());
                    call.getBegin().ifPresent(begin -> access.put("line", begin.line));
                    String accessKey = access.toString();
                    if (seenAccesses.add(accessKey)) {
                        accesses.add(access);
                    }
                }
            }
            for (FieldAccessExpr field : method.findAll(FieldAccessExpr.class)) {
                if (!(field.getScope() instanceof NameExpr scoped)
                        || !name.equals(scoped.getNameAsString())) {
                    continue;
                }
                Map<String, Object> access = new LinkedHashMap<>();
                access.put("operation", "field_access");
                access.put("field", field.getNameAsString());
                field.getBegin().ifPresent(begin -> access.put("line", begin.line));
                String accessKey = access.toString();
                if (seenAccesses.add(accessKey)) {
                    accesses.add(access);
                }
            }
            if (!accesses.isEmpty()) {
                evidence.put("body_accesses", accesses);
            }

            List<Map<String, Object>> conditions = new ArrayList<>();
            for (IfStmt branch : method.findAll(IfStmt.class)) {
                String condition = branch.getCondition().toString();
                if (!condition.contains(name)) {
                    continue;
                }
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("condition", condition);
                branch.getBegin().ifPresent(begin -> item.put("line", begin.line));
                conditions.add(item);
            }
            if (!conditions.isEmpty()) {
                evidence.put("conditions", conditions);
            }
            result.add(evidence);
        }
        return result;
    }

    private static String getterFieldName(String methodName) {
        String suffix = null;
        if (methodName.startsWith("get") && methodName.length() > 3) {
            suffix = methodName.substring(3);
        } else if (methodName.startsWith("is") && methodName.length() > 2) {
            suffix = methodName.substring(2);
        }
        if (suffix == null || suffix.isEmpty()) {
            return null;
        }
        return Character.toLowerCase(suffix.charAt(0)) + suffix.substring(1);
    }

    /** 长方法完整原文之外，补充所有参数访问点的源码位置索引。 */
    private static List<Map<String, Object>> methodBodyContext(
            MethodDeclaration method, List<Map<String, Object>> evidence) {
        String source = method.toString();
        if (source.length() <= METHOD_CONTEXT_THRESHOLD) {
            return List.of();
        }
        String[] lines = source.split("\\R", -1);
        int methodStart = method.getBegin().map(position -> position.line).orElse(1);
        Set<Integer> evidenceLines = new LinkedHashSet<>();
        for (Map<String, Object> parameter : evidence) {
            for (String key : List.of("body_accesses", "conditions")) {
                for (Object item : asObjectList(parameter.get(key))) {
                    if (item instanceof Map<?, ?> map && map.get("line") instanceof Number line) {
                        evidenceLines.add(line.intValue());
                    }
                }
            }
        }
        if (evidenceLines.isEmpty()) {
            return List.of();
        }
        List<Integer> sorted = evidenceLines.stream().sorted().toList();
        List<Map<String, Object>> windows = new ArrayList<>();
        int previousEnd = -1;
        for (int absoluteLine : sorted) {
            int relative = absoluteLine - methodStart + 1;
            int from = Math.max(1, relative - 2);
            int to = Math.min(lines.length, relative + 2);
            if (from <= previousEnd) {
                continue;
            }
            StringBuilder text = new StringBuilder();
            for (int line = from; line <= to; line++) {
                text.append(lines[line - 1]).append('\n');
            }
            Map<String, Object> window = new LinkedHashMap<>();
            window.put("start_line", methodStart + from - 1);
            window.put("end_line", methodStart + to - 1);
            window.put("text", text.toString());
            windows.add(window);
            previousEnd = to;
        }
        return windows;
    }

    private static List<Object> asObjectList(Object value) {
        if (value instanceof List<?> list) {
            return new ArrayList<>(list);
        }
        return List.of();
    }

    private static String parameterRequiredEvidence(Parameter param) {
        for (AnnotationExpr annotation : param.getAnnotations()) {
            String annotationName = simpleAnnotationName(annotation.getNameAsString());
            if (Set.of("RequestBody", "RequestParam", "PathVariable", "RequestHeader")
                    .contains(annotationName)) {
                if (annotation instanceof NormalAnnotationExpr normal) {
                    for (MemberValuePair pair : normal.getPairs()) {
                        if ("required".equals(pair.getNameAsString())
                                && pair.getValue().isBooleanLiteralExpr()) {
                            return annotationName + ".required="
                                    + pair.getValue().asBooleanLiteralExpr().getValue();
                        }
                    }
                }
                return annotationName + " 默认 required=true";
            }
        }
        return "未发现参数位置注解的 required 声明";
    }

    private static String parameterLocation(List<String> annotations) {
        for (String annotation : annotations) {
            String simpleName = simpleAnnotationName(annotation);
            if ("RequestBody".equals(simpleName)) {
                return "body";
            }
            if ("RequestParam".equals(simpleName)) {
                return "query";
            }
            if ("PathVariable".equals(simpleName)) {
                return "path";
            }
            if ("RequestHeader".equals(simpleName)) {
                return "header";
            }
        }
        return null;
    }

    private static Boolean parameterRequired(Parameter param) {
        for (AnnotationExpr annotation : param.getAnnotations()) {
            String simpleName = simpleAnnotationName(annotation.getNameAsString());
            if (!Set.of("RequestBody", "RequestParam", "PathVariable", "RequestHeader")
                    .contains(simpleName)) {
                continue;
            }
            if (!(annotation instanceof NormalAnnotationExpr normal)) {
                return true;
            }
            for (MemberValuePair pair : normal.getPairs()) {
                if ("required".equals(pair.getNameAsString())
                        && pair.getValue().isBooleanLiteralExpr()) {
                    return pair.getValue().asBooleanLiteralExpr().getValue();
                }
            }
            return true;
        }
        return null;
    }

    /** 强类型 @RequestBody：解析类定义提取字段；解析失败返回 null（交由动态提取兜底）。 */
    private static List<Map<String, Object>> extractStrongTypedFields(Parameter param, Path root) {
        try {
            ResolvedType type = param.getType().resolve();
            if (!type.isReferenceType()) {
                return null;
            }
            ResolvedReferenceTypeDeclaration declaration =
                    type.asReferenceType().getTypeDeclaration().orElse(null);
            if (declaration == null) {
                return null;
            }
            Node node = declaration.toAst().orElse(null);
            if (!(node instanceof ClassOrInterfaceDeclaration)
                    && !(node instanceof RecordDeclaration)) {
                return null;
            }
            List<Map<String, Object>> fields = new ArrayList<>();
            collectTypeFields(
                    node, root, new LinkedHashSet<>(), new LinkedHashSet<>(), fields);
            return fields.isEmpty() ? null : fields;
        } catch (RuntimeException ex) {
            // 动态类型（如 JSONObject）无法解析，或类型不在当前仓库：交由动态提取兜底
            LOG.debug("强类型解析失败（转动态提取）：{}（{}）", param.getType().asString(),
                    ex.getMessage());
            return null;
        }
    }

    /** 提取 class/record 的自身字段，并递归补齐可解析的父类字段。 */
    private static void collectTypeFields(
            Node node, Path root, Set<String> visitedTypes, Set<String> seenFields,
            List<Map<String, Object>> fields) {
        if (node instanceof ClassOrInterfaceDeclaration cls) {
            String typeName = cls.getNameAsString();
            if (!visitedTypes.add(typeName)) {
                return;
            }
            for (FieldDeclaration field : cls.getFields()) {
                addFieldEntries(field, root, seenFields, fields);
            }
            for (ClassOrInterfaceType parent : cls.getExtendedTypes()) {
                resolveTypeAst(parent).ifPresent(parentNode ->
                        collectTypeFields(parentNode, root, visitedTypes, seenFields, fields));
            }
            return;
        }
        if (node instanceof RecordDeclaration record) {
            String typeName = record.getNameAsString();
            if (!visitedTypes.add(typeName)) {
                return;
            }
            for (Parameter component : record.getParameters()) {
                addParameterEntry(component, root, seenFields, fields);
            }
            record.getMembers().stream()
                    .filter(member -> member instanceof FieldDeclaration)
                    .map(member -> (FieldDeclaration) member)
                    .forEach(field -> addFieldEntries(field, root, seenFields, fields));
        }
    }

    private static Optional<Node> resolveTypeAst(ClassOrInterfaceType type) {
        try {
            ResolvedType resolved = type.resolve();
            if (!resolved.isReferenceType()) {
                return Optional.empty();
            }
            ResolvedReferenceTypeDeclaration declaration =
                    resolved.asReferenceType().getTypeDeclaration().orElse(null);
            return declaration == null ? Optional.empty() : declaration.toAst();
        } catch (RuntimeException ex) {
            return Optional.empty();
        }
    }

    private static void addFieldEntries(
            FieldDeclaration field, Path root, Set<String> seenFields,
            List<Map<String, Object>> fields) {
        if (field.isStatic()) {
            return;
        }
        String description = fieldDescription(field);
        for (VariableDeclarator variable : field.getVariables()) {
            if (!seenFields.add(variable.getNameAsString())) {
                continue;
            }
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("name", variable.getNameAsString());
            entry.put("type", variable.getType().asString());
            addRequirement(entry, fieldRequirement(field));
            addSourceRef(entry, field, root);
            if (!description.isEmpty()) {
                entry.put("description", description);
            }
            fields.add(entry);
        }
    }

    /** 优先保留 DTO 注解里的业务取值说明；普通注释作为兜底。只读取字面量，不推测表达式。 */
    private static String fieldDescription(FieldDeclaration field) {
        for (AnnotationExpr annotation : field.getAnnotations()) {
            String name = simpleAnnotationName(annotation.getNameAsString());
            if (!Set.of("ApiModelProperty", "Schema").contains(name)) {
                continue;
            }
            if (annotation instanceof SingleMemberAnnotationExpr single
                    && single.getMemberValue().isStringLiteralExpr()) {
                String value = single.getMemberValue().asStringLiteralExpr().asString().trim();
                if (!value.isEmpty()) {
                    return value;
                }
            }
            if (annotation instanceof NormalAnnotationExpr normal) {
                for (MemberValuePair pair : normal.getPairs()) {
                    if (!Set.of("value", "description").contains(pair.getNameAsString())
                            || !pair.getValue().isStringLiteralExpr()) {
                        continue;
                    }
                    String value = pair.getValue().asStringLiteralExpr().asString().trim();
                    if (!value.isEmpty()) {
                        return value;
                    }
                }
            }
        }
        return field.getJavadocComment()
                .map(comment -> comment.parse().getDescription().toText().trim())
                .orElse("");
    }

    private static void addParameterEntry(
            Parameter parameter, Path root, Set<String> seenFields, List<Map<String, Object>> fields) {
        if (!seenFields.add(parameter.getNameAsString())) {
            return;
        }
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("name", parameter.getNameAsString());
        entry.put("type", parameter.getType().asString());
        addRequirement(entry, parameterRequirement(parameter));
        addSourceRef(entry, parameter, root);
        fields.add(entry);
    }

    private static void addSourceRef(Map<String, Object> entry, Node node, Path root) {
        node.getBegin().ifPresent(begin -> {
            String file = "";
            Optional<Path> storagePath = node.findCompilationUnit()
                    .flatMap(unit -> unit.getStorage())
                    .map(storage -> storage.getPath().normalize());
            if (storagePath.isPresent() && storagePath.get().startsWith(root.normalize())) {
                file = normalizeRelative(root.normalize().relativize(storagePath.get()));
            }
            if (!file.isBlank()) {
                int endLine = node.getEnd().map(position -> position.line).orElse(begin.line);
                entry.put("source_refs", List.of(Map.of(
                        "file", file, "line", begin.line, "end_line", endLine)));
            }
        });
    }

    private record Requirement(Boolean required, String evidence) {
    }

    private static void addRequirement(Map<String, Object> entry, Requirement requirement) {
        if (requirement.required() != null) {
            entry.put("required", requirement.required());
        }
        if (requirement.evidence() != null) {
            entry.put("required_evidence", requirement.evidence());
        }
    }

    /** 动态类型 @RequestBody：有限调用图内追踪 body 形参，支持 Controller→Service→Helper。
     * 每层都重新绑定实际参数名，避免 service 使用 request/body 等不同名字时漏字段。 */
    private static List<Map<String, Object>> extractDynamicFields(
            Path root, String relative, MethodDeclaration method, Parameter param,
            CompilationUnit unit,
            JavaParser parser, MethodIndex index, Map<String, String> injected) {
        String paramName = param.getNameAsString();
        Map<String, String> constants = stringConstants(unit);
        List<Map<String, Object>> fields = new ArrayList<>();
        LinkedHashSet<String> seen = new LinkedHashSet<>();
        List<FieldTrace> queue = new ArrayList<>();
        queue.add(new FieldTrace(method, unit, relative, paramName, 0));
        Set<String> visited = new LinkedHashSet<>();
        for (int cursor = 0; cursor < queue.size(); cursor++) {
            FieldTrace trace = queue.get(cursor);
            int start = trace.method().getBegin().map(position -> position.line).orElse(1);
            String visitKey = trace.file() + ":" + start + ":" + trace.parameterName();
            if (!visited.add(visitKey)) {
                continue;
            }
            collectGetters(
                    trace.method(), trace.parameterName(), constants, trace.file(), fields, seen);
            if (trace.depth() >= MAX_FIELD_TRACE_DEPTH) {
                continue;
            }
            ClassOrInterfaceDeclaration owner = trace.method()
                    .findAncestor(ClassOrInterfaceDeclaration.class)
                    .orElse(null);
            Map<String, String> ownerInjected = owner == null ? injected : injectedTypes(owner);
            if (owner == null) {
                continue;
            }
            for (MethodCallExpr call : trace.method().findAll(MethodCallExpr.class)) {
                int argumentIndex = forwardedArgumentIndex(call, trace.parameterName());
                if (argumentIndex < 0) {
                    continue;
                }
                String ownerType = null;
                var scope = call.getScope().orElse(null);
                if (scope == null || scope instanceof ThisExpr) {
                    ownerType = owner.getNameAsString();
                } else if (scope instanceof NameExpr name) {
                    ownerType = ownerInjected.get(name.getNameAsString());
                }
                if (ownerType == null) {
                    continue;
                }
                MethodTarget target = resolveTarget(
                        ownerType, call.getNameAsString(), index, new ArrayList<>(),
                        new LinkedHashSet<>(), roleOf(ownerType));
                if (target == null || argumentIndex >= target.parameterNames().size()) {
                    continue;
                }
                MethodDeclaration targetMethod = methodForTarget(
                        root, trace.unit(), target, parser);
                if (targetMethod == null) {
                    continue;
                }
                CompilationUnit targetUnit = targetMethod.findCompilationUnit().orElse(trace.unit());
                Map<String, String> targetConstants = stringConstants(targetUnit);
                queue.add(new FieldTrace(
                        targetMethod,
                        targetUnit,
                        target.file(),
                        target.parameterNames().get(argumentIndex),
                        trace.depth() + 1));
                // targetConstants 通过下一轮 trace 的 CompilationUnit 重新计算；这里读取一次
                // 是为了显式保证跨文件目标已成功解析，不依赖共享常量表。
                if (targetConstants.isEmpty()) {
                    // no-op: empty constants are a valid target state
                }
            }
        }
        return fields.isEmpty() ? null : fields;
    }

    /** 通过参数转发定位调用参数下标，支持直接传参；复杂表达式交给 unresolved/LLM。 */
    private static int forwardedArgumentIndex(MethodCallExpr call, String parameterName) {
        for (int index = 0; index < call.getArguments().size(); index++) {
            Expression argument = call.getArgument(index);
            if (argument.isNameExpr()
                    && parameterName.equals(argument.asNameExpr().getNameAsString())) {
                return index;
            }
        }
        return -1;
    }

    /** 从索引位置重新解析被调方法；同文件目标复用已有 CompilationUnit。 */
    private static MethodDeclaration methodForTarget(
            Path root, CompilationUnit currentUnit, MethodTarget target, JavaParser parser) {
        if (target.file().equals(currentUnit.getStorage().map(storage ->
                normalizeRelative(root.relativize(storage.getPath()))).orElse(""))) {
            return findMethod(currentUnit, target);
        }
        Path path = root.resolve(target.file()).normalize();
        if (!path.startsWith(root.normalize())) {
            return null;
        }
        CompilationUnit unit = parseOrNull(path, parser);
        return unit == null ? null : findMethod(unit, target);
    }

    private static MethodDeclaration findMethod(CompilationUnit unit, MethodTarget target) {
        return unit.findAll(MethodDeclaration.class).stream()
                .filter(method -> method.getNameAsString().equals(target.methodName()))
                .filter(method -> method.getBegin().map(position -> position.line)
                        .orElse(-1) == target.startLine())
                .findFirst()
                .orElse(null);
    }

    /** 从单个方法体内提取对 param 的直接/嵌套 getter 字段，去重后追加到 fields。 */
    private static void collectGetters(
            MethodDeclaration method, String paramName, Map<String, String> constants,
            String file, List<Map<String, Object>> fields, LinkedHashSet<String> seen) {
        for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
            for (FieldAccess access : accessChain(call, paramName, constants)) {
                if (!seen.add(access.path())) {
                    continue;
                }
                Map<String, Object> entry = new LinkedHashMap<>();
                entry.put("name", access.path());
                entry.put("type", access.type());
                if ("getOrDefault".equals(access.operation())) {
                    entry.put("required", false);
                    entry.put("required_evidence", "getOrDefault 提供默认值");
                }
                if (!file.isBlank() && access.line() > 0) {
                    entry.put("source_refs", List.of(Map.of("file", file, "line", access.line())));
                }
                fields.add(entry);
            }
        }
    }

    /** 计算 param.get("a").getJSONObject("b").getString("c") 的完整路径。 */
    private static List<FieldAccess> accessChain(
            MethodCallExpr call, String paramName, Map<String, String> constants) {
        List<MethodCallExpr> chain = new ArrayList<>();
        MethodCallExpr current = call;
        while (true) {
            chain.add(current);
            var scope = current.getScope().orElse(null);
            if (scope instanceof MethodCallExpr nested) {
                current = nested;
                continue;
            }
            if (scope instanceof NameExpr name && paramName.equals(name.getNameAsString())) {
                break;
            }
            return List.of();
        }
        java.util.Collections.reverse(chain);
        List<FieldAccess> result = new ArrayList<>();
        String path = "";
        for (MethodCallExpr item : chain) {
            String type = JSON_GETTER_TYPE.get(item.getNameAsString());
            if (type == null || item.getArguments().isEmpty()) {
                continue;
            }
            String fieldName = stringArgument(item.getArgument(0), constants);
            if (fieldName == null || fieldName.isBlank()) {
                continue;
            }
            path = path.isEmpty() ? fieldName : path + "." + fieldName;
            String effectiveType = type;
            if ("getOrDefault".equals(item.getNameAsString()) && item.getArguments().size() > 1) {
                effectiveType = literalType(item.getArgument(1), type);
            }
            String defaultExpression = null;
            if ("getOrDefault".equals(item.getNameAsString()) && item.getArguments().size() > 1) {
                defaultExpression = item.getArgument(1).toString();
            }
            int line = item.getBegin().map(position -> position.line).orElse(0);
            result.add(new FieldAccess(
                    path, effectiveType, item.getNameAsString(), defaultExpression, line));
        }
        return result;
    }

    private static String stringArgument(Expression expression, Map<String, String> constants) {
        if (expression.isStringLiteralExpr()) {
            return expression.asStringLiteralExpr().getValue();
        }
        if (expression.isNameExpr()) {
            return constants.get(expression.asNameExpr().getNameAsString());
        }
        return null;
    }

    private static String literalType(Expression expression, String fallback) {
        if (expression.isStringLiteralExpr()) {
            return "String";
        }
        if (expression.isBooleanLiteralExpr()) {
            return "Boolean";
        }
        if (expression.isIntegerLiteralExpr()) {
            return "Integer";
        }
        if (expression.isDoubleLiteralExpr()) {
            return "Double";
        }
        return fallback;
    }

    /** DTO 字段必填性：只对源码明确声明的约束下结论，未标注时保持未知。 */
    private static Requirement fieldRequirement(FieldDeclaration field) {
        for (AnnotationExpr annotation : field.getAnnotations()) {
            String name = simpleAnnotationName(annotation.getNameAsString());
            if (Set.of("NotNull", "NotBlank", "NotEmpty", "NonNull").contains(name)) {
                return new Requirement(true, "@" + name);
            }
            if ("Nullable".equals(name)) {
                return new Requirement(false, "@Nullable");
            }
            if ("JsonProperty".equals(name) && annotation instanceof NormalAnnotationExpr normal) {
                for (MemberValuePair pair : normal.getPairs()) {
                    if ("required".equals(pair.getNameAsString())
                            && pair.getValue().isBooleanLiteralExpr()) {
                        boolean required = pair.getValue().asBooleanLiteralExpr().getValue();
                        return new Requirement(
                                required, "@JsonProperty(required=" + required + ")");
                    }
                }
            }
        }
        return new Requirement(null, null);
    }

    private static Requirement parameterRequirement(Parameter parameter) {
        for (AnnotationExpr annotation : parameter.getAnnotations()) {
            String name = simpleAnnotationName(annotation.getNameAsString());
            if (Set.of("NotNull", "NotBlank", "NotEmpty", "NonNull").contains(name)) {
                return new Requirement(true, "@" + name);
            }
            if ("Nullable".equals(name)) {
                return new Requirement(false, "@Nullable");
            }
        }
        return new Requirement(null, null);
    }

    private static String simpleAnnotationName(String name) {
        int separator = name.lastIndexOf('.');
        return separator >= 0 ? name.substring(separator + 1) : name;
    }

    /** 收集同文件内 static final String 常量（供动态字段常量引用解析，如 param.getX(ACCOUNT_NO)）。 */
    private static Map<String, String> stringConstants(CompilationUnit unit) {
        Map<String, String> constants = new LinkedHashMap<>();
        for (FieldDeclaration field : unit.findAll(FieldDeclaration.class)) {
            if (!field.isStatic() || !field.isFinal()) {
                continue;
            }
            for (VariableDeclarator var : field.getVariables()) {
                var init = var.getInitializer().orElse(null);
                if (init != null && init.isStringLiteralExpr()) {
                    constants.put(var.getNameAsString(), init.asStringLiteralExpr().getValue());
                }
            }
        }
        return constants;
    }

    private static String joinPath(String prefix, String path) {
        if (prefix.isEmpty()) {
            return path.isEmpty() ? "/" : path;
        }
        if (path.isEmpty()) {
            return prefix;
        }
        return prefix.endsWith("/") ? prefix + path.substring(1) : prefix + path;
    }

    private static boolean inScope(Path relative, List<String> includes) {
        if (includes.isEmpty()) {
            return true;
        }
        // 规范化后比较：includes 是用户配置里的字符串（正斜杠语义），而 Path 在 Windows
        // 上按反斜杠切分——Path.startsWith("a/b") 会把 "a/b" 当成单个段而永不命中。
        String normalized = normalizeRelative(relative);
        for (String include : includes) {
            if (normalized.startsWith(include.replace('\\', '/'))) {
                return true;
            }
        }
        return false;
    }

    /** 按注解名取方法上的全部匹配（NodeWithAnnotations 无多值查询，自行过滤）。 */
    private static List<AnnotationExpr> annotationsNamed(MethodDeclaration method, String name) {
        List<AnnotationExpr> matched = new ArrayList<>();
        for (AnnotationExpr ann : method.getAnnotations()) {
            if (name.equals(ann.getNameAsString())) {
                matched.add(ann);
            }
        }
        return matched;
    }

    /** 提取注解单个字符串参数（@GetMapping("/x") / @GetMapping(value = "/x")）。 */
    private static Optional<String> annotationPath(AnnotationExpr ann) {
        List<String> paths = annotationPaths(ann);
        return paths.isEmpty() ? Optional.empty() : Optional.of(paths.get(0));
    }

    /** 提取注解全部字符串路径参数（数组形式逐个产出候选）。 */
    private static List<String> annotationPaths(AnnotationExpr ann) {
        if (ann instanceof MarkerAnnotationExpr) {
            // 裸注解（@GetMapping 无成员）：Spring 语义 = 映射到类级前缀
            return List.of("");
        }
        if (ann instanceof SingleMemberAnnotationExpr single) {
            return stringArgs(single.getMemberValue());
        }
        if (ann instanceof NormalAnnotationExpr normal) {
            for (MemberValuePair pair : normal.getPairs()) {
                if ("value".equals(pair.getNameAsString()) || "path".equals(pair.getNameAsString())) {
                    return stringArgs(pair.getValue());
                }
            }
            // 有成员但无路径（如仅 consumes/produces）：同样映射到类级前缀
            return List.of("");
        }
        return List.of();
    }

    private static List<String> stringArgs(Expression expr) {
        List<String> values = new ArrayList<>();
        if (expr instanceof StringLiteralExpr literal) {
            values.add(literal.getValue());
        } else if (expr instanceof ArrayInitializerExpr array) {
            for (Expression item : array.getValues()) {
                values.addAll(stringArgs(item));
            }
        }
        return values;
    }

    /** method 层 @RequestMapping 的 HTTP 方法属性；缺省时 Spring 语义为所有常见 HTTP 方法。 */
    private static List<String> requestMappingMethods(AnnotationExpr ann) {
        if (ann instanceof NormalAnnotationExpr normal) {
            for (MemberValuePair pair : normal.getPairs()) {
                if ("method".equals(pair.getNameAsString())) {
                    List<String> methods = new ArrayList<>();
                    collectRequestMethods(pair.getValue(), methods);
                    if (!methods.isEmpty()) {
                        return methods;
                    }
                }
            }
        }
        return List.of("GET", "POST", "PUT", "DELETE", "PATCH");
    }

    private static void collectRequestMethods(Expression expression, List<String> methods) {
        if (expression instanceof FieldAccessExpr field) {
            methods.add(field.getNameAsString());
        } else if (expression instanceof ArrayInitializerExpr array) {
            for (Expression value : array.getValues()) {
                collectRequestMethods(value, methods);
            }
        }
    }
}
