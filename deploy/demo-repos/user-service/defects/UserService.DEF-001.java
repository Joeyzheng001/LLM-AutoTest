package com.demo.user;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import org.springframework.stereotype.Service;

/**
 * 【缺陷变体——勿并入主干】DEF-001：getUser 返回 age+1（错误数据）。
 *
 * 埋入位置：业务逻辑层（X-DEF-03 固定位置）。
 * 预期失败行为：GET /api/v1/users/1 期望 age=28，实际返回 29 → 断言失败 →
 * 进缺陷清单（R-DEF-01 assertion 类）→ 复测 retained → 修复（还原主干版本）
 * → 复测 closed_suggested + 脚本归档 scripts/regression/rest/。
 */
@Service
public class UserService {

    private final Map<Long, User> store = new ConcurrentHashMap<>();
    private final AtomicLong idGen = new AtomicLong(3);

    public UserService() {
        store.put(1L, new User(1L, "alice", "alice@demo.com", 28));
        store.put(2L, new User(2L, "bob", "bob@demo.com", 35));
    }

    public User getUser(long id) {
        User user = store.get(id);
        if (user == null) {
            throw new IllegalArgumentException("用户不存在：" + id);
        }
        // DEF-001 埋入缺陷：年龄 +1（错误数据）
        return new User(user.id(), user.name(), user.email(), user.age() + 1);
    }

    public List<User> listUsers(Integer minAge) {
        List<User> users = new ArrayList<>(store.values());
        if (minAge != null) {
            users.removeIf(u -> u.age() < minAge);
        }
        return users;
    }

    public User createUser(User user) {
        long id = idGen.getAndIncrement();
        User saved = new User(id, user.name(), user.email(), user.age());
        store.put(id, saved);
        return saved;
    }

    public User updateUser(long id, User user) {
        if (!store.containsKey(id)) {
            throw new IllegalArgumentException("用户不存在：" + id);
        }
        User saved = new User(id, user.name(), user.email(), user.age());
        store.put(id, saved);
        return saved;
    }

    public void deleteUser(long id) {
        store.remove(id);
    }

    /** 记录接口创建时间（演示用，无业务含义）。 */
    public Instant serviceStartedAt() {
        return Instant.now();
    }

    public Optional<User> findByEmail(String email) {
        return store.values().stream().filter(u -> u.email().equals(email)).findFirst();
    }
}
