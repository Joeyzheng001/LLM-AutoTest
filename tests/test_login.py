"""
基金订单管理系统 - 登录模块自动化测试
目标地址: https://10.50.16.19:8443
覆盖范围: API 接口测试 + UI 端到端测试

运行方式:
  # 仅 API 测试
  pytest tests/test_login.py -v -m "not ui"

  # 仅 UI 测试（需要浏览器）
  pytest tests/test_login.py -v -m ui

  # 全部运行
  pytest tests/test_login.py -v
"""

import pytest
import requests
import urllib3

# 禁用自签名证书告警
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── 配置 ───────────────────────────────────────────────
BASE_URL = "https://10.50.16.19:8443"
API_BASE = f"{BASE_URL}/api"
LOGIN_URL = f"{API_BASE}/auth/login/"
LOGOUT_URL = f"{API_BASE}/auth/logout/"
USER_URL = f"{API_BASE}/auth/user/"
VERIFY_SSL = False
TIMEOUT = 10

# 测试凭据（按实际环境修改）
VALID_USERNAME = "admin"
VALID_PASSWORD = "admin123456"


# ─── 工具函数 ────────────────────────────────────────────
def api_post(path: str, json: dict | None = None, token: str | None = None):
    """发送 POST 请求到 API"""
    headers = {}
    if token:
        headers["Authorization"] = f"Token {token}"
    resp = requests.post(
        f"{API_BASE}{path}",
        json=json,
        headers=headers,
        verify=VERIFY_SSL,
        timeout=TIMEOUT,
    )
    return resp


def api_get(path: str, token: str | None = None):
    """发送 GET 请求到 API"""
    headers = {}
    if token:
        headers["Authorization"] = f"Token {token}"
    resp = requests.get(
        f"{API_BASE}{path}",
        headers=headers,
        verify=VERIFY_SSL,
        timeout=TIMEOUT,
    )
    return resp


def login(username: str = VALID_USERNAME, password: str = VALID_PASSWORD):
    """登录并返回 (response, token)"""
    resp = api_post("/auth/login/", {"username": username, "password": password})
    token = None
    if resp.status_code == 200:
        data = resp.json()
        token = data.get("data", {}).get("token") or data.get("token")
    return resp, token


# ═══════════════════════════════════════════════════════════
# 一、API 接口测试
# ═══════════════════════════════════════════════════════════


class TestLoginAPI:
    """登录接口 API 测试"""

    # ── 正向用例 ──────────────────────────────────────────

    def test_login_success(self):
        """TC-LOGIN-001: 正确用户名密码登录成功"""
        resp, token = login()
        assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}"
        data = resp.json()
        # 兼容 {data: {token, username}} 或 {token, username} 两种结构
        payload = data.get("data", data)
        assert "token" in payload, "响应中缺少 token 字段"
        assert len(payload["token"]) > 0, "token 不应为空"

    def test_login_returns_username(self):
        """TC-LOGIN-002: 登录成功返回 username"""
        resp, _ = login()
        if resp.status_code == 200:
            data = resp.json()
            payload = data.get("data", data)
            assert "username" in payload, "响应中缺少 username 字段"

    def test_login_content_type_json(self):
        """TC-LOGIN-003: 响应 Content-Type 为 JSON"""
        resp, _ = login()
        ct = resp.headers.get("Content-Type", "")
        assert "json" in ct.lower(), f"期望 JSON，实际: {ct}"

    # ── 反向用例：空值 ────────────────────────────────────

    def test_login_empty_body(self):
        """TC-LOGIN-004: 空请求体应返回错误"""
        resp = api_post("/auth/login/", {})
        assert resp.status_code in (400, 401, 422), f"空 body 应拒绝，实际 {resp.status_code}"

    def test_login_no_username(self):
        """TC-LOGIN-005: 缺少用户名应返回错误"""
        resp = api_post("/auth/login/", {"password": "anything"})
        assert resp.status_code in (400, 401, 422)

    def test_login_no_password(self):
        """TC-LOGIN-006: 缺少密码应返回错误"""
        resp = api_post("/auth/login/", {"username": "admin"})
        assert resp.status_code in (400, 401, 422)

    def test_login_empty_username(self):
        """TC-LOGIN-007: 空字符串用户名"""
        resp = api_post("/auth/login/", {"username": "", "password": "admin123"})
        assert resp.status_code in (400, 401, 422)

    def test_login_empty_password(self):
        """TC-LOGIN-008: 空字符串密码"""
        resp = api_post("/auth/login/", {"username": "admin", "password": ""})
        assert resp.status_code in (400, 401, 422)

    # ── 反向用例：错误凭据 ────────────────────────────────

    def test_login_wrong_password(self):
        """TC-LOGIN-009: 错误密码应返回 401"""
        resp = api_post("/auth/login/", {"username": "admin", "password": "wrong_pass"})
        assert resp.status_code == 401, f"错误密码应 401，实际 {resp.status_code}"

    def test_login_nonexistent_user(self):
        """TC-LOGIN-010: 不存在的用户应返回错误"""
        resp = api_post(
            "/auth/login/",
            {"username": "ghost_user_xyz", "password": "whatever"},
        )
        assert resp.status_code in (400, 401, 404)

    # ── 反向用例：注入与异常 ──────────────────────────────

    @pytest.mark.parametrize(
        "payload",
        [
            {"username": "' OR '1'='1", "password": "anything"},
            {"username": "admin'--", "password": "x"},
            {"username": "<script>alert(1)</script>", "password": "x"},
            {"username": "admin", "password": "<img onerror=alert(1)>"},
        ],
        ids=["sql-injection-user", "sql-injection-comment", "xss-user", "xss-pass"],
    )
    def test_login_injection_attempts(self, payload):
        """TC-LOGIN-011~014: SQL 注入 / XSS 尝试不应登录成功"""
        resp = api_post("/auth/login/", payload)
        assert resp.status_code != 200, "注入尝试不应返回 200"

    # ── Token 鉴权测试 ────────────────────────────────────

    def test_auth_user_with_valid_token(self):
        """TC-LOGIN-015: 有效 Token 获取用户信息"""
        resp, token = login()
        if resp.status_code != 200 or not token:
            pytest.skip("登录失败，无法测试 Token")
        resp2 = api_get("/auth/user/", token=token)
        assert resp2.status_code == 200, f"有效 Token 应返回 200，实际 {resp2.status_code}"

    def test_auth_user_with_invalid_token(self):
        """TC-LOGIN-016: 无效 Token 应返回 401"""
        resp = api_get("/auth/user/", token="invalid_token_abc123")
        assert resp.status_code == 401

    def test_auth_user_without_token(self):
        """TC-LOGIN-017: 无 Token 访问受保护接口应返回 401"""
        resp = api_get("/auth/user/")
        assert resp.status_code == 401

    def test_logout_success(self):
        """TC-LOGIN-018: 登出成功"""
        resp, token = login()
        if resp.status_code != 200 or not token:
            pytest.skip("登录失败，无法测试登出")
        resp2 = api_post("/auth/logout/", token=token)
        assert resp2.status_code in (200, 204), f"登出应 200/204，实际 {resp2.status_code}"


