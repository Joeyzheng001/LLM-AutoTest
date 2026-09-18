"""Python 仓库静态扫描（C-13）：Django / FastAPI / Flask 路由 → 清单 + 接口候选。

与 java-parser 同口径产出（X-CODE-01~03 四态 + interface_candidates 候选），
全部由代码派生，不经 LLM（数值/溯源诚实红线）。支持：
- Django：urls.py 为根（path/re_path/include 递归展开；CBV .as_view() 按类内
  get/post/put/delete/patch 派生方法；DRF router.register 映射标准 REST 路由；
  <converter:name> 路径参数归一为 {name}）；
- FastAPI：@app/router.get("/x") 等方法装饰器（api_route 带 methods）；
- Flask：@app.route("/x", methods=[...])（缺省 GET）。

基线 diff 与 java-parser 同口径（C-09）：git diff --name-status <基线>...<被测>，
merge-base，不动工作区；失败（非 Git 仓库/ref 不存在）→ 仓库级 error（X-CODE-01
宁停不猜）。被测分支仅留痕（维持「已检出正确分支的本地路径」口径）。
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.javaparser_client import RepositoryInput

HTTP_VERBS = ("get", "post", "put", "delete", "patch")

# DRF ViewSet 标准动作 → (HTTP 方法, 是否 detail 路由)
_VIEWSET_ACTIONS: dict[str, tuple[str, bool]] = {
    "list": ("GET", False),
    "create": ("POST", False),
    "retrieve": ("GET", True),
    "update": ("PUT", True),
    "partial_update": ("PATCH", True),
    "destroy": ("DELETE", True),
}

# files 清单上限（超大仓库防产物膨胀；超出记 warning 如实标注）
_MAX_FILES_LIST = 2000

# 置信度口径（静态派生，非 LLM）：
# 装饰器直书路由 0.95；Django path+CBV 方法 0.90；DRF router 约定推导 0.80；
# 无法定位 HTTP 方法的兜底 0.70
_CONF_DECORATOR = 0.95
_CONF_DJANGO = 0.90
_CONF_ROUTER = 0.80
_CONF_DEFAULT_GET = 0.70


@dataclass
class PythonScanResult:
    """单仓库扫描结果：inventory 条目 + 候选清单 + 告警。"""

    inventory_entry: dict[str, Any]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def scan_repository(repo: RepositoryInput) -> PythonScanResult:
    """扫描单个 Python 仓库（code_scan 与 interface_reconstruct 共用，确定性）。"""
    root = Path(repo.path)
    if not root.is_dir():
        return PythonScanResult(
            inventory_entry=_error_entry(repo, f"仓库路径不存在或不是目录：{repo.path}")
        )

    changed = _git_diff(root, repo.baseline_branch, repo.branch) if repo.baseline_branch else None
    if isinstance(changed, str):  # diff 失败信息
        return PythonScanResult(inventory_entry=_error_entry(repo, changed))

    py_files = _collect_py_files(root, repo.include_paths)
    warnings: list[str] = []
    entry: dict[str, Any] = {
        "name": repo.name,
        "path": repo.path,
        "branch": repo.branch,
        "language": "python",
        "is_fallback": repo.is_fallback,
        "status": "ok" if py_files else "empty",
        "file_count": len(py_files),
    }
    if repo.baseline_branch:
        entry["baseline_branch"] = repo.baseline_branch
        entry["changed_files"] = changed or {"added": [], "modified": [], "removed": []}
    files_list = [
        {
            "repo": repo.name,
            "module": rel.parts[0] if len(rel.parts) > 1 else ".",
            "path": rel.as_posix(),
        }
        for rel in (p.relative_to(root) for p in py_files[:_MAX_FILES_LIST])
    ]
    if len(py_files) > _MAX_FILES_LIST:
        warnings.append(
            f"X-CODE-03：files 清单截断为前 {_MAX_FILES_LIST} 项（共 {len(py_files)} 个 .py）"
        )
        entry["truncated_scope"] = ["<files 清单截断，路由扫描仍为全量>"]
    entry["files"] = files_list

    candidates = _extract_routes(root, py_files, repo, warnings)
    return PythonScanResult(inventory_entry=entry, candidates=candidates, warnings=warnings)


# ---- 文件收集与解析 ----


def _collect_py_files(root: Path, include_paths: tuple[str, ...]) -> list[Path]:
    """收集 .py 文件（跳过 .git/__pycache__/venv；include_paths 非空时按前缀过滤）。"""
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", ".mypy_cache"}
    files = [
        p
        for p in root.rglob("*.py")
        if not (skip_dirs & set(p.relative_to(root).parts[:-1]))
    ]
    if include_paths:
        prefixes = tuple(Path(item.strip("/")).as_posix() for item in include_paths)
        files = [p for p in files if p.relative_to(root).as_posix().startswith(prefixes)]
    return sorted(files)


def _parse_ast(path: Path, warnings: list[str]) -> ast.Module | None:
    """单文件 AST 解析（失败跳过记告警，X-REV-01 同口径不中断）。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        warnings.append(f"X-REV-01：Python 文件解析失败（跳过）：{path.name}：{exc}")
        return None


