"""受限 Python 执行沙箱。

防护分三层：
1) AST 静态校验 —— 只允许白名单模块、禁止双下划线属性、禁止 open/exec/eval/compile/__import__；
2) 独立子进程执行 —— `-I -S -B` 隔离站点配置与用户 site-packages，不继承环境变量；
3) 超时与输出上限 —— 超时即 kill，stdout 截断，防止把结果塞爆上下文。

生产部署建议再叠加容器/只读文件系统/seccomp（见 README 的部署章节）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...config import settings

ALLOWED_IMPORTS = {
    "json",
    "math",
    "statistics",
    "datetime",
    "decimal",
    "fractions",
    "re",
    "itertools",
    "functools",
    "collections",
    "heapq",
    "bisect",
    "textwrap",
    "unicodedata",
    "copy",
    "hashlib",
    "base64",
    "string",
    "typing",
    "dataclasses",
    "enum",
    "operator",
}
BANNED_ATTR_PREFIXES = ("__", "_ctypes", "gi_", "cr_")


class SandboxError(RuntimeError):
    pass


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    elapsed_ms: int
    violations: list[str]


def validate_source(code: str) -> list[str]:
    violations: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"语法错误: {exc.msg} (行 {exc.lineno})"]

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name.split(".")[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".")[0]]
            )
            for name in names:
                if name not in ALLOWED_IMPORTS:
                    violations.append(f"禁止导入模块 {name}（白名单: {', '.join(sorted(ALLOWED_IMPORTS))}）")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith(BANNED_ATTR_PREFIXES):
                violations.append(f"禁止访问内部属性 {node.attr}")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id.startswith("__"):
                violations.append(f"禁止绑定 {node.id}")
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "input", "breakpoint", "memoryview", "exit", "quit"}:
                violations.append(f"禁止调用 {name}()")
            if name == "getattr" or name == "setattr":
                violations.append(f"禁止调用 {name}() 绕过属性检查")
    return violations


_PRELUDE = """
import builtins as _b
_ALLOWED = {allowed!r}
_b.dont_write_bytecode = True
def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split('.')[0] not in _ALLOWED:
        raise ImportError('沙箱禁止导入模块: ' + name)
    return _b.__import__(name, globals, locals, fromlist, level)
_b.__import__ = _safe_import
"""


def run_python(code: str, *, timeout: float | None = None, stdin_data: str = "") -> SandboxResult:
    import time

    if not settings.sandbox_enabled:
        return SandboxResult(False, "", "沙箱已被配置禁用（FINONTO_SANDBOX_ENABLED=false）", -1, 0, [])
    violations = validate_source(code)
    if violations:
        return SandboxResult(False, "", "静态校验未通过", 1, 0, violations)

    started = time.perf_counter()
    workdir = Path(settings.sandbox_workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / f"snippet_{int(time.time() * 1000)}.py"
    script.write_text(_PRELUDE.format(allowed=sorted(ALLOWED_IMPORTS)) + "\n" + code, encoding="utf-8")
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-S", "-B", str(script)],
            capture_output=True,
            text=True,
            timeout=timeout or settings.sandbox_timeout_s,
            encoding="utf-8",
            errors="replace",
            env=env,
            input=stdin_data,
            cwd=str(workdir),
        )
        stdout = (completed.stdout or "")[: settings.sandbox_max_output]
        stderr = (completed.stderr or "")[:4000]
        ok = completed.returncode == 0
        return SandboxResult(ok, stdout, stderr, completed.returncode, int((time.perf_counter() - started) * 1000), [])
    except subprocess.TimeoutExpired:
        return SandboxResult(
            False,
            "",
            f"执行超时（>{timeout or settings.sandbox_timeout_s}s），进程已终止",
            124,
            int((time.perf_counter() - started) * 1000),
            [],
        )
    except OSError as exc:
        raise SandboxError(f"沙箱启动失败: {exc}") from exc
    finally:
        try:
            script.unlink()
        except OSError:
            pass
