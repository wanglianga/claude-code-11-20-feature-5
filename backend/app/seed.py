"""演示数据自动播种：构建各代次、批准现行版、预置同义词与候选配置评测数据。

仅在空库时执行一次，保证幂等。场景覆盖：
- 初中数学旧版（gen1）与新版（gen2，删除旧例题）：默认现行稿为新版，旧版停用归档；
- 高中数学（同名“函数”，集合映射前提）；
- 物理册仅提交一章，模拟构建中断 -> 整本 failed，不可启用；
- 已审同义词（可直接扩展）与一条待审同义词（编辑审核流程）；
- 评测：两条配置（现行二元组切分 vs 关闭二元组的候选切分）、三个查询集，
  其中初中集复现“总体指标上升但必考章节题型下降”，物理集保留无适用索引难例。
"""
from __future__ import annotations

import json
import os
import uuid

from . import importer
from .config import settings
from .db import tx

DATA = settings.data_dir

# 候选配置评测固定重采样种子（历史评测按同一输入复现）
EVAL_SEED = 20261114
EQUIV_SCOPE = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _path(*parts: str) -> str:
    return os.path.join(DATA, *parts)


def _approve(conn, edition_id: int) -> None:
    req = conn.execute(
        "SELECT * FROM replacement_requests WHERE new_edition_id=%s "
        "AND status='pending' ORDER BY request_id", (edition_id,)).fetchone()
    if not req:
        return
    new_gen = conn.execute(
        "SELECT g.generation_id FROM index_generations g "
        "JOIN textbook_editions e ON e.edition_id=g.edition_id "
        "WHERE g.edition_id=%s AND g.status='ready'", (edition_id,)).fetchone()
    old = conn.execute(
        "SELECT e.edition_id, g.generation_id FROM textbook_editions e "
        "JOIN index_generations g ON g.edition_id=e.edition_id "
        "WHERE e.isbn=%s AND e.status='active'", (req["isbn"],)).fetchall()
    for o in old:
        conn.execute(
            "UPDATE textbook_editions SET status='deprecated', superseded_by=%s, "
            "supersede_reason=coalesce(nullif(supersede_reason,''),%s) "
            "WHERE edition_id=%s",
            (edition_id, req["reason"] or "已由编辑批准的新版替代", o["edition_id"]))
        conn.execute(
            "UPDATE index_generations SET status='archived' WHERE generation_id=%s",
            (o["generation_id"],))
    conn.execute("UPDATE textbook_editions SET status='active' WHERE edition_id=%s",
                 (edition_id,))
    conn.execute("UPDATE textbooks SET current_generation_id=%s WHERE isbn=%s",
                 (new_gen["generation_id"], req["isbn"]))
    conn.execute(
        "UPDATE replacement_requests SET status='approved', decided_by='seed', "
        "decided_at=now() WHERE request_id=%s", (req["request_id"],))


def _synonym(conn, subject: str, term: str, syn: str, status: str) -> None:
    from .text import normalize
    tn, _ = normalize(term)
    sn, sd = normalize(syn)
    conn.execute(
        "INSERT INTO synonyms"
        "(subject, term_norm, term_display, synonym_norm, synonym_display, "
        " status, created_by, reviewed_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,'seed',%s) "
        "ON CONFLICT (subject, term_norm, synonym_norm) DO NOTHING",
        (subject, tn, term, sn, syn, status,
         "seed" if status == "approved" else None),
    )


