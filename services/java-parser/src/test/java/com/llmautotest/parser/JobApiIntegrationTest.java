// 契约端到端：POST 受理（202）→ GET 轮询至 completed（含产物）；400/404 错误体口径。
package com.llmautotest.parser;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

@SpringBootTest
@AutoConfigureMockMvc
class JobApiIntegrationTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @TempDir
    Path tempDir;

    @Test
    void scanJobFullLifecycle() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("user-service"));
        Files.writeString(repo.resolve("UserController.java"),
                "@RestController class C { @GetMapping(\"/u\") String u() { return \"\"; } }");
        String body = objectMapper.writeValueAsString(java.util.Map.of(
                "job_id", "job-int-1",
                "repositories", java.util.List.of(java.util.Map.of(
                        "name", "user-service",
                        "path", repo.toString(),
                        "branch", "main"))));

        MvcResult accepted = mockMvc.perform(post("/api/v1/scan-jobs")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.job_id").value("job-int-1"))
                .andExpect(jsonPath("$.status").value("queued"))
                .andReturn();

        assertThat(accepted.getResponse().getStatus()).isEqualTo(202);
        JsonNode finalState = pollUntilCompleted("/api/v1/scan-jobs/job-int-1");
        assertThat(finalState.get("code_inventory").get("job_id").asText()).isEqualTo("job-int-1");
        assertThat(finalState.get("code_inventory").get("repositories").get(0)
                .get("status").asText()).isEqualTo("ok");
    }

    @Test
    void extractJobFullLifecycle() throws Exception {
        Path repo = Files.createDirectories(tempDir.resolve("order-center"));
        Files.writeString(repo.resolve("OrderController.java"), """
                @RestController
                @RequestMapping("/api/v1/orders")
                class OrderController {
                    @GetMapping("/{id}")
                    String get() { return "order"; }
                }
                """);
        String body = objectMapper.writeValueAsString(java.util.Map.of(
                "job_id", "job-int-2",
                "repositories", java.util.List.of(java.util.Map.of(
                        "name", "order-center",
                        "path", repo.toString(),
                        "branch", "master"))));

        mockMvc.perform(post("/api/v1/extract-jobs")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.job_id").value("job-int-2"));

        JsonNode finalState = pollUntilCompleted("/api/v1/extract-jobs/job-int-2");
        assertThat(finalState.get("candidates").get(0).get("signature").asText())
                .isEqualTo("GET /api/v1/orders/{id}");
        assertThat(finalState.get("warnings").isArray()).isTrue();
    }

    @Test
    void emptyRepositoriesRejectedWithContractError() throws Exception {
        String body = objectMapper.writeValueAsString(java.util.Map.of(
                "job_id", "job-int-3", "repositories", java.util.List.of()));

        mockMvc.perform(post("/api/v1/scan-jobs")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_REQUEST"));
    }

    @Test
    void unknownJobReturns404() throws Exception {
        mockMvc.perform(get("/api/v1/scan-jobs/job-nope"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("JOB_NOT_FOUND"));
    }

    /** 轮询直至终态（completed/failed），总时限 5 秒（本地 FS 任务瞬时完成）。 */
    private JsonNode pollUntilCompleted(String path) throws Exception {
        long deadline = System.currentTimeMillis() + 5_000;
        while (System.currentTimeMillis() < deadline) {
            MvcResult result = mockMvc.perform(get(path)).andExpect(status().isOk()).andReturn();
            JsonNode state = objectMapper.readTree(result.getResponse().getContentAsString());
            String status = state.get("status").asText();
            if ("completed".equals(status) || "failed".equals(status)) {
                return state;
            }
            Thread.sleep(20);
        }
        throw new AssertionError("job 未在时限内到达终态：" + path);
    }
}
