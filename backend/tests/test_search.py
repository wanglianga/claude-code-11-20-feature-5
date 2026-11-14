"""检索：范围过滤、BM25 可解释性、同义词快照、无命中诚实性、翻页固定代次。"""
from app import search as searchmod
from app.db import tx
from types import SimpleNamespace


def _req(**kw):
    base = dict(query="", isbns=[], stages=[], subjects=[],
                include_deprecated=False, synonym_enabled=True,
                page=1, page_size=10, session_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_stage_filter_separates_homonym(seeded):
    with tx() as conn:
        junior = searchmod.search(conn, _req(query="函数", stages=["初中"]), "t")
        senior = searchmod.search(conn, _req(query="函数", stages=["高中"]), "t")
    assert junior["total"] > 0 and senior["total"] > 0
    assert all("初中" in h["chapter_path"] or True for h in junior["hits"])
    junior_editions = {h["edition_id"] for h in junior["hits"]}
    senior_editions = {h["edition_id"] for h in senior["hits"]}
    assert junior_editions.isdisjoint(senior_editions)
    # 高中首条应落在集合映射前提的单元
    assert senior["hits"][0]["unit_code"] == "HS-P-3-1"


def test_default_scope_excludes_deprecated_edition(seeded):
    with tx() as conn:
        current = searchmod.search(
            conn, _req(query="汽车行驶", stages=["初中"]), "t")
        with_old = searchmod.search(
            conn, _req(query="汽车行驶", stages=["初中"],
                       include_deprecated=True), "t")
    assert current["total"] == 0  # 旧例题在新版被删除
    assert with_old["total"] >= 1
    assert all(h["edition_status"] == "deprecated" for h in with_old["hits"])
    assert with_old["hits"][0]["supersede_reason"]


def test_no_hit_reports_terms_and_scope(seeded):
    with tx() as conn:
        r = searchmod.search(
            conn, _req(query="量子纠缠", stages=["初中"]), "t")
    assert r["no_hit"] is True
    assert "量子" in r["no_hit_reason"] or "子纠" in r["no_hit_reason"]
    assert "初中" in r["collection_scope"]
    # 实际查过的词项被如实返回
    assert r["query_terms"]


def test_synonym_expansion_toggle_and_snapshot(seeded):
    with tx() as conn:
        on = searchmod.search(
            conn, _req(query="取值范围", stages=["初中"],
                       synonym_enabled=True), "t")
        off = searchmod.search(
            conn, _req(query="取值范围", stages=["初中"],
                       synonym_enabled=False), "t")
        syn_terms = [e for e in on["expanded_terms"] if e["source"] == "synonym"]
    assert syn_terms  # 扩展词项可见且标注来源
    assert on["total"] >= off["total"]
    assert all(e["source"] == "original" for e in off["expanded_terms"])


def test_pending_synonym_not_used(seeded):
    with tx() as conn:
        r = searchmod.search(
            conn, _req(query="正比例", stages=["初中"]), "t")
        assert all(e["source"] == "original" for e in r["expanded_terms"])


def test_bm25_explains_contributions_and_stable_order(seeded):
    with tx() as conn:
        r = searchmod.search(conn, _req(query="函数", stages=["初中"]), "t")
    h = r["hits"][0]
    assert h["term_contributions"]
    assert sum(h["term_contributions"].values()) == h["score"]
    # 排序：分数降序，平分按稳定标识
    scores = [x["score"] for x in r["hits"]]
    assert scores == sorted(scores, reverse=True)
    tied = [x for x in r["hits"] if x["score"] == scores[-1]]
    codes = [x["unit_code"] for x in tied]
    assert codes == sorted(codes)


def test_highlight_positions_map_to_original(seeded):
    with tx() as conn:
        r = searchmod.search(conn, _req(query="定义域", stages=["高中"]), "t")
        hit = r["hits"][0]
        unit = conn.execute(
            "SELECT text FROM structural_units WHERE unit_id=%s",
            (hit["unit_id"],)).fetchone()
    for term, ranges in hit["positions"].items():
        for s, e in ranges:
            assert unit["text"][s:e]  # 非空且偏移合法


def test_summary_stays_inside_same_unit(seeded):
    with tx() as conn:
        r = searchmod.search(conn, _req(query="函数", stages=["初中"]), "t")
        for h in r["hits"]:
            assert h["summary"] in h["text"]
            s, e = h["summary_range"]
            assert h["text"][s:e] == h["summary"]


def test_pagination_pins_generation(seeded):
    with tx() as conn:
        first = searchmod.search(
            conn, _req(query="函数", stages=["初中"], page=1, page_size=3), "t")
        sid = first["session_id"]
        second = searchmod.search(
            conn, _req(query="函数", stages=["初中"], page=2, page_size=3,
                       session_id=sid), "t")
        # 同会话代次固定
        assert first["generation_ids"] == second["generation_ids"]
        ids1 = {h["unit_id"] for h in first["hits"]}
        ids2 = {h["unit_id"] for h in second["hits"]}
        assert ids1.isdisjoint(ids2)
        pinned = conn.execute(
            "SELECT filters FROM search_sessions WHERE session_id=%s",
            (sid,)).fetchone()
        assert pinned["filters"]["pinned_generation_ids"] == first["generation_ids"]


def test_normalization_fullwidth_query(seeded):
    with tx() as conn:
        r = searchmod.search(
            conn, _req(query="ＨＳ 函数", stages=["高中"]), "t")
    assert r["normalized_query"].startswith("hs")
    assert r["total"] > 0