def seed_all() -> None:
    with tx() as conn:
        already = conn.execute("SELECT count(*) AS c FROM textbooks").fetchone()["c"]
    if already:
        return

    # 1. 初中数学旧版 -> 批准为现行
    r1 = importer.build_from_payload(_path("9787100000011", "payload.json"))
    with tx() as conn:
        _approve(conn, _edition_id(conn, r1["generation_id"]))
        conn.commit()

    # 2. 初中数学新版 -> 批准切换，旧版停用归档
    r2 = importer.build_from_payload(_path("9787100000011-gen2", "payload.json"))
    with tx() as conn:
        _approve(conn, _edition_id(conn, r2["generation_id"]))
        conn.commit()

    # 3. 高中数学 -> 批准为现行
    r3 = importer.build_from_payload(_path("9787100000028", "payload.json"))
    with tx() as conn:
        _approve(conn, _edition_id(conn, r3["generation_id"]))
        conn.commit()

    # 4. 物理册构建中断：只提交第 13 章，第 14 章缺失 -> 整本 failed
    importer.build_from_payload(
        _path("9787100000035", "payload.json"), chapter_codes=["W-CH13"])

    # 5. 同义词：已审两条 + 待审一条
    with tx() as conn:
        _synonym(conn, "数学", "因变量", "函数值", "approved")
        _synonym(conn, "数学", "取值范围", "定义域", "approved")
        _synonym(conn, "物理", "热运动", "无规则运动", "approved")
        _synonym(conn, "数学", "正比例", "正比关系", "pending")
        conn.commit()

    # 6. 候选配置评测：查询集、标注、配置、对比运行、待裁定分歧与停用样本
    _seed_evaluation()


def _edition_id(conn, generation_id: int) -> int:
    return conn.execute(
        "SELECT edition_id FROM index_generations WHERE generation_id=%s",
        (generation_id,)).fetchone()["edition_id"]


# ============================================================
# 候选配置评测演示数据
# ============================================================

BASELINE_SETTINGS = {
    "bm25_k1": 1.5, "bm25_b": 0.75,
    "synonym_enabled": True, "bigram_enabled": True, "top_k": 10,
}
CANDIDATE_SETTINGS = {
    "bm25_k1": 1.5, "bm25_b": 0.75,
    "synonym_enabled": True, "bigram_enabled": False, "top_k": 10,
}

JV = "八年级下册"
SV = "必修第一册"
WV = "九年级全一册"

# (查询文本, 题型, 册次, [(unit_code, grade, required, equiv_group)])
JUNIOR_QUERIES = [
    ("函数", "concept", JV, [("P-19-2", 2, False, None), ("EX-19-4", 1, False, None)]),
    ("定义域", "concept", JV, [("P-19-1", 3, False, None), ("P-19-5", 1, False, None)]),
    ("取值范围", "concept", JV,
     [("P-19-1", 2, False, EQUIV_SCOPE), ("P-19-4", 2, False, EQUIV_SCOPE)]),
    ("中位数", "concept", JV, [("P-20-1", 2, False, None), ("EX-20-1", 1, False, None)]),
    ("平均数", "concept", JV, [("P-20-1", 2, False, None), ("EX-20-1", 1, False, None)]),
    ("图象", "concept", JV,
     [("fig-FIG:19-2", 2, False, None), ("EX-19-5", 1, False, None)]),
    ("数据", "concept", JV, [("P-20-1", 2, False, None), ("P-19-5", 1, False, None)]),
    ("常数", "concept", JV, [("P-20-1", 2, False, None), ("P-19-5", 1, False, None)]),
    ("变量", "concept", JV, [("P-19-2", 2, False, None), ("P-19-1", 1, False, None)]),
    ("正比例", "concept", JV, [("P-19-5", 2, False, None), ("EX-19-4", 1, False, None)]),
    ("票房收入", "example", JV, [("EX-19-4", 3, False, None)]),
    ("一次函数", "required_chapter", JV,
     [("P-19-4", 3, True, None), ("P-19-1", 1, False, None)]),
    ("正比例函数", "required_chapter", JV,
     [("P-19-4", 3, True, None), ("EX-19-4", 1, False, None)]),
    ("函数图象", "required_chapter", JV, [("P-19-4", 3, True, None)]),
    ("因变量", "required_chapter", JV,
     [("P-19-4", 3, True, None), ("P-19-2", 2, False, None)]),
    ("常量与变量", "required_chapter", JV, [("P-19-4", 3, True, None)]),
]

