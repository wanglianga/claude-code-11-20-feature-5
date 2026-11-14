"""构建管线：结构边界、待修正问题、逐章报告、构建中断与隔离切换。"""
import os

from app import importer
from app.config import settings
from app.db import tx

DATA = settings.data_dir


def test_build_indexes_units_and_keeps_example_independent(seeded):
    with tx() as conn:
        gen = conn.execute(
            "SELECT * FROM index_generations WHERE generation_id=%s",
            (seeded["gen2"]["generation_id"],)).fetchone()
        assert gen["status"] == "ready"
        types = conn.execute(
            "SELECT unit_type, count(*) c FROM structural_units "
            "WHERE generation_id=%s GROUP BY 1",
            (gen["generation_id"],)).fetchall()
        counts = {r["unit_type"]: r["c"] for r in types}
        assert counts.get("example", 0) >= 1
        assert counts.get("paragraph", 0) >= 1
        # 例题是独立单元，不与段落合并
        ex = conn.execute(
            "SELECT * FROM structural_units WHERE generation_id=%s AND unit_type='example' "
            "LIMIT 1", (gen["generation_id"],)).fetchone()
        assert ex["unit_code"].startswith("EX")


def test_parse_issues_collected(seeded):
    with tx() as conn:
        rows = conn.execute(
            "SELECT issue_type, count(*) c FROM parse_issues GROUP BY 1").fetchall()
        issues = {r["issue_type"]: r["c"] for r in rows}
    assert issues["missing_figure"] >= 1       # 新版 FIG:19-9 缺图
    assert issues["duplicate_id"] >= 1         # P-DUP 重复标识
    assert issues["missing_premise"] >= 1      # 旧版 EX-19-2 缺前提
    assert issues["chapter_error"] >= 1        # 物理册构建中断


def test_duplicate_unit_not_indexed(seeded):
    with tx() as conn:
        # 重复标识只保留第一条
        rows = conn.execute(
            "SELECT count(*) c FROM structural_units WHERE unit_code='P-DUP'").fetchall()
        assert rows[0]["c"] == 1


def test_chapter_reports_four_stages(seeded):
    with tx() as conn:
        stages = {r["stage"] for r in conn.execute(
            "SELECT DISTINCT stage FROM index_chapter_reports").fetchall()}
    assert {"received", "parsed", "terms", "verified"}.issubset(stages)


def test_interrupted_build_fails_and_not_usable(seeded):
    report = seeded["broken"]
    assert report["status"] == "failed"
    assert report["failed_chapters"] == 1
    assert "不齐全" in report["error"]
    with tx() as conn:
        gen = conn.execute(
            "SELECT status FROM index_generations WHERE generation_id=%s",
            (report["generation_id"],)).fetchone()
        assert gen["status"] == "failed"
        # 失败代次不产生倒排统计
        stats = conn.execute(
            "SELECT count(*) c FROM index_stats WHERE generation_id=%s",
            (report["generation_id"],)).fetchone()
        assert stats["c"] == 0


def test_long_paragraph_split_keeps_stable_id(seeded):
    with tx() as conn:
        unit = conn.execute(
            "SELECT * FROM structural_units WHERE unit_code='P-19-LONG'").fetchone()
        assert unit is not None
        assert len(unit["sentence_ranges"]) > 1
        full = unit["text"]
        assert "".join(full[s:e] for s, e in unit["sentence_ranges"]) == full


def test_generation_isolation_no_mixed_versions(seeded):
    with tx() as conn:
        rows = conn.execute(
            "SELECT generation_id, count(DISTINCT edition_id) editions "
            "FROM structural_units GROUP BY generation_id").fetchall()
        for r in rows:
            assert r["editions"] == 1  # 一个代次只属于一个版本，不新旧混拼


def test_example_conditions_persisted_independently(seeded):
    with tx() as conn:
        ex = conn.execute(
            "SELECT * FROM structural_units WHERE unit_code='EX-19-1' LIMIT 1").fetchone()
        assert ex["unit_type"] == "example"
        assert len(ex["conditions"]) == 2
        assert any("60 千米" in c for c in ex["conditions"])
        missing = conn.execute(
            "SELECT * FROM structural_units WHERE unit_code='EX-19-2' LIMIT 1").fetchone()
        assert missing["conditions"] == []


def test_rebuild_same_generation_rejected(seeded):
    import pytest
    with pytest.raises(importer.BuildError):
        importer.build_from_payload(os.path.join(DATA, "9787100000028", "payload.json"))