# ---- 基线 diff（C-09 同口径）----


def _git_diff(root: Path, baseline: str, branch: str) -> dict[str, list[str]] | str:
    """git diff --name-status <基线>...<被测>（merge-base，不动工作区）。

    Returns:
        {"added": [...], "modified": [...], "removed": [...]}；失败时返回错误信息。
    """
    cmd = ["git", "-C", str(root), "diff", "--name-status", f"{baseline}...{branch}"]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"基线 diff 执行失败（X-CODE-01 宁停不猜）：{exc}"
    if completed.returncode != 0:
        stderr_lines = completed.stderr.strip().splitlines()
        detail = stderr_lines[0] if stderr_lines else "未知原因"
        return f"基线 diff 失败（X-CODE-01 宁停不猜）：{baseline}...{branch}：{detail}"
    result: dict[str, list[str]] = {"added": [], "modified": [], "removed": []}
    status_map = {"A": "added", "M": "modified", "D": "removed"}
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or parts[0][:1] not in status_map:
            continue  # 重命名/拷贝等复合状态如实跳过（文件级三类口径）
        result[status_map[parts[0][:1]]].append(parts[1])
    return result


# ---- 路由提取 ----


def _extract_routes(
    root: Path, py_files: list[Path], repo: RepositoryInput, warnings: list[str]
) -> list[dict[str, Any]]:
    """Django urls 树 + FastAPI/Flask 装饰器 → 候选清单（CAND-### 顺序编号）。"""
    trees: dict[Path, ast.Module] = {}
    for path in py_files:
        tree = _parse_ast(path, warnings)
        if tree is not None:
            trees[path] = tree

    routes: list[dict[str, Any]] = []
    urls_files = [p for p in py_files if p.name == "urls.py"]
    included_modules: set[str] = set()
    for urls_path in urls_files:
        _walk_django_urls(root, urls_path, trees, repo, "", routes, warnings, included_modules)
    for path, tree in trees.items():
        _scan_decorators(path, root, repo, tree, routes)

    routes.sort(key=lambda r: (r["code_location"]["file"], r["code_location"]["start_line"]))
    for index, route in enumerate(routes, start=1):
        route["candidate_id"] = f"CAND-{index:03d}"
    return routes


def _module_name(root: Path, path: Path) -> str:
    """文件 → 点分模块名（含 __init__ 视为包本身）。"""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def _resolve_import(
    root: Path,
    trees: dict[Path, ast.Module],
    module: str,
    symbol: str,
) -> Path | None:
    """按模块名定位文件（先根相对，再后缀匹配）；返回定义 symbol 的文件。"""
    rel = module.replace(".", "/")
    for candidate in (root / f"{rel}.py", root / rel / "__init__.py"):
        if candidate in trees:
            return candidate
    suffix = f"{rel}.py"
    for path in trees:
        if path.relative_to(root).as_posix().endswith(suffix):
            return path
    return None


def _find_view_class(
    root: Path,
    trees: dict[Path, ast.Module],
    view_name: str,
    imports: dict[str, str],
    urls_path: Path | None,
) -> tuple[ast.ClassDef | None, Path | None]:
    """定位 view 类定义 → (类节点, 定义文件)。

    先在 urls.py 本文件找，再按 import 解析到定义文件（含仓内后缀匹配）。
    """
    if urls_path is not None and urls_path in trees:
        for node in trees[urls_path].body:
            if isinstance(node, ast.ClassDef) and node.name == view_name:
                return node, urls_path
    module = imports.get(view_name)
    if module:
        target = _resolve_import(root, trees, module, view_name)
        if target is not None:
            for node in trees[target].body:
                if isinstance(node, ast.ClassDef) and node.name == view_name:
                    return node, target
    return None, None


