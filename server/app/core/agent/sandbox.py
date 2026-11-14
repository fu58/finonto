"""代码沙箱：AST 白名单 + 子进程隔离 + 超时 + 输出上限。

为什么不是「直接 exec」
---------------------
在本机可信用户假设下，最危险的并不是恶意代码，而是**误操作**：删文件、起进程、
连外网。AST 白名单把「能 import 什么、能调用什么、能碰哪些名字」先卡死，
再用独立子进程 + 超时兜底。两道闸门都过不了就不执行。

允许的模块都是纯计算类（json/math/statistics/re/datetime/collections/itertools/random/textwrap），
不包含 os/sys/subprocess/socket/pathlib。
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

from ...config import get_settings

ALLOWED_MODULES = {
    "json", "math", "statistics", "re", "datetime", "collections",
    "itertools", "random", "textwrap", "decimal", "fractions", "string",
}
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "input", "breakpoint",
    "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "exit", "quit", "memoryview",
}
FORBIDDEN_ATTRS = {"__globals__", "__class__", "__bases__", "__subclasses__", "__code__", "__dict__"}
MAX_CODE_CHARS = 6000
MAX_OUTPUT_CHARS = 8000


class _Guard(ast.NodeVisitor):
    def __init__(self) -> None:
        self.blocked: list[dict[str, Any]] = []

    def _block(self, node: ast.AST, reason: str) -> None:
        self.blocked.append(
            {"reason": reason, "line": getattr(node, "lineno", 0)}
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_MODULES:
                self._block(node, f"禁止导入模块 {root}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root and root not in ALLOWED_MODULES:
            self._block(node, f"禁止导入模块 {root}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self._block(node, f"禁止使用名称 {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRS:
            self._block(node, f"禁止访问属性 {node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            self._block(node, f"禁止调用 {node.func.id}")
        self.generic_visit(node)


def validate(code: str) -> dict[str, Any]:
    if not code or not code.strip():
        return {"ok": False, "blocked": [{"reason": "代码为空", "line": 0}]}
    if len(code) > MAX_CODE_CHARS:
        return {"ok": False, "blocked": [{"reason": f"代码超过 {MAX_CODE_CHARS} 字符", "line": 0}]}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"ok": False, "blocked": [{"reason": f"语法错误：{exc.msg}", "line": exc.lineno or 0}]}
    guard = _Guard()
    guard.visit(tree)
    return {"ok": not guard.blocked, "blocked": guard.blocked}


def run(code: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.sandbox_enabled:
        return {"ok": False, "error": "沙箱已在配置中关闭（FINONTO_SANDBOX_ENABLED=false）"}

    check = validate(code)
    if not check["ok"]:
        return {
            "ok": False,
            "blocked": check["blocked"],
            "error": "代码未通过 AST 白名单检查，已拒绝执行",
        }

    import subprocess
    import sys

    # 必须转成绝对路径：子进程会 chdir 到 workdir，
    # 相对路径在那里会被二次拼接（CI 上 FINONTO_DATA_DIR 是相对路径时必现）
    workdir = Path(settings.sandbox_dir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / f"run_{int(time.time() * 1000)}.py"
    script.write_text(code, encoding="utf-8")

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(script)],
            capture_output=True,
            text=True,
            timeout=settings.sandbox_timeout_s,
            cwd=str(workdir),
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        stdout = (proc.stdout or "")[:MAX_OUTPUT_CHARS]
        stderr = (proc.stderr or "")[:MAX_OUTPUT_CHARS]
        return {
            "ok": proc.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode,
            "elapsed_ms": elapsed,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"执行超时（>{settings.sandbox_timeout_s}s），已终止",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    finally:
        try:
            script.unlink(missing_ok=True)
        except OSError:
            pass
