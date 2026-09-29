// GitRunner 单测：超时可配 + 超时消息可诊断（评审指出的两个真问题）。
package com.llmautotest.parser.job;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.llmautotest.parser.ParserProperties;
import java.io.IOException;
import java.time.Duration;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledOnOs;
import org.junit.jupiter.api.condition.OS;

@EnabledOnOs({OS.LINUX, OS.MAC})
class GitRunnerTest {

    @Test
    void timeoutMessageNamesTheCommandAndTheKnob() {
        GitRunner runner = new GitRunner(1);

        long started = System.nanoTime();
        assertThatThrownBy(() -> runner.exec(List.of("/bin/sh", "-c", "sleep 5"), "diff --unified=0 a b"))
                .isInstanceOf(IOException.class)
                .hasMessageContaining("git 超时（>1s）")
                // 不说是哪条命令超时就没法诊断——这正是评审指出的问题
                .hasMessageContaining("git diff --unified=0 a b")
                .hasMessageContaining("parser.git-timeout-seconds");
        assertThat(Duration.ofNanos(System.nanoTime() - started)).isLessThan(Duration.ofSeconds(4));
    }

    @Test
    void nonPositiveTimeoutFallsBackToDefault() {
        assertThat(new GitRunner(0).timeoutSeconds()).isEqualTo(GitRunner.DEFAULT_TIMEOUT_SECONDS);
        assertThat(new GitRunner(-3).timeoutSeconds()).isEqualTo(GitRunner.DEFAULT_TIMEOUT_SECONDS);
        assertThat(new GitRunner(7).timeoutSeconds()).isEqualTo(7);
    }

    @Test
    void parserPropertiesNormalizeGitTimeout() {
        // 配置文件未提供该键时 Spring 会绑出 0 → 必须回落默认值，不能退化成"立即超时"
        assertThat(new ParserProperties(2, 100, 0).gitTimeoutSeconds())
                .isEqualTo(ParserProperties.DEFAULT_GIT_TIMEOUT_SECONDS);
        assertThat(new ParserProperties(2, 100, 7).gitTimeoutSeconds()).isEqualTo(7);
        assertThat(ParserProperties.of(2, 100).gitTimeoutSeconds())
                .isEqualTo(ParserProperties.DEFAULT_GIT_TIMEOUT_SECONDS);
    }
}
