"""中英混合文本处理：分词、切句、术语抽取、slug 与相似度。

为什么自己写而不装 jieba
-----------------------
1. 只有一处依赖（BM25 与 mock 模型），jieba 会带来词典加载与体积成本；
2. 本项目的检索下限由「二元词 + 短语」保证即可，真正提质量的是混合检索和重排；
3. 零依赖是「一条命令跑起来」的前提之一。

分词策略
--------
* 英文/数字：按 ``[A-Za-z][A-Za-z0-9_+.\\-]*`` 切，转小写。
* 中文：连续 CJK 串同时产出 **单字 + 二元词**。单字保证召回，二元词提供区分度，
  这是中文小语料下最稳的折中（``设备`` ``维保`` ``预防性`` 都会被命中）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_+.\-]*")
NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?%?")
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+(?=[A-Z])|\n{2,}")

#: 句首/词首常出现的连接词与助词，术语抽取时要剥掉
BOUNDARY_PARTICLES = (
    "的", "了", "和", "与", "及", "或", "等", "在", "对", "按", "将", "把", "被",
    "由", "为", "以", "于", "从", "向", "并", "且", "而", "但", "则", "即", "其",
    "该", "本", "此", "各", "每", "所", "是", "有", "应", "须", "可", "需", "不",
    "并应", "并且", "或者", "以及", "对于", "关于", "根据", "按照", "依据",
)

#: 中文停用词（用于查询理解与关键词打分，不参与术语判定）
STOPWORDS = set(
    """的 了 和 与 及 或 等 在 对 按 将 把 被 由 为 以 于 从 向 并 且 而 但 则 即 其 该 本 此 各 每 所 是 有
    什么 如何 怎样 怎么 哪些 哪个 多少 是否 为什么 请 问 一下 我们 你们 他们 它 这 那 这些 那些
    一个 一种 可以 需要 应该 必须 进行 通过 关于 对于 根据 按照 依据 如果 那么 因此 所以 但是
    以及 或者 并且 其中 相关 有关 上述 如下 以下 第 条 款 项 目 中 上 下 内 外
    the a an of to in for on with and or is are be as at by that this these those from""".split()
)


# --------------------------------------------------------------------------- #
# 归一化与分词
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """全角转半角 + 压缩空白 + 去零宽字符。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    """返回小写 token 列表，供 BM25 与关键词召回使用。"""
    if not text:
        return []
    text = normalize(text)
    tokens: list[str] = []

    for match in EN_TOKEN.finditer(text):
        token = match.group(0).lower().strip(".-")
        if token:
            tokens.append(token)

    for match in NUM_TOKEN.finditer(text):
        tokens.append(match.group(0))

    for match in CJK_RUN.finditer(text):
        run = match.group(0)
        tokens.extend(run)                                   # 单字：保召回
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # 二元词：保区分度

    return [t for t in tokens if t not in STOPWORDS]


def term_set(text: str) -> set[str]:
    """去重后的 token 集合，用于覆盖率、Jaccard 等轻量相似度。"""
    return set(tokenize(text))


# --------------------------------------------------------------------------- #
# 切句与切段
# --------------------------------------------------------------------------- #
def split_sentences(text: str, max_len: int = 400) -> list[str]:
    """按中英文句末标点切句；超长句按逗号再切一次。"""
    text = normalize(text)
    if not text:
        return []
    pieces: list[str] = []
    for part in SENT_SPLIT.split(text):
        if part is None:
            continue
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_len:
            pieces.append(part)
            continue
        buffer = ""
        for clause in re.split(r"(?<=[，,、])", part):
            if len(buffer) + len(clause) > max_len and buffer:
                pieces.append(buffer.strip())
                buffer = clause
            else:
                buffer += clause
        if buffer.strip():
            pieces.append(buffer.strip())
    return [p for p in pieces if p]


def split_paragraphs(text: str) -> list[str]:
    text = normalize(text)
    return [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*(?:第[一二三四五六七八九十]+[章节条]|\d+[.、]))", text) if p.strip()]


# --------------------------------------------------------------------------- #
# 术语抽取（mock 模型与语义候选共用）
# --------------------------------------------------------------------------- #
def _strip_particles(term: str) -> str:
    changed = True
    while changed and len(term) > 2:
        changed = False
        for particle in BOUNDARY_PARTICLES:
            if term.startswith(particle) and len(term) - len(particle) >= 2:
                term = term[len(particle):]
                changed = True
                break
            if term.endswith(particle) and len(term) - len(particle) >= 2:
                term = term[: -len(particle)]
                changed = True
                break
    return term


