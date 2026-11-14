"""文本归一化、中文二元组词项化与分句。

设计要点（对应需求）：
- 统一全半角与英文字母大小写，归一化前后字符数严格 1:1，
  保存 norm 位置 -> 原文位置的映射，高亮选区可无损落回原稿，
  表情符号与特殊符号不参与词项化，但仍占位，不会造成位置错移。
- 中文以字（unigram）+ 字二元组（bigram）建立倒排；短词靠原字 unigram 命中，
  不为得到结果任意扩大含义；拉丁字母与数字按整词切分。
- 长段按句拆开，句子区间使用原文字符偏移，原段稳定标识保持不变。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_FULLWIDTH_OFFSET = 0xFEE0
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))
_SENTENCE_ENDERS = "。！？；!?;\n"
_SOFT_ENDERS = "，、,．"
_ALNUM_RUN = re.compile(r"[a-z0-9]+")


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def normalize(text: str) -> tuple[str, list[int]]:
    """返回归一化文本与位置映射（mapping[norm_pos] = orig_pos）。

    全角 ASCII（含全角空格）转半角，拉丁字母统一小写；
    不删除任何字符，因此长度恒等，表情等符号原样占位。
    """
    chars: list[str] = []
    mapping: list[int] = []
    for i, ch in enumerate(text):
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            ch = " "
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII 可见字符
            ch = chr(code - _FULLWIDTH_OFFSET)
        if "A" <= ch <= "Z":
            ch = ch.lower()
        chars.append(ch)
        mapping.append(i)
    assert len(chars) == len(text) == len(mapping)
    return "".join(chars), mapping


@dataclass(frozen=True)
class Token:
    term: str
    start: int  # 归一化文本上的起止（end 不含）
    end: int
    kind: str  # cjk_uni | cjk_bi | latin


def tokenize(norm_text: str) -> list[Token]:
    tokens: list[Token] = []
    n = len(norm_text)
    # 1. 中文字与字二元组
    cjk_positions = [i for i, ch in enumerate(norm_text) if is_cjk(ch)]
    cjk_set = set(cjk_positions)
    for pos in cjk_positions:
        tokens.append(Token(norm_text[pos], pos, pos + 1, "cjk_uni"))
    for idx, pos in enumerate(cjk_positions):
        if idx + 1 < len(cjk_positions) and cjk_positions[idx + 1] == pos + 1:
            tokens.append(Token(norm_text[pos:pos + 2], pos, pos + 2, "cjk_bi"))
    # 2. 拉丁/数字整词（跳过与中文重叠的位置不可能出现，因为 is_cjk 与 alnum 互斥）
    for match in _ALNUM_RUN.finditer(norm_text):
        s, e = match.span()
        if all(i not in cjk_set for i in range(s, e)):
            tokens.append(Token(match.group(), s, e, "latin"))
    tokens.sort(key=lambda t: (t.start, t.kind))
    return tokens


def query_terms(norm_text: str) -> list[str]:
    """查询侧词项（区别于文档侧的全量 tokenize）。

    - 连续 CJK 段长度为 1（短词）：保留原字 unigram；
    - 连续 CJK 段长度 >= 2：只取相邻字二元组，避免单字泛匹配任意扩大含义；
    - 拉丁/数字段：整词。
    """
    terms: list[str] = []
    n = len(norm_text)
    i = 0
    while i < n:
        ch = norm_text[i]
        if is_cjk(ch):
            j = i
            while j < n and is_cjk(norm_text[j]):
                j += 1
            run = norm_text[i:j]
            if len(run) == 1:
                terms.append(run)
            else:
                terms.extend(run[k:k + 2] for k in range(len(run) - 1))
            i = j
        elif ch.isascii() and ch.isalnum():
            j = i
            while j < n and norm_text[j].isascii() and norm_text[j].isalnum():
                j += 1
            terms.append(norm_text[i:j])
            i = j
        else:
            i += 1
    return terms


def split_sentences(text: str, long_threshold: int) -> list[tuple[int, int]]:
    """按句切分长段，返回原文偏移区间列表 (start, end)，覆盖全文且互不重叠。"""
    if len(text) <= long_threshold:
        return [(0, len(text))] if text else []

    def hard_wrap(s: int, e: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        cur = s
        while e - cur > long_threshold:
            cut = cur + long_threshold
            out.append((cur, cut))
            cur = cut
        out.append((cur, e))
        return out

    def split_on(enders: str, s: int, e: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        cur = s
        for i in range(s, e):
            if text[i] in enders:
                out.append((cur, i + 1))
                cur = i + 1
        if cur < e:
            out.append((cur, e))
        return [seg for seg in out if seg[1] > seg[0]]

    result: list[tuple[int, int]] = []
    for s, e in split_on(_SENTENCE_ENDERS, 0, len(text)):
        if e - s <= long_threshold:
            result.append((s, e))
            continue
        for s2, e2 in split_on(_SOFT_ENDERS, s, e):
            if e2 - s2 <= long_threshold:
                result.append((s2, e2))
            else:
                result.extend(hard_wrap(s2, e2))
    return result


def fingerprint(text: str) -> str:
    """内容指纹：对原文精确文字取 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def map_norm_range_to_orig(mapping: list[int], start: int, end: int) -> tuple[int, int]:
    """归一化区间 -> 原文区间（end 转含尾字符后再 +1）。"""
    if start >= end:
        return 0, 0
    return mapping[start], mapping[end - 1] + 1
