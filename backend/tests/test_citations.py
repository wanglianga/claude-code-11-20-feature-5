"""引用：固定所见代次与指纹、换版漂移提示、代次不可访问时诚实失败。"""
from types import SimpleNamespace

import pytest

from app import search as searchmod
from app.db import tx


def _req(**kw):
    base = dict(query="", isbns=[], stages=[], subjects=[],
                include_deprecated=False, synonym_enabled=True,
                page=1, page_size=10, session_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _make_citation(conn, query="函数", stages=("初中",)):
    r = searchmod.search(conn, _req(query=query, stages=list(stages)), "teacher1")
    hit = r["hits"][0]
    req = SimpleNamespace(session_id=r["session_id"], unit_id=hit["unit_id"],
                          quoted_text=hit["text"][:20], locator=None)
    cit = searchmod.create_citation(conn, req, "teacher1")
    conn.commit()
    return cit, hit, r


def test_citation_created_with_fingerprint(seeded):
    with tx() as conn:
        cit, hit, _ = _make_citation(conn)
        assert cit["generation_id"] == hit["generation_id"]
        assert cit["content_hash"]
        assert cit["last_status"] == "ok"
        assert cit["locator"]["unit_code"] == hit["unit_code"]


def test_citation_rejects_text_not_in_original(seeded):
    with tx() as conn:
        r = searchmod.search(conn, _req(query="函数", stages=["初中"]), "t")
        bad = SimpleNamespace(session_id=r["session_id"],
                              unit_id=r["hits"][0]["unit_id"],
                              quoted_text="教材中根本不存在的杜撰文字", locator=None)
        with pytest.raises(ValueError):
            searchmod.create_citation(conn, bad, "teacher1")


def test_citation_drifts_after_content_change(seeded):
    with tx() as conn:
        cit, hit, _ = _make_citation(conn)
        # 模拟同一代次原文被修订（内容指纹变化）
        conn.execute(
            "UPDATE structural_units SET content_hash=%s WHERE unit_id=%s",
            ("a" * 64, hit["unit_id"]))
        checked = searchmod.check_citation(conn, cit["citation_id"])
        conn.commit()
    assert checked["last_status"] == "drifted"
    assert "指纹" in checked["last_detail"]


def test_citation_fails_when_generation_retired(seeded):
    with tx() as conn:
        cit, _, _ = _make_citation(conn)
        conn.execute(
            "UPDATE index_generations SET status='retired' WHERE generation_id=%s",
            (cit["generation_id"],))
        checked = searchmod.check_citation(conn, cit["citation_id"])
        conn.commit()
    assert checked["last_status"] == "edition_unavailable"
    assert "不会从最新版" in checked["last_detail"]


def test_citation_kept_after_new_edition_switch(seeded):
    """引用固定旧代次：新版切换后，旧代次归档仍可核对，文字与指纹保持一致。"""
    with tx() as conn:
        # 在初中旧版里检索并固定引用（显式开启停用版本）
        r = searchmod.search(
            conn, _req(query="汽车行驶", stages=["初中"],
                       include_deprecated=True), "teacher1")
        hit = next(h for h in r["hits"] if "60t" in h["text"])
        req = SimpleNamespace(session_id=r["session_id"], unit_id=hit["unit_id"],
                              quoted_text=hit["text"][:30], locator=None)
        cit = searchmod.create_citation(conn, req, "teacher1")
        checked = searchmod.check_citation(conn, cit["citation_id"])
        conn.commit()
    assert checked["last_status"] == "ok"
    assert checked["generation_id"] == hit["generation_id"]


def test_citation_cross_generation_unit_rejected(seeded):
    with tx() as conn:
        r = searchmod.search(conn, _req(query="函数", stages=["初中"]), "t")
        other = conn.execute(
            "SELECT unit_id FROM structural_units WHERE generation_id <> ALL(%s) LIMIT 1",
            (r["generation_ids"],)).fetchone()
        if other:
            req = SimpleNamespace(session_id=r["session_id"],
                                  unit_id=other["unit_id"],
                                  quoted_text="x", locator=None)
            with pytest.raises(PermissionError):
                searchmod.create_citation(conn, req, "teacher1")