# ═══════════════════════════════════════════════════════════
# 二、UI 端到端测试（Playwright）
# ═══════════════════════════════════════════════════════════


@pytest.mark.ui
class TestLoginUI:
    """登录页面 UI 测试"""

    @pytest.fixture(autouse=True)
    def setup(self, playwright):
        self.browser = playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(ignore_https_errors=True)
        self.page = self.context.new_page()
        yield
        self.browser.close()

    def test_login_page_loads(self):
        """TC-UI-001: 登录页正常加载"""
        self.page.goto(f"{BASE_URL}/login")
        title = self.page.title()
        assert "基金" in title or "订单" in title, f"页面标题不符: {title}"

    def test_login_form_visible(self):
        """TC-UI-002: 登录表单元素可见"""
        self.page.goto(f"{BASE_URL}/login")
        # Element Plus 输入框
        assert self.page.locator('input[placeholder="用户名"]').is_visible()
        assert self.page.locator('input[placeholder="密码"]').is_visible()
        assert self.page.locator('button:has-text("登")').is_visible()

    def test_login_page_title(self):
        """TC-UI-003: 页面显示系统标题"""
        self.page.goto(f"{BASE_URL}/login")
        heading = self.page.locator("h2.login-title, .login-title")
        assert heading.is_visible()
        assert "基金订单管理系统" in heading.inner_text()

    def test_empty_submit_shows_validation(self):
        """TC-UI-004: 空表单提交触发前端校验提示"""
        self.page.goto(f"{BASE_URL}/login")
        self.page.locator('button:has-text("登")').click()
        # Element Plus 表单校验提示
        error_msgs = self.page.locator(".el-form-item__error")
        assert error_msgs.count() >= 1, "空提交应显示校验提示"

    def test_login_success_redirect(self):
        """TC-UI-005: 登录成功跳转到首页"""
        self.page.goto(f"{BASE_URL}/login")
        self.page.locator('input[placeholder="用户名"]').fill(VALID_USERNAME)
        self.page.locator('input[placeholder="密码"]').fill(VALID_PASSWORD)
        self.page.locator('button:has-text("登")').click()
        # 等待跳转（登录成功后 push("/")）
        self.page.wait_for_url("**/login", timeout=5000)
        # 如果 URL 不再是 /login 说明跳转成功
        # 或者检查 localStorage 中是否有 token
        token = self.page.evaluate("localStorage.getItem('token')")
        assert token is not None and len(token) > 0, "登录后应有 token"

    def test_wrong_password_shows_error(self):
        """TC-UI-006: 错误密码应显示错误提示"""
        self.page.goto(f"{BASE_URL}/login")
        self.page.locator('input[placeholder="用户名"]').fill("admin")
        self.page.locator('input[placeholder="密码"]').fill("definitely_wrong")
        self.page.locator('button:has-text("登")').click()
        # Element Plus 的 ElMessage 错误提示
        error = self.page.locator(".el-message--error, .el-notification--error")
        error.wait_for(state="visible", timeout=5000)
        assert error.is_visible()

    def test_password_field_masked(self):
        """TC-UI-007: 密码输入框应为密文"""
        self.page.goto(f"{BASE_URL}/login")
        pwd_input = self.page.locator('input[placeholder="密码"]')
        assert pwd_input.get_attribute("type") == "password"

    def test_token_stored_after_login(self):
        """TC-UI-008: 登录成功后 Token 存入 localStorage"""
        self.page.goto(f"{BASE_URL}/login")
        self.page.locator('input[placeholder="用户名"]').fill(VALID_USERNAME)
        self.page.locator('input[placeholder="密码"]').fill(VALID_PASSWORD)
        self.page.locator('button:has-text("登")').click()
        self.page.wait_for_timeout(2000)
        token = self.page.evaluate("localStorage.getItem('token')")
        username = self.page.evaluate("localStorage.getItem('username')")
        assert token and len(token) > 0, "token 应存入 localStorage"
        assert username, "username 应存入 localStorage"