def _attr_or_id(func: ast.expr) -> str:
    """属性/名称表达式 → 字符串（其他形态返回空串）。"""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _url_call(node: ast.expr) -> tuple[str, ast.expr] | None:
    """识别 path()/re_path()/url() 调用 → (路由串, view 表达式)。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = _attr_or_id(func)
    if name not in ("path", "re_path", "url"):
        return None
    if (
        not node.args
        or not isinstance(node.args[0], ast.Constant)
        or not isinstance(node.args[0].value, str)
    ):
        return None
    view = node.args[1] if len(node.args) > 1 else None
    if view is None:
        return None
    return node.args[0].value, view


def _walk_django_urls(
    root: Path,
    urls_path: Path,
    trees: dict[Path, ast.Module],
    repo: RepositoryInput,
    prefix: str,
    routes: list[dict[str, Any]],
    warnings: list[str],
    included_modules: set[str],
) -> None:
    """递归展开一个 urls.py：path/re_path 条目 + include() 子模块 + router.register。"""
    tree = trees.get(urls_path)
    if tree is None:
        return
    module_file = _module_name(root, urls_path)
    if module_file in included_modules:
        return  # 同一模块被多个根 include（如 admin + api）：只展开一次防重复
    included_modules.add(module_file)

    imports = _import_map(tree)
    elements = _urlpattern_elements(tree)

    # DRF router.register（先展开，前缀独立于 urlpatterns 顺序）
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else ""
            if name == "register" and node.args and isinstance(node.args[1], ast.Name):
                first_arg = node.args[0]
                url_prefix = (
                    first_arg.value
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)
                    else ""
                )
                _emit_router_routes(
                    root, trees, repo, prefix, url_prefix, node.args[1].id, imports,
                    urls_path, routes, node.lineno,
                )

    for element in elements:
        parsed = _url_call(element)
        if parsed is None:
            continue
        route, view = parsed
        if isinstance(view, ast.Call):
            view_func = view.func
            view_name = _attr_or_id(view_func)
            if view_name == "include" and view.args and isinstance(view.args[0], ast.Constant):
                # include("orders.urls") → 递归子模块（前缀拼接）
                sub_value = view.args[0].value
                if not isinstance(sub_value, str):
                    continue
                sub_path = _resolve_import(root, trees, sub_value, "")
                if sub_path is not None:
                    _walk_django_urls(
                        root, sub_path, trees, repo, _join_prefix(prefix, route),
                        routes, warnings, included_modules,
                    )
                else:
                    warnings.append(f"X-REV-01：include 模块未找到（跳过）：{sub_value}")
                continue
            if view_name == "as_view" and isinstance(view_func, ast.Attribute):
                _emit_view_routes(
                    root, trees, repo, prefix, route, view_func.value, urls_path,
                    element.lineno, _CONF_DJANGO, routes, imports=imports,
                )
                continue
        if isinstance(view, ast.Name):
            _emit_view_routes(
                root, trees, repo, prefix, route, view, urls_path, element.lineno,
                _CONF_DJANGO, routes, imports=imports,
            )


def _urlpattern_elements(tree: ast.Module) -> list[ast.expr]:
    """urlpatterns = [...] 赋值元素 + urlpatterns.append(...) 条目（条件追加）。"""
    elements: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "urlpatterns"
                    and isinstance(node.value, (ast.List, ast.Tuple))
                ):
                    elements.extend(node.value.elts)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "append"
                and isinstance(func.value, ast.Name)
                and func.value.id == "urlpatterns"
            ):
                elements.extend(node.value.args)
    return elements


def _import_map(tree: ast.Module) -> dict[str, str]:
    """from X.Y import Name [as Alias] → {绑定名: 模块}。"""
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module
    return imports


def _emit_view_routes(
    root: Path,
    trees: dict[Path, ast.Module],
    repo: RepositoryInput,
    prefix: str,
    route: str,
    view_expr: ast.expr,
    urls_path: Path,
    line: int,
    confidence: float,
    routes: list[dict[str, Any]],
    imports: dict[str, str] | None = None,
) -> None:
    """按 view 表达式派生 HTTP 方法候选（CBV 类内方法 / 函数缺省 GET）。"""
    view_name = view_expr.id if isinstance(view_expr, ast.Name) else ""
    if not view_name:
        return
    class_node, class_file = _find_view_class(root, trees, view_name, imports or {}, urls_path)
    method_map = _class_node_methods(class_node)
    methods = [m for m in HTTP_VERBS if m in method_map]
    if not methods:
        methods = ["GET"]
        confidence = min(confidence, _CONF_DEFAULT_GET)
    full_path = _normalize_django_path(prefix, route)
    for method in methods:
        method_node = method_map.get(method)
        # 将请求处理方法与其直接使用的 Serializer 定义一并给接口还原。仅看到
        # `serializer = XSerializer(data=request.data)` 时，模型无法知道真实字段，
        # 容易退化为 valid_payload 一类语义标签。
        snippet = _node_snippet(class_file, method_node)
        serializer_context = _serializer_context(root, trees, class_file, method_node)
        if serializer_context:
            snippet = f"{snippet or ''}\n\n{serializer_context}".strip()
        routes.append(
            _candidate(
                repo,
                class_file or urls_path,
                root,
                f"{method.upper()} {full_path}",
                method_node.lineno if method_node is not None else line,
                confidence,
                snippet=snippet,
            )
        )


def _class_node_methods(
    class_node: ast.ClassDef | None,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """类节点 → 全量方法定义（含 CBV HTTP 动词与 DRF ViewSet 动作名）。"""
    if class_node is None:
        return {}
    return {
        item.name: item
        for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _emit_router_routes(
    root: Path,
    trees: dict[Path, ast.Module],
    repo: RepositoryInput,
    prefix: str,
    url_prefix: str,
    viewset_name: str,
    imports: dict[str, str],
    urls_path: Path,
    routes: list[dict[str, Any]],
    register_line: int = 1,
) -> None:
    """DRF router.register → 标准 REST 路由 + @action 自定义路由。

    ModelViewSet 等基类的标准动作不在类体内（继承而来）——检测基类名含
    ViewSet/ModelViewSet 时按 DRF 约定派生全量标准路由（置信度降档）。"""
    class_node, class_file = _find_view_class(root, trees, viewset_name, imports, urls_path)
    method_map = _class_node_methods(class_node)
    base_names = [_attr_or_id(base) for base in class_node.bases] if class_node else []
    inherits_viewset = any("ViewSet" in name for name in base_names if name)
    if not method_map and not inherits_viewset:
        return
    base = _join_prefix(prefix, url_prefix)
    for action, (verb, detail) in _VIEWSET_ACTIONS.items():
        if action not in method_map and not (inherits_viewset and action in _VIEWSET_ACTIONS):
            continue
        # DRF DefaultRouter 默认 trailing_slash=True；候选须保留真实路由语义。
        path = f"{base}/{{pk}}/" if detail else f"{base}/"
        line = method_map[action].lineno if action in method_map else register_line
        routes.append(
            _candidate(
                repo, class_file or urls_path, root,
                f"{verb} {_normalize_django_path('', path)}", line, _CONF_ROUTER,
            )
        )
    # @action(methods=[...], url_path="x", detail=...) 自定义路由
    for action_name, func_node in method_map.items():
        for decorator in func_node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            dname = _attr_or_id(func)
            if dname != "action":
                continue
            http_methods = ["GET"]
            url_path, detail = action_name, False
            for keyword in decorator.keywords:
                if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    http_methods = [
                        elt.value.upper()
                        for elt in keyword.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ] or ["GET"]
                elif keyword.arg == "url_path" and isinstance(keyword.value, ast.Constant):
                    url_path = str(keyword.value.value)
                elif keyword.arg == "detail" and isinstance(keyword.value, ast.Constant):
                    detail = bool(keyword.value.value)
            action_route = f"{{{url_path}}}" if url_path.startswith("<") else url_path
            path = (
                f"{base}/{{pk}}/{action_route}/"
                if detail
                else f"{base}/{action_route}/"
            )
            for verb in http_methods:
                routes.append(
                    _candidate(
                        repo,
                        class_file or urls_path,
                        root,
                        f"{verb} {_normalize_django_path('', path)}",
                        decorator.lineno,
                        _CONF_ROUTER,
                    )
                )


def _scan_decorators(
    path: Path, root: Path, repo: RepositoryInput, tree: ast.Module, routes: list[dict[str, Any]]
) -> None:
    """FastAPI/Flask 装饰器路由（@x.get("/p") / @x.route("/p", methods=[...])）。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if not attr:
                continue
            path_arg = decorator.args[0] if decorator.args else None
            if not (
                isinstance(path_arg, ast.Constant)
                and isinstance(path_arg.value, str)
                and path_arg.value.startswith("/")
            ):
                continue
            verbs: list[str] = []
            if attr in ("get", "post", "put", "delete", "patch", "head", "options"):
                verbs = [attr.upper()]
            elif attr in ("route", "api_route"):
                verbs = ["GET"]
                for keyword in decorator.keywords:
                    if (
                        keyword.arg == "methods"
                        and isinstance(keyword.value, (ast.List, ast.Tuple))
                    ):
                        verbs = [
                            elt.value.upper()
                            for elt in keyword.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ] or ["GET"]
            for verb in verbs:
                routes.append(
                    _candidate(
                        repo,
                        path,
                        root,
                        f"{verb} {path_arg.value}",
                        decorator.lineno,
                        _CONF_DECORATOR,
                    )
                )


