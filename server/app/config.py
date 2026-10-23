"""运行配置与组件选型。

所有外部依赖（模型厂商 / Embedding / 向量库 / 对象存储 / 网络搜索 / IM）都是
`provider`，通过 `FINONTO_*` 环境变量或 `.env` 选择实现，未配置密钥时自动落到
本地可跑实现，保证零外部调用也能跑通主流程。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent
DATA_DIR_DEFAULT = SERVER_DIR / "var"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINONTO_",
        env_file=(SERVER_DIR / ".env"),
        extra="ignore",
    )

    app_name: str = "FinOnto"
    data_dir: Path = DATA_DIR_DEFAULT
    database_url: str | None = None
    auto_bootstrap: bool = True

    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ]
    )

    # --- 模型厂商 ---------------------------------------------------------
    llm_provider: str = "mock"
    llm_model: str = "qwen-plus"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_s: float = 120.0

    # --- Embedding --------------------------------------------------------
    embedding_provider: str = "local_hash"
    embedding_model: str = "text-embedding-v3"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = 384

    # --- 向量数据库 -------------------------------------------------------
    vector_store: str = "sqlite_numpy"

    # --- 对象存储 ---------------------------------------------------------
    object_store: str = "local_fs"
    oss_bucket: str = ""
    oss_endpoint: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""

    # --- 网络搜索 ---------------------------------------------------------
    web_search_provider: str = "none"
    web_search_api_key: str = ""
    web_search_endpoint: str = ""

    # --- IM 渠道 ----------------------------------------------------------
    im_channel: str = "none"
    im_webhook_url: str = ""
    im_secret: str = ""

    # --- 沙箱 -------------------------------------------------------------
    sandbox_enabled: bool = True
    sandbox_timeout_s: float = 10.0
    sandbox_max_output: int = 20_000

    # --- Agent ------------------------------------------------------------
    agent_max_steps: int = 12
    agent_step_token_budget: int = 1500

    # --- RAG --------------------------------------------------------------
    rag_top_k_bm25: int = 40
    rag_top_k_dense: int = 40
    rag_fusion: str = "rrf"
    rag_candidate_k: int = 24
    rag_final_k: int = 8
    rag_mmr_lambda: float = 0.35
    rerank_provider: str = "heuristic"

    # --- 检索/生成 ---------------------------------------------------------
    chunk_max_tokens: int = 420
    chunk_overlap_tokens: int = 60

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'finonto.db').as_posix()}"

    @property
    def object_store_root(self) -> Path:
        return self.data_dir / "objects"

    @property
    def sandbox_workdir(self) -> Path:
        return self.data_dir / "sandbox"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.object_store_root, self.sandbox_workdir, self.uploads_dir):
            Path(p).mkdir(parents=True, exist_ok=True)

    @property
    def has_remote_llm(self) -> bool:
        return self.llm_provider != "mock" and bool(self.llm_api_key or self.llm_base_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if os.environ.get("FINONTO_DATA_DIR"):
        settings.data_dir = Path(os.environ["FINONTO_DATA_DIR"])
    settings.ensure_dirs()
    return settings


settings = get_settings()
