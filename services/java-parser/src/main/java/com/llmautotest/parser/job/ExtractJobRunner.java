// 对应契约 ExtractJob：静态特征定位接口候选（R-REV-02 第①步，供 orchestrator LLM 语义解析）。
// 当前实现 Spring 注解 REST 识别（@RestController + @XxxMapping）；其余协议 Resolver
// 未接入时如实告警（不虚构候选）——协议插件化在第二个协议落地时经 ProtocolResolver 接入。
package com.llmautotest.parser.job;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseProblemException;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
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
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.github.javaparser.ast.expr.ThisExpr;
import com.github.javaparser.resolution.declarations.ResolvedReferenceTypeDeclaration;
import com.github.javaparser.resolution.types.ResolvedType;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;
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
import java.util.stream.Stream;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/** 候选提取 job 执行器：JavaParser 语法树 + Spring 注解静态定位。 */
@Component
public class ExtractJobRunner {

    private static final Logger LOG = LoggerFactory.getLogger(ExtractJobRunner.class);

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

    /** snippet 截断上限（供 LLM 上下文，防止超大方法撑爆请求）。 */
    private static final int SNIPPET_MAX_CHARS = 1500;

    /** R-REV-02 候选数底线（低于则告警提示换兜底仓库，X-REV-04）。 */
    private static final int MIN_CANDIDATES = 5;