SENIOR_QUERIES = [
    ("定义域", "concept", SV,
     [("HS-EX-3-2", 3, False, None), ("HS-EX-3-1", 1, False, None)]),
    ("值域", "concept", SV, [("HS-EX-3-2", 3, False, None)]),
    ("函数", "concept", SV, [("HS-P-3-1", 3, False, None)]),
    ("对应法则", "required_chapter", SV,
     [("HS-P-3-1", 3, True, None), ("HS-P-3-2", 2, False, None)]),
    ("单调性", "required_chapter", SV,
     [("HS-P-3-4", 3, True, None), ("HS-EX-3-3", 2, False, None)]),
]

# 物理册索引构建失败：两个查询在两套配置下都无适用索引，作为难例保留
PHYSICS_QUERIES = [
    ("热运动", "required_chapter", WV, [("W-P-13-1", 3, True, None)]),
    ("扩散现象", "concept", WV, [("W-EX-13-1", 2, False, None)]),
]


def _create_query_set(conn, name: str, description: str, scope: dict,
                      plan: list, creator: str = "teacher1") -> int:
    row = conn.execute(
        "INSERT INTO eval_query_sets(name, description, scope, created_by) "
        "VALUES (%s,%s,%s,%s) RETURNING set_id",
        (name, description, json.dumps(scope, ensure_ascii=False),
         creator)).fetchone()
    set_id = row["set_id"]
    for text, qtype, volume, anns in plan:
        q = conn.execute(
            "INSERT INTO eval_queries"
            "(set_id, query_text, question_type, volume, note, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING query_id",
            (set_id, text, qtype, volume, "教师按教学场景建立", creator)).fetchone()
        for unit_code, grade, required, equiv in anns:
            conn.execute(
                "INSERT INTO eval_annotations"
                "(query_id, unit_code, grade, required_source, equiv_group, annotator) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (q["query_id"], unit_code, grade, required,
                 str(equiv) if equiv else None, creator))
    return set_id


