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
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.springframework.stereotype.Component;

/** 扫描 job 执行器：目录遍历 + 文件计数 + 编码探测（不做语法分析）。 */
@Component
public class ScanJobRunner {

    private final ParserProperties properties;

    public ScanJobRunner(ParserProperties properties) {
        this.properties = properties;
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
        entry.put("is_fallback", repo.fallback());
        Path root = Path.of(repo.path());
        if (!Files.isDirectory(root)) {
            // X-CODE-01：仓库级硬错误（调用方按契约判失败）
            entry.put("status", "error");
            entry.put("file_count", 0);
            entry.put("error", Map.of(
                    "code", "REPO_PATH_NOT_FOUND",
                    "message", "仓库路径不存在或不是目录"));
            return entry;
        }

        List<Path> javaFiles = listJavaFiles(root, repo.includes());
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

    private List<Path> listJavaFiles(Path root, List<String> includes) throws IOException {
        try (Stream<Path> stream = Files.walk(root)) {
            return stream
                    .filter(Files::isRegularFile)
                    .filter(p -> p.getFileName().toString().endsWith(".java"))
                    .filter(p -> inScope(root.relativize(p), includes))
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        }
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

    private static Map<String, Object> sourceFileEntry(String repo, Path relative) {
        // F-CODE-03：模块取 Maven 结构第一层（无模块结构时取顶层目录名；根下文件记仓库名）
        String module = relative.getNameCount() > 1
                ? relative.getName(0).toString()
                : repo;
        return Map.of(
                "repo", repo,
                "module", module,
                "path", relative.toString());
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