def extract_terms(
    text: str,
    *,
    suffixes: tuple[str, ...] | list[str] = (),
    cue_markers: tuple[str, ...] | list[str] = (),
    max_terms: int = 24,
    min_len: int = 2,
    max_len: int = 16,
) -> list[str]:
    """从文本中抽取候选术语。

    三条线索（与领域包解耦，具体词表由调用方从 DomainPack 传入）：
    1. **后缀线索**：以 ``合同/协议/账户/规程/模型/协议栈`` 等结尾的连续中文串；
    2. **句式线索**：``X是指…`` / ``X指…`` / ``所谓X`` / ``X，是指`` 这类定义句；
    3. **标记线索**：``第X条``、``§``、``[标签]`` 之后紧跟的名词短语。

    返回按出现频次与长度排序的去重列表（不做词性标注，避免引入模型依赖）。
    """
    text = normalize(text)
    if not text:
        return []
    scores: Counter[str] = Counter()

    # 1) 后缀线索
    for suffix in suffixes:
        if not suffix:
            continue
        for match in re.finditer(rf"([\u4e00-\u9fff]{{1,{max(1, max_len - len(suffix))}}}{re.escape(suffix)})", text):
            candidate = _strip_particles(match.group(1))
            if min_len <= len(candidate) <= max_len:
                scores[candidate] += 2 + (2 if suffix in ("合同", "协议", "账户", "模型") else 1)

    # 2) 句式线索：X 是指 / X 指 / 所谓 X / X，是指
    for pattern in (
        r"([\u4e00-\u9fff]{2,12})(?:是指|指的是|系指)",
        r"所谓([\u4e00-\u9fff]{2,12})",
        r"([\u4e00-\u9fff]{2,12})[，,]\s*(?:是指|指的是)",
        r"([A-Za-z][A-Za-z0-9_\- ]{2,24})\s*[（(]([^）)]{2,20})[）)]",
    ):
        for match in re.finditer(pattern, text):
            candidate = _strip_particles(match.group(1).strip())
            if min_len <= len(candidate) <= max_len:
                scores[candidate] += 4

    # 3) 标记线索：第X条 / § 之后的短语
    for marker in cue_markers or ("第", "§", "附则", "规定"):
        pattern = rf"{re.escape(marker)}[^。；\n]{{0,24}}?([\u4e00-\u9fff]{{2,10}})"
        for match in re.finditer(pattern, text):
            candidate = _strip_particles(match.group(1))
            if min_len <= len(candidate) <= max_len:
                scores[candidate] += 1

    ranked = sorted(
        scores.items(),
        key=lambda item: (item[1], len(item[0])),
        reverse=True,
    )
    seen: set[str] = set()
    out: list[str] = []
    for term, _score in ranked:
        key = term.lower()
        if key in seen:
            continue
        # 跳过纯停用词组合与纯数字
        if term.isdigit():
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= max_terms:
            break
    return out


# --------------------------------------------------------------------------- #
# 标识符与相似度
# --------------------------------------------------------------------------- #
_EN_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "on", "with", "by"}


def slugify(label: str, *, fallback: str = "item", max_len: int = 60) -> str:
    """把任意标签转成 URL/IRI 友好的 slug。

    中文标签无法直接进 IRI，这里用「拼音不可靠、哈希更稳定」的折中：
    保留 ASCII 单词，其余用稳定的短哈希后缀，保证 **同一标签永远得到同一 slug**。
    """
    label = normalize(label)
    ascii_part = "-".join(
        w.lower() for w in re.findall(r"[A-Za-z0-9]+", label) if w.lower() not in _EN_STOP
    )
    cjk = "".join(CJK_RUN.findall(label))
    if ascii_part:
        slug = ascii_part[:max_len]
    elif cjk:
        # 用字符的 code point 和做稳定短码，便于人读「这是中文来的」
        digest = sum(ord(ch) * (i + 1) for i, ch in enumerate(cjk)) % 100_000
        slug = f"zh-{digest:05d}"
    else:
        slug = fallback
    return slug[:max_len].strip("-") or fallback


def overlap_ratio(a: str, b: str) -> float:
    """a 的 token 有多少比例出现在 b 中（用于相似类型提示）。"""
    ta, tb = term_set(a), term_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta)


def jaccard(a: str, b: str) -> float:
    ta, tb = term_set(a), term_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def truncate(text: str, limit: int = 160, suffix: str = "…") -> str:
    text = normalize(text).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - len(suffix)] + suffix


def has_cjk(text: str) -> bool:
    return bool(CJK_RUN.search(text))
