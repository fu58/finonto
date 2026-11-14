"""核心单元测试：文本处理、切块、BM25、Markdown 渲染、沙箱、领域包、配置。

    cd server
    python -m pytest ../tests -q

集成级验证见 ``server/scripts/smoke.py``（服务层）与 ``http_smoke.py``（HTTP 层）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
os.environ.setdefault("FINONTO_DATA_DIR", str(SERVER_DIR / "var-test"))

import pytest  # noqa: E402

from app.core import text  # noqa: E402
from app.core.agent import sandbox  # noqa: E402
from app.core.ontology import packs  # noqa: E402
from app.core.rag.bm25 import BM25Index  # noqa: E402
from app.core.rag.chunking import chunk_text  # noqa: E402
from app.web.markdown import render_markdown  # noqa: E402


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #
def test_tokenize_chinese_bigrams():
    tokens = text.tokenize("设备预防性维护")
    assert "设备" in tokens
    assert "预防" in tokens
    assert "维护" in tokens


def test_tokenize_english_lowercased():
    tokens = text.tokenize("VLAN ID must be 1-4094")
    assert "vlan" in tokens
    assert "4094" in tokens


def test_split_sentences_keeps_clause_marker():
    sentences = text.split_sentences("第一条 为规范管理，制定本办法。第二条 本办法适用于全行。")
    assert len(sentences) >= 2
    assert sentences[0].startswith("第一条")


def test_extract_terms_with_suffix():
    corpus = "本办法所称授信额度，是指核定的最高融资余额。信贷合同应当载明借款金额。"
    terms = text.extract_terms(corpus, suffixes=("额度", "合同"))
    assert any("额度" in term for term in terms)
    assert any("合同" in term for term in terms)


def test_slugify_is_stable():
    assert text.slugify("信贷合同") == text.slugify("信贷合同")
    assert text.slugify("Loan Contract") == "loan-contract"
    assert len(text.slugify("")) >= 1


def test_overlap_ratio():
    assert text.overlap_ratio("信贷合同", "信贷合同管理办法") > 0.5


# --------------------------------------------------------------------------- #
# 切块
# --------------------------------------------------------------------------- #
def test_chunk_text_marks_sections():
    document = """# 第一章 总则

第一条 为规范管理制定本办法。第二条 本办法适用于全行信贷业务。

## 第二章 准入

第五条 借款人应当具备下列条件。第六条 授信应当核定额度。
"""
    chunks = chunk_text(document, size=60, overlap=10)
    assert chunks
    assert any("§" in chunk.locator for chunk in chunks)
    assert all(chunk.text.strip() for chunk in chunks)


def test_chunk_text_empty():
    assert chunk_text("   \n\n  ") == []


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #
def test_bm25_ranks_relevant_first():
    index = BM25Index().build(
        [
            (1, "信贷合同应当载明年利率与还款方式。"),
            (2, "设备点检应当每班执行一次并记录实测值。"),
            (3, "防火墙策略必须设置到期时间。"),
        ]
    )
    hits = index.search("年利率", top_k=3)
    assert hits[0].doc_id == 1


def test_bm25_empty_query():
    index = BM25Index().build([(1, "任意文本")])
    assert index.search("   ") == []


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def test_markdown_renders_table_and_heading():
    html = render_markdown("# 标题\n\n| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert "<h1" in html and "<table>" in html


def test_markdown_escapes_script():
    html = render_markdown("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_wikilink_and_citation():
    html = render_markdown("见 [[type-3]] 与 [c2]", title_of=lambda slug: "信贷合同")
    assert "信贷合同" in html
    assert 'data-tag="c2"' in html


# --------------------------------------------------------------------------- #
# 沙箱
# --------------------------------------------------------------------------- #
def test_sandbox_blocks_os_import():
    result = sandbox.validate("import os\nos.listdir('.')")
    assert result["ok"] is False
    assert result["blocked"]


def test_sandbox_blocks_dunder_access():
    result = sandbox.validate("print((1).__class__.__subclasses__())")
    assert result["ok"] is False


def test_sandbox_allows_whitelisted():
    result = sandbox.run("import json\nprint(json.dumps({'a': 1}))")
    assert result["ok"] is True
    assert "a" in result["stdout"]


# --------------------------------------------------------------------------- #
# 领域包
# --------------------------------------------------------------------------- #
def test_all_packs_declared():
    keys = {pack.key for pack in packs.all_packs()}
    assert keys == {"finance", "maintenance", "ai", "network", "general"}


def test_pack_iris_unique():
    iris = [pack.space_iri for pack in packs.all_packs()]
    assert len(iris) == len(set(iris))


def test_pack_types_have_labels():
    for pack in packs.all_packs():
        for item in pack.object_types:
            assert item.get("label"), f"{pack.key} 存在无 label 的类型"


def test_general_pack_is_empty():
    general = packs.get_pack("general")
    assert general.object_types == []
    assert general.samples == []


def test_unknown_pack_falls_back():
    assert packs.get_pack("not-a-domain").key == "general"


def test_pack_samples_exist_on_disk():
    for pack in packs.real_packs():
        for sample in pack.samples:
            assert sample.exists(pack.key), f"{pack.key}/{sample.filename} 缺失"


def test_vocab_switches_by_domain():
    finance = packs.vocab_for("finance")
    network = packs.vocab_for("network")
    assert finance["pack"] == "finance"
    assert network["pack"] == "network"
    assert finance["business_domains"] != network["business_domains"]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_env_parsing():
    from app.config import parse_env_file

    # 不用 pytest 的 tmp_path：默认落在系统临时区，企业内网常被策略禁止
    path = SERVER_DIR / "var-test" / "_env_probe.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 注释\nexport FINONTO_LLM_PROVIDER=mock\nFINONTO_CHUNK_SIZE=512\n"
        'FINONTO_CORS_ORIGINS=["http://a"]\nFINONTO_QUOTED="value"\n',
        encoding="utf-8",
    )
    try:
        values = parse_env_file(path)
    finally:
        path.unlink(missing_ok=True)
    assert values["FINONTO_LLM_PROVIDER"] == "mock"
    assert values["FINONTO_QUOTED"] == "value"


def test_coerce_types():
    from app.config import _coerce

    assert _coerce("true", False) is True
    assert _coerce("nope", True) is True
    assert _coerce("42", 0) == 42
    assert _coerce("x", 0) == 0
    assert _coerce('["a","b"]', []) == ["a", "b"]
    assert _coerce("a,b", []) == ["a", "b"]


def test_settings_defaults_are_local():
    from app.config import build_settings

    settings = build_settings()
    assert settings.llm_provider in ("mock", "openai_compat:dashscope", "openai_compat:openai")
    assert settings.vector_store in ("sqlite_numpy", "memory", "pgvector")
    assert settings.secret_flags()["llm_api_key"] in (True, False)