    private static final Map<String, String> MAPPING_ANNOTATIONS = Map.of(
            "GetMapping", "GET",
            "PostMapping", "POST",
            "PutMapping", "PUT",
            "DeleteMapping", "DELETE",
            "PatchMapping", "PATCH");

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
            Map.entry("get", "unknown"));

    /** 执行提取，返回契约 ExtractJob 产物载荷（candidates + warnings）。 */
    public Map<String, Object> run(ExtractRequest request) throws IOException {
        List<Map<String, Object>> candidates = new ArrayList<>();
        List<String> warnings = new ArrayList<>();

        for (RepositoryInput repo : request.repositories()) {
            Path root = Path.of(repo.path());
            if (!Files.isDirectory(root)) {
                // 仓库级缺失：告警 + 跳过该仓库（提取阶段不终止整体，由调用方决策）
                warnings.add("仓库 " + repo.name() + " 路径不存在：" + repo.path() + "，跳过候选提取");
                continue;
            }
            extractRepository(root, repo, candidates, warnings);
        }

        if (request.protocols() != null && !request.protocols().isEmpty()) {
            // 如实声明能力边界：请求了当前未接入的协议 → 告警（不虚构候选）
            for (String protocol : request.protocols()) {
                if (!"rest".equals(protocol)) {
                    warnings.add("协议 " + protocol + " 的静态识别未接入（当前仅支持 rest），相关候选缺失");
                }
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

    private void extractRepository(
            Path root, RepositoryInput repo,
            List<Map<String, Object>> candidates, List<String> warnings) throws IOException {
        JavaParser parser = parserFor(root);
        List<Path> javaFiles;
        try (Stream<Path> stream = Files.walk(root)) {
            javaFiles = stream
                    .filter(Files::isRegularFile)
                    .filter(p -> p.getFileName().toString().endsWith(".java"))
                    .filter(p -> inScope(root.relativize(p), repo.includes()))
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        }
        for (Path file : javaFiles) {
            String relative = root.relativize(file).toString();
            try {
                String source = Files.readString(file, StandardCharsets.UTF_8);
                extractFile(source, repo, relative, candidates, parser);
            } catch (ParseProblemException ex) {
                // X-REV-01：单文件解析失败记录跳过 + 原因，不中断整体
                warnings.add("X-REV-01：" + relative + " 解析失败跳过：" + ex.getMessage());
            } catch (IOException ex) {
                warnings.add("X-REV-01：" + relative + " 读取失败跳过：" + ex.getMessage());
            }
        }
    }

    private void extractFile(
            String source, RepositoryInput repo, String relative,
            List<Map<String, Object>> candidates, JavaParser parser) {
        ParseResult<CompilationUnit> result = parser.parse(source);
        if (!result.isSuccessful() || result.getResult().isEmpty()) {
            // 解析失败路径：转交外层 catch 统一记 X-REV-01 告警（单文件跳过不中断）
            throw new ParseProblemException(result.getProblems());
        }
        CompilationUnit unit = result.getResult().get();
        for (ClassOrInterfaceDeclaration cls : unit.findAll(ClassOrInterfaceDeclaration.class)) {
            Optional<AnnotationExpr> restController = cls.getAnnotationByName("RestController");
            if (restController.isEmpty()) {
                continue;
            }
            // 类级 @RequestMapping 前缀（方法路径拼接基础）
            Optional<AnnotationExpr> classMapping = cls.getAnnotationByName("RequestMapping");
            String prefix = classMapping.flatMap(ExtractJobRunner::annotationPath).orElse("");
            for (MethodDeclaration method : cls.getMethods()) {
                for (Map.Entry<String, String> mapping : MAPPING_ANNOTATIONS.entrySet()) {
                    for (AnnotationExpr ann : annotationsNamed(method, mapping.getKey())) {
                        for (String path : annotationPaths(ann)) {
                            candidates.add(candidate(repo, relative, method, mapping.getValue(),
                                    joinPath(prefix, path), unit));
                        }
                    }
                }
                for (AnnotationExpr ann : annotationsNamed(method, "RequestMapping")) {
                    String httpMethod = requestMappingMethod(ann);
                    if (httpMethod != null) {
                        for (String path : annotationPaths(ann)) {
                            candidates.add(candidate(repo, relative, method, httpMethod,
                                    joinPath(prefix, path), unit));
                        }
                    }
                }
            }
        }
    }

    private static Map<String, Object> candidate(
            RepositoryInput repo, String relative, MethodDeclaration method,
            String httpMethod, String signature, CompilationUnit unit) {
        Map<String, Object> location = new LinkedHashMap<>();
        location.put("repo", repo.name());
        location.put("file", relative);
        location.put("start_line", method.getBegin().map(p -> p.line).orElse(1));
        location.put("end_line", method.getEnd().map(p -> p.line).orElse(1));

        String snippet = method.toString();
        if (snippet.length() > SNIPPET_MAX_CHARS) {
            snippet = snippet.substring(0, SNIPPET_MAX_CHARS) + "\n// ...（截断）";
        }
        if (LOG.isDebugEnabled()) {
            LOG.debug("候选定位：{} {} ({}:{})", httpMethod, signature, relative,
                    location.get("start_line"));
        }

        Map<String, Object> candidate = new LinkedHashMap<>();
        candidate.put("protocol", "rest");
        candidate.put("signature", httpMethod + " " + signature);
        candidate.put("code_location", location);
        candidate.put("snippet", snippet);
        // D5 分列标记随候选透传（兜底演示仓库不计入真实结论）
        candidate.put("is_fallback", repo.fallback());
        candidate.put("confidence", STATIC_CONFIDENCE);
        List<Map<String, Object>> requestBody = extractRequestBodyFields(method, unit);
        if (requestBody != null) {
            candidate.put("request_body", requestBody);
        }
        return candidate;
    }

    /** 提取方法 @RequestBody 参数字段（字段名 + 类型 + 描述）。
     * 优先强类型（类定义字段 AST 提取）；动态类型（JSONObject/Map）或解析失败时，
     * 退化为方法体内对 param 的 getter 访问点提取（param.getString("x") 等）。
     * 两者都拿不到（如透传给 service）返回 null，不阻断候选提取。 */
    private static List<Map<String, Object>> extractRequestBodyFields(
            MethodDeclaration method, CompilationUnit unit) {
        for (Parameter param : method.getParameters()) {
            if (param.getAnnotationByName("RequestBody").isEmpty()) {
                continue;
            }
            List<Map<String, Object>> strong = extractStrongTypedFields(param);
            if (strong != null) {
                return strong;
            }
            List<Map<String, Object>> dynamic = extractDynamicFields(method, param, unit);
            if (dynamic != null) {
                return dynamic;
            }
        }
        return null;
    }

    /** 强类型 @RequestBody：解析类定义提取字段；解析失败返回 null（交由动态提取兜底）。 */
    private static List<Map<String, Object>> extractStrongTypedFields(Parameter param) {
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
            var node = declaration.toAst().orElse(null);
            if (!(node instanceof ClassOrInterfaceDeclaration cls)) {
                return null;
            }
            List<Map<String, Object>> fields = new ArrayList<>();
            for (FieldDeclaration field : cls.getFields()) {
                String javadoc = field.getJavadocComment()
                        .map(comment -> comment.parse().getDescription().toText().trim())
                        .orElse("");
                for (VariableDeclarator var : field.getVariables()) {
                    Map<String, Object> entry = new LinkedHashMap<>();
                    entry.put("name", var.getNameAsString());
                    entry.put("type", var.getType().asString());
                    if (!javadoc.isEmpty()) {
                        entry.put("description", javadoc);
                    }
                    fields.add(entry);
                }
            }
            return fields.isEmpty() ? null : fields;
        } catch (RuntimeException ex) {
            // 动态类型（如 JSONObject）无法解析，或类型不在当前仓库：交由动态提取兜底
            LOG.debug("强类型解析失败（转动态提取）：{}（{}）", param.getType().asString(),
                    ex.getMessage());
            return null;
        }
    }

    /** 动态类型 @RequestBody：分析本方法及本方法体内透传调用的本类方法，
     * 对 param 的 getter 调用点提取字符串字段名；字符串为常量引用时解析同文件
     * static final 常量；类型按 getter 方法名推断。
     * 透传给跨类 service（无法在本编译单元定位实现）时不再深入。 */
    private static List<Map<String, Object>> extractDynamicFields(
            MethodDeclaration method, Parameter param, CompilationUnit unit) {
        String paramName = param.getNameAsString();
        Map<String, String> constants = stringConstants(unit);
        List<Map<String, Object>> fields = new ArrayList<>();
        LinkedHashSet<String> seen = new LinkedHashSet<>();
        for (MethodDeclaration target : methodsToScan(method, unit)) {
            collectGetters(target, paramName, constants, fields, seen);
        }
        return fields.isEmpty() ? null : fields;
    }

    /** 收集需要扫描 getter 的方法：本方法 + 本方法体内透传调用的本类方法。 */
    private static List<MethodDeclaration> methodsToScan(
            MethodDeclaration method, CompilationUnit unit) {
        List<MethodDeclaration> result = new ArrayList<>();
        result.add(method);
        // 仅 method 所在类的本类方法名 → 方法
        Map<String, MethodDeclaration> classMethods = new LinkedHashMap<>();
        for (ClassOrInterfaceDeclaration cls : unit.findAll(ClassOrInterfaceDeclaration.class)) {
            if (!cls.isAncestorOf(method)) {
                continue;
            }
            for (MethodDeclaration m : cls.getMethods()) {
                classMethods.put(m.getNameAsString(), m);
            }
        }
        // 方法体内无 scope / this scope 的调用 → 本类方法，纳入扫描
        for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
            var scope = call.getScope().orElse(null);
            if (scope == null || scope instanceof ThisExpr) {
                MethodDeclaration target = classMethods.get(call.getNameAsString());
                if (target != null && !result.contains(target)) {
                    result.add(target);
                }
            }
        }
        return result;
    }

    /** 从单个方法体内提取对 param 的 getter 字段，去重后追加到 fields。 */
    private static void collectGetters(
            MethodDeclaration method, String paramName, Map<String, String> constants,
            List<Map<String, Object>> fields, LinkedHashSet<String> seen) {
        for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
            var scope = call.getScope().orElse(null);
            if (!(scope instanceof NameExpr name) || !name.getNameAsString().equals(paramName)) {
                continue;
            }
            String type = JSON_GETTER_TYPE.get(call.getNameAsString());
            if (type == null) {
                continue;
            }
            if (call.getArguments().isEmpty()) {
                continue;
            }
            Expression arg = call.getArgument(0);
            String fieldName = null;
            if (arg.isStringLiteralExpr()) {
                fieldName = arg.asStringLiteralExpr().getValue();
            } else if (arg.isNameExpr()) {
                fieldName = constants.get(arg.asNameExpr().getNameAsString());
            }
            if (fieldName == null || fieldName.isEmpty() || !seen.add(fieldName)) {
                continue;
            }
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("name", fieldName);
            entry.put("type", type);
            fields.add(entry);
        }
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
        for (String include : includes) {
            if (relative.startsWith(include)) {
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

    /** method 层 @RequestMapping 的 HTTP 方法属性（RequestMethod.GET → GET；缺省 GET）。 */
    private static String requestMappingMethod(AnnotationExpr ann) {
        if (ann instanceof NormalAnnotationExpr normal) {
            for (MemberValuePair pair : normal.getPairs()) {
                if ("method".equals(pair.getNameAsString())
                        && pair.getValue() instanceof FieldAccessExpr field) {
                    return field.getNameAsString();
                }
            }
        }
        return "GET"; // Spring 默认缺省方法为 GET
    }
}
