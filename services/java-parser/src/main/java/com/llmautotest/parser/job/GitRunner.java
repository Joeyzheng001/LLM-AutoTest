// 统一的 git 子进程执行器（ScanJobRunner 与 ExtractJobRunner 共用的唯一入口）。
package com.llmautotest.parser.job;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/** git 子进程执行：超时可配、并发排空管道、失败消息带命令名。 */
public class GitRunner {

    /** 默认墙钟上限（秒）；生产由 {@code parser.git-timeout-seconds} 覆盖。 */
    public static final int DEFAULT_TIMEOUT_SECONDS = 30;

    private final int timeoutSeconds;

    /** 非正数回落默认值（配置文件未提供该键时 Spring 绑出 0，不能让超时退化成"立即超时"）。 */
    public GitRunner(int timeoutSeconds) {
        this.timeoutSeconds = timeoutSeconds > 0 ? timeoutSeconds : DEFAULT_TIMEOUT_SECONDS;
    }

    /** 生效的墙钟上限（秒）；供运维核对配置是否被读到。 */
    public int timeoutSeconds() {
        return timeoutSeconds;
    }

    /**
     * 在 {@code cwd} 下执行 git（返回 stdout；非 0 退出码抛 IOException）。
     *
     * <p>统一带 {@code -c core.quotepath=false}：否则含非 ASCII 的路径会被输出成
     * {@code "a/\344\270\255..."} 这种加引号的转义形式，与源码里用到的路径对不上 → 静默漏。
     */
    public String run(Path cwd, String... args) throws IOException, InterruptedException {
        List<String> command = new ArrayList<>();
        command.add("git");
        command.add("-c");
        command.add("core.quotepath=false");
        command.add("-C");
        command.add(cwd.toString());
        command.addAll(List.of(args));
        return exec(command, String.join(" ", args));
    }

    /**
     * 执行命令并返回 stdout；非 0 退出码抛 IOException（stderr 作消息）。
     *
     * <p>**必须并发排空两条管道**：{@code git diff --unified=0} 的输出可达数百 KB，超过管道
     * 缓冲（约 64KB）后子进程会阻塞在写、父进程若先 {@code waitFor} 就会一直等到超时。
     * 此前只用 {@code --name-status}（输出小、装得下缓冲）所以没暴露这个问题。
     *
     * <p>超时消息带 {@code label}（git 子命令 + ref）：{@code timeoutSeconds} 是**所有** git
     * 命令共用的上限，不说是哪条命令超时就没法诊断。
     */
    public String exec(List<String> command, String label) throws IOException, InterruptedException {
        Process process = new ProcessBuilder(command).start();
        CompletableFuture<String> stdout = drain(process.getInputStream());
        CompletableFuture<String> stderr = drain(process.getErrorStream());
        if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
            process.destroyForcibly();
            throw new IOException(
                    "git 超时（>" + timeoutSeconds + "s）：git " + label
                            + "（超时可用 parser.git-timeout-seconds 调大）");
        }
        String out = stdout.join();
        String err = stderr.join();
        if (process.exitValue() != 0) {
            throw new IOException(err.trim());
        }
        return out;
    }

    private static CompletableFuture<String> drain(InputStream stream) {
        return CompletableFuture.supplyAsync(() -> {
            try (stream) {
                return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
            } catch (IOException ex) {
                return "";
            }
        });
    }
}