# ---- 路径与候选构造 ----


def _join_prefix(prefix: str, route: str) -> str:
    """拼接两段路由，同时保留 Django 路由定义中的末尾斜杠。"""
    left = prefix.strip("/")
    right = route.strip("/")
    joined = f"{left}/{right}" if left and right else (left or right)
    has_trailing_slash = route.endswith("/") or (not route and prefix.endswith("/"))
    return f"{joined}/" if joined and has_trailing_slash else joined


def _normalize_django_path(prefix: str, route: str) -> str:
    """Django 路由归一：前缀拼接 + <converter:name> → {name}，保留末尾斜杠。"""
    joined = _join_prefix(prefix, route)
    converted = re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", joined)
    return f"/{converted.lstrip('/')}" if converted else "/"


def _candidate(
    repo: RepositoryInput,
    file_path: Path,
    root: Path,
    signature: str,
    line: int,
    confidence: float,
    snippet: str | None = None,
) -> dict[str, Any]:
    """构造候选条目（code_location 仓内相对路径，溯源不经 LLM）。"""
    try:
        rel = file_path.relative_to(root).as_posix()
    except ValueError:
        rel = file_path.as_posix()
    candidate: dict[str, Any] = {
        "candidate_id": "",  # 由 _extract_routes 统一编号
        "protocol": "rest",
        "signature": signature,
        "code_location": {
            "repo": repo.name,
            "file": rel,
            "start_line": max(line, 1),
            "end_line": max(line, 1),
        },
        "is_fallback": False,
        "confidence": confidence,
    }
    if snippet:
        candidate["snippet"] = snippet
    return candidate


