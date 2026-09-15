package com.demo.order;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** 订单中心（G3 第二仓库；gRPC 契约见 src/main/proto/——静态识别未接入，如实降级）。 */
@SpringBootApplication
@RestController
@RequestMapping("/api/v1/orders")
public class OrderApplication {

    private final Map<Long, Map<String, Object>> store = new ConcurrentHashMap<>();
    private final AtomicLong idGen = new AtomicLong(2);

    public static void main(String[] args) {
        SpringApplication.run(OrderApplication.class, args);
    }

    @GetMapping("/{id}")
    public Map<String, Object> getOrder(@PathVariable long id) {
        Map<String, Object> order = store.get(id);
        if (order == null) {
            throw new IllegalArgumentException("订单不存在：" + id);
        }
        return order;
    }

    @GetMapping
    public List<Map<String, Object>> listOrders(@RequestParam(required = false) Long userId) {
        return store.values().stream()
            .filter(o -> userId == null || userId.equals(o.get("userId")))
            .toList();
    }

    @PostMapping
    public Map<String, Object> createOrder(@RequestBody Map<String, Object> order) {
        long id = idGen.getAndIncrement();
        order.put("id", id);
        order.put("status", "CREATED");
        store.put(id, order);
        return order;
    }
}
