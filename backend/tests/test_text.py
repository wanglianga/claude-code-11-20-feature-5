"""文本归一化、词项化与分句的单元测试。"""
from app.text import (
    map_norm_range_to_orig, normalize, query_terms, split_sentences, tokenize,
)


def test_normalize_width_case_and_length_preserved():
    text = "ＡＢＣ函数ｘｙｚ123！"
    norm, mapping = normalize(text)
    assert norm == "abc函数xyz123!"
    assert len(norm) == len(text) == len(mapping)
    assert mapping == list(range(len(text)))


def test_emoji_and_symbol_keep_position():
    text = "函数😀定义"
    norm, mapping = normalize(text)
    assert len(norm) == len(text)
    # 表情占位，不改变后续字符的位置映射
    assert norm[2] == "😀"
    assert mapping[3] == 3


def test_highlight_maps_back_to_original_offsets():
    text = "Ｆｕｎ函数 f（x）"
    norm, mapping = normalize(text)
    tokens = tokenize(norm)
    first = next(t for t in tokens if t.term == "函数")
    s, e = map_norm_range_to_orig(mapping, first.start, first.end)
    assert text[s:e] == "函数"


def test_query_terms_bigram_only_for_multi_char_cjk():
    assert query_terms("函数") == ["函数"]  # 双字词不取单字
    terms = query_terms("函数的定义域")
    assert "函数" in terms and "定义" in terms and "义域" in terms
    assert "函" not in terms and "数" not in terms


def test_short_word_keeps_unigram():
    assert query_terms("函") == ["函"]


def test_latin_word_and_fullwidth_normalization():
    norm, _ = normalize("Ｙ＝ＫＸ＋ｂ")
    terms = query_terms(norm)
    assert "y" in terms and "kx" in terms and "b" in terms


def test_split_sentences_covers_full_text():
    text = "第一句。第二句！第三句？还有一段没有句号但是非常长的内容" + "甲乙丙丁" * 30
    ranges = split_sentences(text, 40)
    assert "".join(text[s:e] for s, e in ranges) == text
    assert all(e > s for s, e in ranges)
    # 区间互不重叠且有序
    for (_, e1), (s2, _) in zip(ranges, ranges[1:]):
        assert e1 == s2


def test_short_text_single_range():
    assert split_sentences("短文", 80) == [(0, 2)]