def _node_snippet(file_path: Path | None, node: ast.AST | None) -> str | None:
    """返回受限源码片段，供 LLM 还原真实字段，读取失败则不臆造。"""
    if file_path is None or node is None:
        return None
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    lineno = getattr(node, "lineno", 0)
    if lineno <= 0:
        return None
    start = lineno - 1
    # 防止单个超长方法撑爆接口还原上下文；前 120 行涵盖参数读取与 serializer 声明。
    end = min(getattr(node, "end_lineno", lineno), start + 120, len(lines))
    snippet = "\n".join(lines[start:end]).strip()
    return snippet or None


def _serializer_context(
    root: Path,
    trees: dict[Path, ast.Module],
    view_file: Path | None,
    method_node: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> str | None:
    """解析视图方法直接实例化的仓内 Serializer，并附上受限定义片段。

    这是静态可证的上下文增强：只跟踪 `from package import XSerializer` 的直接引用，
    无法解析时安静跳过，不编造字段或跨仓库猜测。
    """
    if view_file is None or method_node is None or view_file not in trees:
        return None
    imports = _import_map(trees[view_file])
    serializer_names: list[str] = []
    for node in ast.walk(method_node):
        if not isinstance(node, ast.Call):
            continue
        name = _attr_or_id(node.func)
        if name.endswith("Serializer") and name in imports and name not in serializer_names:
            serializer_names.append(name)

    snippets: list[str] = []
    for name in serializer_names[:3]:  # 一个端点最多附带三个，控制上下文体积
        serializer_file = _resolve_import(root, trees, imports[name], name)
        if serializer_file is None:
            continue
        serializer_node = next(
            (
                item
                for item in trees[serializer_file].body
                if isinstance(item, ast.ClassDef) and item.name == name
            ),
            None,
        )
        snippet = _node_snippet(serializer_file, serializer_node)
        if snippet:
            try:
                relative = serializer_file.relative_to(root).as_posix()
            except ValueError:
                relative = serializer_file.as_posix()
            snippets.append(f"# Serializer source: {relative}\n{snippet}")
    return "\n\n".join(snippets) or None


def _error_entry(repo: RepositoryInput, message: str) -> dict[str, Any]:
    """仓库级 error 条目（X-CODE-01：节点层据此宁停不猜）。"""
    return {
        "name": repo.name,
        "path": repo.path,
        "branch": repo.branch,
        "language": "python",
        "is_fallback": repo.is_fallback,
        "status": "error",
        "file_count": 0,
        "error": {"code": "X-CODE-01", "message": message},
    }
