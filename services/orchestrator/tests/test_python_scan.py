"""python_scan 单元测试（C-13）：Django urls 树 / DRF router / 装饰器路由 / 四态。

全部基于临时仓库的真实 .py 文件走 AST（不经 LLM，数值/溯源代码派生红线）。
覆盖：include 前缀拼接、CBV as_view 方法派生、ModelViewSet 继承标准路由、
@action 自定义路由、<converter:name> 归一、FastAPI/Flask 装饰器、
inventory 四态（ok/empty/error）与 C-09 基线 diff 口径。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.javaparser_client import RepositoryInput
from orchestrator.python_scan import _normalize_django_path, scan_repository

_CONFIG_URLS = '''
from django.urls import path, include
from orders.views import LoginView

urlpatterns = [
    path("api/auth/login", LoginView.as_view()),
    path("api/orders/", include("orders.urls")),
]
'''

_ORDERS_URLS = '''
from django.urls import path
from rest_framework.routers import DefaultRouter
from orders.views import ProductConfigViewSet, cancel_order

router = DefaultRouter()
router.register("products", ProductConfigViewSet)

urlpatterns = [
    path("cancel/<int:order_id>", cancel_order),
]
'''

_ORDERS_VIEWS = '''
from rest_framework.decorators import action
from rest_framework.viewsets import ModelViewSet


class LoginView:
    def get(self, request):
        return None

    def post(self, request):
        return None


class ProductConfigViewSet(ModelViewSet):
    def list(self, request):
        return None

    def create(self, request):
        return None

    @action(detail=False, methods=["get"], url_path="active")
    def active(self, request):
        return None


def cancel_order(request):
    return None
'''


def test_normalize_django_path_preserves_trailing_slash() -> None:
    """Django 的 path('x/') 必须保留末尾 /，否则 POST 可能被代理层拒绝。"""
    assert _normalize_django_path("api/", "auth/login/") == "/api/auth/login/"
    assert _normalize_django_path("api/", "auth/login") == "/api/auth/login"

_FASTAPI_APP = '''
from fastapi import FastAPI

app = FastAPI()


@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {"id": item_id}


@app.post("/items")
def create_item():
    return {}
'''

_FLASK_APP = '''
from flask import Flask

app = Flask(__name__)


@app.route("/ping", methods=["GET", "POST"])
def ping():
    return "pong"
'''


def _write_repo(tmp_path: Path) -> Path:
    """构造混合框架的 Python 仓库（Django + DRF + FastAPI + Flask）。"""
    root = tmp_path / "py-repo"
    (root / "config").mkdir(parents=True)
    (root / "orders").mkdir()
    (root / "config" / "urls.py").write_text(_CONFIG_URLS, encoding="utf-8")
    (root / "orders" / "urls.py").write_text(_ORDERS_URLS, encoding="utf-8")
    (root / "orders" / "views.py").write_text(_ORDERS_VIEWS, encoding="utf-8")
    (root / "app.py").write_text(_FASTAPI_APP, encoding="utf-8")
    (root / "flask_app.py").write_text(_FLASK_APP, encoding="utf-8")
    return root


def _repo(root: Path, **kwargs: object) -> RepositoryInput:
    params: dict[str, object] = {
        "name": "py-repo",
        "path": str(root),
        "branch": "main",
        "language": "python",
    }
    params.update(kwargs)
    return RepositoryInput(**params)  # type: ignore[arg-type]


def test_python_scan_django_tree_candidates(tmp_path: Path) -> None:
    """Django/DRF 路由树：include 前缀、CBV 方法、ViewSet 继承与 @action。"""
    root = _write_repo(tmp_path)
    result = scan_repository(_repo(root))

    assert result.inventory_entry["status"] == "ok"
    assert result.inventory_entry["language"] == "python"
    assert result.inventory_entry["file_count"] == 5
    signatures = [c["signature"] for c in result.candidates]
    by_sig = {c["signature"]: c for c in result.candidates}

    # CBV as_view：类内 get/post 各派生一条（置信度 0.90）
    assert "GET /api/auth/login" in by_sig
    assert "POST /api/auth/login" in by_sig
    assert by_sig["GET /api/auth/login"]["confidence"] == 0.90
    # CBV 溯源应落在实际处理方法，而不是 urls.py；方法源码随候选带给接口还原。
    assert by_sig["POST /api/auth/login"]["code_location"]["file"] == "orders/views.py"
    assert "def post" in by_sig["POST /api/auth/login"]["snippet"]

    # include 前缀拼接 + FBV 兜底 GET（0.70）+ <int:x> → {x}
    assert by_sig["GET /api/orders/cancel/{order_id}"]["confidence"] == 0.70

    # ModelViewSet 继承 → 6 条标准路由（0.80），detail 路由带 {pk}
    for sig in (
        "GET /api/orders/products/",
        "POST /api/orders/products/",
        "GET /api/orders/products/{pk}/",
        "PUT /api/orders/products/{pk}/",
        "PATCH /api/orders/products/{pk}/",
        "DELETE /api/orders/products/{pk}/",
    ):
        assert sig in by_sig, f"缺标准路由：{sig}"
        assert by_sig[sig]["confidence"] == 0.80

    # @action(detail=False, url_path="active") 自定义路由
    assert "GET /api/orders/products/active/" in by_sig

    # 候选全量（2 login + 7 products + 1 cancel）+ 装饰器 4 条
    assert len(signatures) == 14, signatures

    # 溯源：@action 指向定义文件 views.py 而非 urls.py
    active = by_sig["GET /api/orders/products/active/"]
    assert active["code_location"]["file"] == "orders/views.py"
    # 溯源：候选 ID 为 CAND-### 顺序编号
    assert [c["candidate_id"] for c in result.candidates] == [
        f"CAND-{i:03d}" for i in range(1, len(result.candidates) + 1)
    ]


def test_python_scan_decorator_routes(tmp_path: Path) -> None:
    """FastAPI/Flask 装饰器：方法装饰器直书路由（置信度 0.95）。"""
    root = _write_repo(tmp_path)
    result = scan_repository(_repo(root))
    by_sig = {c["signature"]: c for c in result.candidates}

    assert by_sig["GET /items/{item_id}"]["confidence"] == 0.95
    assert by_sig["POST /items"]["confidence"] == 0.95
    assert by_sig["GET /items/{item_id}"]["code_location"]["file"] == "app.py"
    # Flask route(methods=[...]) 多方法派生
    assert "GET /ping" in by_sig and "POST /ping" in by_sig


def test_python_scan_path_normalization(tmp_path: Path) -> None:
    """路径归一：首斜杠补齐、末尾斜杠保留、<converter:name> → {name}。"""
    root = tmp_path / "norm-repo"
    root.mkdir()
    (root / "urls.py").write_text(
        "from django.urls import path\n"
        "from views import health\n"
        "urlpatterns = [path('api/v1/health/', health)]\n",
        encoding="utf-8",
    )
    (root / "views.py").write_text("def health(request):\n    return None\n", encoding="utf-8")
    result = scan_repository(_repo(root))
    assert [c["signature"] for c in result.candidates] == ["GET /api/v1/health/"]


def test_python_scan_missing_path_error(tmp_path: Path) -> None:
    """仓库路径不存在：error 四态（X-CODE-01 宁停不猜）。"""
    result = scan_repository(
        RepositoryInput(
            name="ghost", path=str(tmp_path / "ghost"), branch="main", language="python"
        )
    )
    entry = result.inventory_entry
    assert entry["status"] == "error"
    assert entry["error"]["code"] == "X-CODE-01"
    assert result.candidates == []


def test_python_scan_empty_repo(tmp_path: Path) -> None:
    """无 .py 文件：empty 四态（正常态，流程继续）。"""
    root = tmp_path / "empty-repo"
    (root / "static").mkdir(parents=True)
    result = scan_repository(_repo(root))
    assert result.inventory_entry["status"] == "empty"
    assert result.inventory_entry["file_count"] == 0
    assert result.candidates == []


def test_python_scan_include_paths_filter(tmp_path: Path) -> None:
    """include_paths 前缀过滤：只扫描指定子目录的文件。"""
    root = _write_repo(tmp_path)
    result = scan_repository(_repo(root, include_paths=("orders",)))
    files = [f["path"] for f in result.inventory_entry["files"]]
    assert files == ["orders/urls.py", "orders/views.py"]
    # FastAPI/Flask 文件不在扫描范围 → 装饰器路由不产出
    decorator_sigs = [c["signature"] for c in result.candidates if "/items" in c["signature"]]
    assert not decorator_sigs and not any("/ping" in c["signature"] for c in result.candidates)


def test_python_scan_bad_baseline_branch_error(tmp_path: Path) -> None:
    """基线分支不存在：仓库级 error（C-09 同口径）。"""
    root = _write_repo(tmp_path)
    result = scan_repository(_repo(root, baseline_branch="nope"))
    assert result.inventory_entry["status"] == "error"
    assert "X-CODE-01" in result.inventory_entry["error"]["message"]
    assert result.candidates == []


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603, S607


def test_python_scan_baseline_diff(tmp_path: Path) -> None:
    """C-09 基线 diff：merge-base 三点 diff，added/modified 如实分列。"""
    root = tmp_path / "git-repo"
    root.mkdir()
    (root / "urls.py").write_text(
        "from django.urls import path\n"
        "from views import list_users\n"
        "urlpatterns = [path('users', list_users)]\n",
        encoding="utf-8",
    )
    (root / "views.py").write_text("def list_users(request):\n    return None\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    _git(root, "checkout", "-b", "feature")
    (root / "views.py").write_text(
        "def list_users(request):\n    return None\n\n\n"
        "def create_user(request):\n    return None\n",
        encoding="utf-8",
    )
    (root / "extra.py").write_text("X = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "feature")

    result = scan_repository(_repo(root, baseline_branch="main", branch="feature"))
    entry = result.inventory_entry
    assert entry["status"] == "ok"
    assert entry["baseline_branch"] == "main"
    assert entry["changed_files"]["added"] == ["extra.py"]
    assert entry["changed_files"]["modified"] == ["views.py"]
    assert entry["changed_files"]["removed"] == []
