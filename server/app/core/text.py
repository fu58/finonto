"""中英混合文本处理：分词、句子切分、术语抽取、相似度。

离线的 Mock LLM、查询理解、BM25 与本体候选抽取共用这里的原语，保证无外部模型时
链路仍然产出有意义的结果。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

CJK = r"一-鿿"
LATIN = r"A-Za-z0-9_\-·"

STOPWORDS = {
    "我们", "你们", "他们", "可以", "应当", "必须", "本", "该", "其", "以及", "并且", "或者",
    "对于", "通过", "根据", "按照", "如果", "则", "为", "是", "的", "了", "在", "和", "与",
    "及", "等", "中", "上", "下", "由", "以", "到", "从", "被", "将", "请", "说明", "要求",
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "in", "on", "for", "with",
    "that", "this", "as", "by", "be", "it", "from", "which", "shall", "must", "may",
}

# 领域后缀，用于中文术语边界判定与本体候选打分
DOMAIN_SUFFIXES = (
    "类型", "实体", "产品", "业务", "合约", "协议", "账户", "机构", "组织", "客户",
    "事件", "凭证", "单据", "记录", "规则", "指标", "模型", "参数", "要素", "资产",
    "负债", "权益", "风险", "限额", "等级", "分类", "标准", "规范", "条款", "费率",
    "利率", "期限", "金额", "币种", "市场", "交易", "清算", "结算", "质押", "担保",
    "授信", "贷款", "票据", "债券", "基金", "保险", "保单", "理赔", "评级", "敞口",
    "额度", "状况", "代码", "名称", "信息", "材料", "流水", "身份", "主体", "义务",
    "权利", "责任", "借款", "融资", "投资", "存款", "抵押", "质权", "流程", "手续",
)

# 义务/流程句式中的连接成分：候选术语里出现它们，基本意味着切到了句子片段
CLAUSE_MARKERS = (
    "应当", "不得", "必须", "可以", "需要", "及时", "采取", "进行", "以及", "并且",
    "其中", "根据", "对于", "所称", "属于", "出现", "报送", "一次性", "或者", "直至",
    "以下", "之一", "并在", "不得将", "应当自", "自交易", "发现", "即使", "下列",
    "上述", "下述", "甚至", "至少", "按照", "基于", "运用", "加以", "分别", "相应",
)

# 虚词/动词起首：三字以上的 gram 以它们开头时几乎不可能是名词术语
FUNCTION_HEADS = set("当其该且或及并对将从至被在是也均已未应须得可把让由于如若除另各这那些为以")

SENT_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")
CJK_RUN = re.compile(f"[{CJK}]+")
TERM_BOUNDARY = re.compile("[的与及等之在或而]")
LATIN_RUN = re.compile(f"[{LATIN}]+")
# 英文术语候选：允许 .（802.1Q）、+（C++）、数字（IPv4），长度受限避免吃掉整句
LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{1,20}")
DEF_CUE = re.compile(
    rf"([{CJK}]{{2,20}})(?:是指|指的是|定义为|是指经|称为|叫做)"
    rf"([^。；;!?\n]{{4,160}})"
    rf"(?:的)?([{CJK}]{{1,8}}?)"
    rf"(?:类型|实体|产品|业务|合约|账户|机构|客户|事件|规则|指标|模型|要素|资产|风险|等级|分类|凭证|单据|协议|条款|利率|期限|金额|币种|交易|额度)"
)
ALIAS_CUE = re.compile(rf"([{CJK}]{{2,20}})[，,]?（([^）{{}}]{{2,30}})）")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENT_SPLIT.split(text or "") if p and p.strip()]
    return [p for p in parts if len(p) > 1]


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """中文按 1~3gram + 英文数字按词切分，输出可直接喂 BM25 的 token 序列。"""
    out: list[str] = []
    low = (text or "").lower()
    for m in CJK_RUN.finditer(low):
        seg = m.group()
        n = len(seg)
        for size in (2, 3):
            for i in range(n - size + 1):
                g = seg[i : i + size]
                if keep_stopwords or g not in STOPWORDS:
                    out.append(g)
        if n == 1 and keep_stopwords:
            out.append(seg)
    for m in LATIN_RUN.finditer(low):
        w = m.group()
        if keep_stopwords or w not in STOPWORDS:
            out.append(w)
    return out


def content_tokens(text: str) -> list[str]:
    return tokenize(text)


def token_weight_map(text: str) -> dict[str, float]:
    counts = Counter(tokenize(text))
    if not counts:
        return {}
    top = max(counts.values())
    return {t: c / top for t, c in counts.items()}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a: str, b: str) -> float:
    """token 覆盖率：a 的 token 有多少出现在 b 中。"""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


def cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in common)
    na = math.sqrt(sum(v * v for v in vec_a.values()))
    nb = math.sqrt(sum(v * v for v in vec_b.values()))
    return dot / (na * nb) if na and nb else 0.0


def tfidf_vector(text: str, idf: dict[str, float]) -> dict[str, float]:
    counts = Counter(tokenize(text))
    vec: dict[str, float] = {}
    for tok, c in counts.items():
        vec[tok] = (1 + math.log(c)) * idf.get(tok, 1.0)
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def candidate_terms(
    text: str,
    *,
    max_terms: int = 40,
    extra_suffixes: tuple[str, ...] = (),
    extra_markers: tuple[str, ...] = (),
) -> list[tuple[str, float]]:
    """从文本中抽取候选术语（术语化 n-gram），附带启发式得分。

    中文没有空格，纯 n-gram 会产出「款人的风险状况」这类跨词片段，所以这里
    用两道判定：助词（的/与/和…）处视为硬边界，不允许成词；从未出现在片段
    开头的 gram 判为长词截断，大幅降权。`extra_suffixes` / `extra_markers`
    由所属领域包注入，让不同领域的名词后缀与句式标记都能生效。
    """
    suffixes = DOMAIN_SUFFIXES + tuple(extra_suffixes)
    markers = CLAUSE_MARKERS + tuple(extra_markers)
    scores: dict[str, float] = defaultdict(float)
    sentences = split_sentences(text)
    freq: Counter[str] = Counter()
    left_chars: dict[str, set[str]] = defaultdict(set)
    for sent in sentences:
        for seg in CJK_RUN.finditer(sent):
            for run in TERM_BOUNDARY.split(seg.group()):
                for size in range(2, min(8, len(run)) + 1):
                    for i in range(len(run) - size + 1):
                        gram = run[i : i + size]
                        if gram in STOPWORDS:
                            continue
                        freq[gram] += 1
                        left_chars[gram].add("" if i == 0 else run[i - 1])
    for gram, c in freq.items():
        base = math.log(1 + c) * (1 + 0.35 * (len(gram) - 2))
        suffixed = gram.endswith(suffixes)
        if suffixed:
            base *= 2.2
        if DEF_CUE.pattern and re.search(rf"{re.escape(gram)}[^。]*?(是指|指的是|定义为|包括)", text):
            base *= 1.8
        if "" not in left_chars[gram]:
            # 从未出现在片段开头 => 只是长词的内部截断，例如「款人」「款人的风险」
            base *= 0.2
        if any(marker in gram for marker in markers):
            base *= 0.12
        elif len(gram) >= 3 and gram[0] in FUNCTION_HEADS:
            base *= 0.25
        elif len(gram) >= 5 and not suffixed:
            # 长 gram 既没有名词后缀又没命中句式标记，通常是动词短语
            base *= 0.5
        scores[gram] = base
    # 英文与缩写术语：计算机网络、人工智能领域的概念大量以大写形式出现
    for token, c in Counter(LATIN_TERM.findall(text or "")).items():
        if c < 2 or token.lower() in STOPWORDS:
            continue
        # 只保留缩写（全大写）或驼峰/带数字的写法，过滤 string、boolean 这类字段类型词
        if not (token.isupper() or re.search(r"[A-Z]", token[1:]) or any(ch.isdigit() for ch in token)):
            continue
        scores[token] = math.log(1 + c) * 2.4
    # 去掉被更高频长词包含的短词
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    kept: list[tuple[str, float]] = []
    for term, score in ordered:
        if any(term in k and score < s * 1.25 for k, s in kept):
            continue
        if any(k in term and s >= score for k, s in kept):
            continue
        kept.append((term, score))
        if len(kept) >= max_terms:
            break
    return kept


def extract_definitions(text: str, *, limit: int = 12) -> list[dict[str, str]]:
    """借助中文定义句式抽取 (术语, 定义) 对。"""
    found: dict[str, dict[str, str]] = {}
    for sent in split_sentences(text):
        for cue in ("是指", "指的是", "定义为", "被称为", "统称为"):
            if cue in sent:
                term, _, rest = sent.partition(cue)
                term = term.strip("，,、。；;：:（）()  ")
                if 2 <= len(term) <= 24:
                    rest = rest.strip("，,。；;：: ")
                    found.setdefault(term, {"term": term, "definition": rest, "sentence": sent})
                break
        else:
            continue
        if len(found) >= limit:
            break
    return list(found.values())


def extract_aliases(text: str) -> dict[str, list[str]]:
    mapping: dict[str, set[str]] = defaultdict(set)
    for m in ALIAS_CUE.finditer(text or ""):
        term, alias = m.group(1).strip(), m.group(2).strip()
        if 2 <= len(term) <= 24 and 2 <= len(alias) <= 30 and not alias.startswith("如"):
            mapping[term].add(alias)
    return {k: sorted(v) for k, v in mapping.items()}


def numeric_constraints(text: str) -> list[dict[str, str]]:
    """抽取数值型约束（阈值 / 比例 / 时限），制度与标准文本里跨领域都高价值的信号。"""
    unit = "(%|倍|天|个月|年|小时|分钟|毫秒|秒|万元|亿元|元|次|台|公里)"
    patterns = [
        (rf"(不低于|不得高于|不得超过|不超过|不少于|超过|达到)\s*(\d+(?:\.\d+)?){unit}", "range"),
        (rf"(应当|必须|需要)\s*(\d+(?:\.\d+)?){unit}", "mandate"),
        (rf"(\d+(?:\.\d+)?){unit}[^。]{{0,12}}(以内|以下|以上)", "threshold"),
    ]
    out: list[dict[str, str]] = []
    for pat, kind in patterns:
        for m in re.finditer(pat, text or ""):
            out.append(
                {
                    "kind": kind,
                    "match": m.group(0).strip(),
                    "value": m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1),
                    "unit": m.group(3) if m.lastindex and m.lastindex >= 3 else "",
                }
            )
    return out


def truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def strip_md(text: str) -> str:
    text = re.sub(r"`{1,3}[^`]*`{1,3}", " ", text or "")
    text = re.sub(r"[*_#>|]+", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()
