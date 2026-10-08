package com.llmautotest.parser.job;

import com.llmautotest.parser.contract.RepositoryInput;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.stream.Stream;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 仓库只读快照（{@code git worktree --detach}）的建立与清理。
 *
 * <p>为什么必须共用同一份实现：scan 与 extract 都要按 {@code target_sha} 数同一版本的
 * 文件。若一边扫工作区、一边扫快照，两边文件数天然可能不等（工作区领先于配置提交时），
 * 下游的扫描完整性门禁会把它误判成「接口成批缺失」而硬拦整条流水线——实测
 * risk-icm-riskwkb-service 出现 611（快照）vs 613（工作区）的误报即此因。
 *
 * <p>为什么用 worktree 而不是 {@code git archive}：archive 尊重 {@code export-ignore}，
 * 可能静默丢文件；worktree 与工作区同构且可复用。
 *
 * <p>目录建在仓库的**兄弟目录**（不写进仓库，避免污染 git status）；同名同提交直接
 * 复用，提交变化时清理旧目录，避免快照无限堆积。
 */
public final class RepoSandbox {

    private static final Logger LOG = LoggerFactory.getLogger(RepoSandbox.class);

    /** 快照目录名前缀（建在仓库的**兄弟目录**，不污染仓库工作区与 git status）。 */
    private static final String SNAPSHOT_PREFIX = ".llm-autotest-snapshot-";

    /** 快照创建的进程内互斥：同一 (仓库, 提交) 并发建 worktree 会互相打架。 */
    private static final Object SNAPSHOT_LOCK = new Object();

    private RepoSandbox() {
    }

    /**
     * 按 {@code targetSha} 检出只读快照并返回其路径；已存在同提交快照则直接复用。
     *
     * @throws IOException 建 worktree 失败（调用方如实标记 degraded，不静默回退工作区）
     * @throws InterruptedException worktree 命令被中断（调用方需恢复中断标志）
     */
    public static Path at(GitRunner git, Path workspace, RepositoryInput repo, String targetSha)
            throws IOException, InterruptedException {
        String shortSha = targetSha.length() > 12 ? targetSha.substring(0, 12) : targetSha;
        String workspaceKey = workspaceKey(workspace);
        Path dir = workspace.resolveSibling(snapshotPrefix(repo, workspaceKey) + shortSha);
        synchronized (SNAPSHOT_LOCK) {
            if (!isSnapshotAt(git, dir, targetSha)) {
                if (Files.exists(dir)) {
                    removeWorktree(git, workspace, dir);
                }
                git.run(workspace, "worktree", "add", "--detach", dir.toString(), targetSha);
            }
            pruneStaleSnapshots(git, workspace, repo, workspaceKey, dir);
        }
        return dir;
    }

    /** 目录是否已是该提交的快照（worktree 的 .git 是文件，不能用 isDirectory 判）。 */
    private static boolean isSnapshotAt(GitRunner git, Path dir, String targetSha) {
        if (!Files.exists(dir.resolve(".git"))) {
            return false;
        }
        try {
            return targetSha.equals(git.run(dir, "rev-parse", "HEAD").trim());
        } catch (IOException | InterruptedException ex) {
            if (ex instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            return false;
        }
    }

    /** 移除 worktree（先走 git worktree remove，失败再退回 prune + 目录删除，尽力而为）。 */
    private static void removeWorktree(GitRunner git, Path workspace, Path dir) {
        try {
            git.run(workspace, "worktree", "remove", "--force", dir.toString());
            return;
        } catch (IOException | InterruptedException ex) {
            if (ex instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            LOG.debug("worktree remove 失败，退回 prune：{}", reasonOf(ex));
        }
        try {
            git.run(workspace, "worktree", "prune");
        } catch (IOException | InterruptedException ex) {
            if (ex instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            LOG.debug("worktree prune 失败：{}", reasonOf(ex));
        }
        try (Stream<Path> stream = Files.walk(dir)) {
            stream.sorted(Comparator.reverseOrder()).forEach(path -> {
                try {
                    Files.deleteIfExists(path);
                } catch (IOException ignored) {
                    // 清理失败不阻断本次提取：下次会用 --force 再试
                }
            });
        } catch (IOException ignored) {
            // 同上
        }
    }

    /** 清理同一仓库其它提交的快照目录（避免每次换提交都留一份完整源码副本）。 */
    private static void pruneStaleSnapshots(
            GitRunner git, Path workspace, RepositoryInput repo, String workspaceKey, Path keep) {
        Path parent = workspace.getParent();
        if (parent == null) {
            return;
        }
        String prefix = snapshotPrefix(repo, workspaceKey);
        try (Stream<Path> stream = Files.list(parent)) {
            stream.filter(Files::isDirectory)
                    .filter(path -> path.getFileName().toString().startsWith(prefix))
                    .filter(path -> !path.equals(keep))
                    .forEach(path -> removeWorktree(git, workspace, path));
        } catch (IOException ex) {
            LOG.debug("清理旧快照失败：{}", reasonOf(ex));
        }
    }

    /** 仓库名 → 可用于目录名的安全形式（只保留字母数字与 . _ -）。 */
    private static String safeName(String name) {
        String safe = name == null ? "" : name.replaceAll("[^A-Za-z0-9._-]", "_");
        return safe.isEmpty() ? "repo" : safe;
    }

    /** 快照目录必须同时绑定仓库真实路径，避免不同路径下同名仓库复用/清理彼此的 worktree。 */
    private static String snapshotPrefix(RepositoryInput repo, String workspaceKey) {
        return SNAPSHOT_PREFIX + safeName(repo.name()) + "-" + workspaceKey + "-";
    }

    private static String workspaceKey(Path workspace) {
        String value;
        try {
            value = workspace.toRealPath().toString();
        } catch (IOException ex) {
            value = workspace.toAbsolutePath().normalize().toString();
        }
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest, 0, 8);
        } catch (NoSuchAlgorithmException impossible) {
            // SHA-256 是 Java 标准实现；仅为极端运行时保留确定性回退。
            return Integer.toUnsignedString(value.hashCode(), 16);
        }
    }

    private static String reasonOf(Exception ex) {
        return ex.getMessage() == null ? ex.getClass().getSimpleName() : ex.getMessage();
    }
}
