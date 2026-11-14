"""集中配置：环境变量 + server/.env + var/components.json。

刻意不使用 pydantic-settings：这里只有 30 来个标量配置，一个 dataclass 足够，
而且「配置从哪来、谁覆盖了谁」在代码里一眼可见。

优先级（高 → 低）：
    1. var/components.json   —— 组件中心「设为生效」写入，重启后仍生效
    2. 进程环境变量 FINONTO_*
    3. server/.env
    4. 本文件里的默认值
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent          # server/app
SERVER_DIR = APP_DIR.parent                        # server
PROJECT_DIR = SERVER_DIR.parent                    # 仓库根

ENV_PREFIX = "FINONTO_"

#: 组件中心可切换的 10 类组件（写入 components.json）
COMPONENT_KEYS = (
    "llm_provider",
    "embedding_provider",
    "vector_store",
    "object_store",
    "doc_parser",
    "data_source",
    "web_search",
    "im_channel",
    "rerank_provider",
    "deploy_target",
)


# --------------------------------------------------------------------------- #
# .env 解析（30 行，替代 python-dotenv / pydantic-settings）
# --------------------------------------------------------------------------- #
def parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env；支持 # 注释、export 前缀、成对引号。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


_BOOL_TRUE = {"1", "true", "yes", "on", "y"}
_BOOL_FALSE = {"0", "false", "no", "off", "n"}


def _coerce(raw: str, default):
    """按默认值的类型把字符串转过去；转不动就退回默认值（配置错误不该炸服务）。"""
    if default is None:
        return raw
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        return default
    if isinstance(default, int):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw.strip())
        except ValueError:
            return default
    if isinstance(default, list):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            return default
        return [item.strip() for item in text.split(",") if item.strip()] or default
    return raw


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Settings:
    # 应用
    app_name: str = "FinOnto"
    env: str = "local"
    data_dir: str = ""                     # 空 → server/var
    database_url: str = ""                 # 空 → sqlite:///<data_dir>/finonto.db
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://127.0.0.1:8000", "http://localhost:8000"]
    )
    auto_bootstrap: bool = True
    max_upload_mb: int = 25

    # 组件：对话/推理模型
    llm_provider: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2

    # 组件：向量化
    embedding_provider: str = "local_hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 384

    # 组件：存储与解析
    vector_store: str = "sqlite_numpy"
    object_store: str = "local_fs"
    object_store_bucket: str = "finonto"
    object_store_endpoint: str = ""
    object_store_key: str = ""
    object_store_secret: str = ""
    doc_parser: str = "default_multi_format"
    data_source: str = "auto"
    web_search: str = "none"
    web_search_api_key: str = ""
    im_channel: str = "none"
    im_webhook: str = ""
    rerank_provider: str = "heuristic"
    deploy_target: str = "local"

    # 检索与生成
    chunk_size: int = 420
    chunk_overlap: int = 60
    retrieve_top_k: int = 8

    # Agent
    agent_max_steps: int = 8
    agent_step_token_budget: int = 1800
    sandbox_enabled: bool = True
    sandbox_timeout_s: int = 6

    wiki_compile_on_accept: bool = True

    # ---- 企业落地：认证 / 授权 / 审计 / 限流 ----
    auth_enabled: bool = True
    admin_username: str = "admin"
    admin_password: str = "admin123"
    session_ttl_hours: int = 12
    auth_login_max_attempts: int = 5
    auth_lock_minutes: int = 15
    cookie_secure: bool = False
    rate_limit_per_min: int = 120
    audit_retention_days: int = 180

    # ------------------------------------------------------------------ #
    # 派生路径
    # ------------------------------------------------------------------ #
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else (SERVER_DIR / "var")

    @property
    def objects_dir(self) -> Path:
        return self.data_path / "objects"

    @property
    def uploads_dir(self) -> Path:
        return self.data_path / "uploads"

    @property
    def sandbox_dir(self) -> Path:
        return self.data_path / "sandbox"

    @property
    def components_path(self) -> Path:
        return self.data_path / "components.json"

    @property
    def backups_dir(self) -> Path:
        return self.data_path / "backups"

    @property
    def db_path(self) -> Path:
        return self.data_path / "finonto.db"

    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.db_path.as_posix()}"

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.objects_dir, self.uploads_dir,
                     self.sandbox_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 密钥只回显「是否已配置」
    # ------------------------------------------------------------------ #
    def secret_flags(self) -> dict[str, bool]:
        return {
            "llm_api_key": bool(self.llm_api_key),
            "embedding_api_key": bool(self.embedding_api_key),
            "web_search_api_key": bool(self.web_search_api_key),
            "im_webhook": bool(self.im_webhook),
            "object_store_secret": bool(self.object_store_secret),
        }


def _load_component_overrides(settings: Settings) -> dict[str, str]:
    path = settings.components_path
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in COMPONENT_KEYS and v}


def build_settings() -> Settings:
    # 1) .env
    file_env = parse_env_file(SERVER_DIR / ".env")
    # 2) 进程环境变量覆盖 .env
    merged = dict(file_env)
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            merged[key] = os.environ[key]

    kwargs: dict[str, object] = {}
    for name, default in Settings.__dataclass_fields__.items():  # type: ignore[attr-defined]
        raw = merged.get(ENV_PREFIX + name.upper())
        if raw is None or raw == "":
            continue
        kwargs[name] = _coerce(raw, default.default if default.default is not None else "")
    settings = Settings(**kwargs)  # type: ignore[arg-type]

    # 3) 组件中心的选择优先级最高
    overrides = _load_component_overrides(settings)
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.ensure_dirs()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()


def reset_settings_cache() -> None:
    """组件切换后调用（仅供测试与开发热更）。"""
    get_settings.cache_clear()
