// 对应契约 ExtractJob：静态特征定位接口候选（R-REV-02 第①步，供 orchestrator LLM 语义解析）。
// 当前实现 Spring 注解 REST 识别（@RestController + @XxxMapping）；其余协议 Resolver
// 未接入时如实告警（不虚构候选）——协议插件化在第二个协议落地时经 ProtocolResolver 接入。
package com.llmautotest.parser.job;

import com.github.javaparser.ParseProblemException;
import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.ArrayInitializerExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.MemberValuePair;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.llmautotest.parser.contract.ExtractRequest;
import com.llmautotest.parser.contract.RepositoryInput;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
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
                extractFile(source, repo, relative, candidates);
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
            List<Map<String, Object>> candidates) {
        CompilationUnit unit = StaticJavaParser.parse(source);
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
                                    joinPath(prefix, path)));
                        }
                    }
                }
                for (AnnotationExpr ann : annotationsNamed(method, "RequestMapping")) {
                    String httpMethod = requestMappingMethod(ann);
                    if (httpMethod != null) {
                        for (String path : annotationPaths(ann)) {
                            candidates.add(candidate(repo, relative, method, httpMethod,
                                    joinPath(prefix, path)));
                        }
                    }
                }
            }
        }
    }

    private static Map<String, Object> candidate(
            RepositoryInput repo, String relative, MethodDeclaration method,
            String httpMethod, String signature) {
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
        return candidate;
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
        if (ann instanceof SingleMemberAnnotationExpr single) {
            return stringArgs(single.getMemberValue());
        }
        if (ann instanceof NormalAnnotationExpr normal) {
            for (MemberValuePair pair : normal.getPairs()) {
                if ("value".equals(pair.getNameAsString()) || "path".equals(pair.getNameAsString())) {
                    return stringArgs(pair.getValue());
                }
            }
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