def _seed_evaluation() -> None:
    from . import evaluation as evalmod

    with tx() as conn:
        existing = conn.execute(
            "SELECT count(*) AS c FROM eval_configs").fetchone()["c"]
        if existing:
            return

        base = conn.execute(
            "INSERT INTO eval_configs(name, description, settings, status, created_by) "
            "VALUES (%s,%s,%s,'baseline','seed') RETURNING config_id",
            ("现行配置（二元组切分 + BM25 基线）",
             "线上当前使用的检索配置：多字 CJK 取相邻字二元组，BM25 k1=1.5/b=0.75",
             json.dumps(BASELINE_SETTINGS, ensure_ascii=False))).fetchone()
        cand = conn.execute(
            "INSERT INTO eval_configs(name, description, settings, status, created_by) "
            "VALUES (%s,%s,%s,'candidate','seed') RETURNING config_id",
            ("候选配置（关闭二元组，退回单字切分）",
             "切分调整试验：多字 CJK 不再生成二元组，仅按单字/拉丁整词匹配，"
             "用于评测切分修改对不同教师群体的影响",
             json.dumps(CANDIDATE_SETTINGS, ensure_ascii=False))).fetchone()
        base_id, cand_id = base["config_id"], cand["config_id"]

        junior_id = _create_query_set(
            conn, "初中数学教师评测集（主示例）",
            "常见概念查询占多数，含必考章节必需出处；复现总体上升但必考题型下降",
            {"stages": ["初中"], "subjects": ["数学"], "isbns": [],
             "include_deprecated": False}, JUNIOR_QUERIES)
        senior_id = _create_query_set(
            conn, "高中数学教师评测集",
            "同名“函数”的高中前提场景，检验集合映射相关段落",
            {"stages": ["高中"], "subjects": ["数学"], "isbns": [],
             "include_deprecated": False}, SENIOR_QUERIES)
        physics_id = _create_query_set(
            conn, "初中物理难例集（无可用索引）",
            "物理册构建中断未启用，两条查询在两套配置下都无适用索引，难例必须保留",
            {"stages": ["初中"], "subjects": ["物理"], "isbns": [],
             "include_deprecated": False}, PHYSICS_QUERIES)

        # 初中集制造一条待教研员裁定的等级分歧（同段落两个等级），随后由教研员裁定
        disp_q = conn.execute(
            "SELECT query_id FROM eval_queries WHERE set_id=%s AND query_text='定义域'",
            (junior_id,)).fetchone()
        conn.execute(
            "INSERT INTO eval_annotations"
            "(query_id, unit_code, grade, required_source, equiv_group, annotator, note) "
            "VALUES (%s,'P-19-1',1,false,NULL,'teacher2','第二位教师认为只需弱相关')",
            (disp_q["query_id"],))
        # 教研员裁定：驳回弱相关意见，维持原 3 级标注与 P-19-5 的 1 级标注
        conn.execute(
            "UPDATE eval_annotations SET status='rejected' WHERE query_id=%s",
            (disp_q["query_id"],))
        for unit_code, grade in (("P-19-1", 3), ("P-19-5", 1)):
            conn.execute(
                "INSERT INTO eval_annotations"
                "(query_id, unit_code, grade, required_source, equiv_group, "
                " annotator, note, status, adjudicated_by, adjudicated_at) "
                "VALUES (%s,%s,%s,false,NULL,'editor1','教研员裁定维持原等级',"
                "'adjudicated','editor1',now())",
                (disp_q["query_id"], unit_code, grade))

        # 冻结前两个查询集；物理集保留草稿态演示难例集管理
        evalmod.freeze_query_set(conn, junior_id)
        evalmod.freeze_query_set(conn, senior_id)

        # 初中集：完整跑完两套配置（小批次多次调用，演示可暂停续接的同一入口）
        run1 = evalmod.create_run(conn, {
            "set_id": junior_id, "baseline_config_id": base_id,
            "candidate_config_id": cand_id, "seed": EVAL_SEED,
            "bootstrap_iters": 1000, "top_k": 10, "eval_timeout_ms": 2000,
        }, "editor1")
        remaining = run1["total_queries"]
        processed = 0
        while processed < remaining:
            run1 = evalmod.process_run(conn, run1["run_id"], 4)
            processed = run1["done_queries"]
        assert run1["status"] == "completed"

        # 高中集：先跑两批后暂停，保留一个可手动续接的运行
        run2 = evalmod.create_run(conn, {
            "set_id": senior_id, "baseline_config_id": base_id,
            "candidate_config_id": cand_id, "seed": EVAL_SEED,
            "bootstrap_iters": 1000, "top_k": 10, "eval_timeout_ms": 2000,
        }, "editor1")
        evalmod.process_run(conn, run2["run_id"], 2)
        evalmod.pause_run(conn, run2["run_id"])

        # 物理难例集：冻结并跑完，no_scope 结果保留在完成统计中
        evalmod.freeze_query_set(conn, physics_id)
        run3 = evalmod.create_run(conn, {
            "set_id": physics_id, "baseline_config_id": base_id,
            "candidate_config_id": cand_id, "seed": EVAL_SEED,
            "bootstrap_iters": 500, "top_k": 10, "eval_timeout_ms": 2000,
        }, "editor1")
        evalmod.process_run(conn, run3["run_id"], 10)

        # 段落停用标记样本：一条待维护（不影响任何评测），一条已应用
        gen2 = conn.execute(
            "SELECT g.generation_id FROM index_generations g "
            "JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "WHERE e.isbn='9787100000011' AND e.generation=2").fetchone()
        conn.execute(
            "INSERT INTO eval_disabled_samples"
            "(generation_id, unit_code, reason, status, created_by, decided_by, decided_at) "
            "VALUES (%s,'P-DUP','重复标识段落，等待编辑修正后再纳入评测',"
            "'pending','teacher1',NULL,NULL)",
            (gen2["generation_id"],))
        conn.execute(
            "INSERT INTO eval_disabled_samples"
            "(generation_id, unit_code, reason, status, created_by, decided_by, decided_at) "
            "VALUES (%s,'fig-FIG:19-2','图表说明暂不参与教师查询评测',"
            "'applied','editor1','editor1',now())",
            (gen2["generation_id"],))
        conn.commit()
