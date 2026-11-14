"""候选配置评测：指标、等价集合、仲裁、难例保留、小样本、重采样区间、
暂停续接、快照复现、兼容核对、权限与普通检索不受试验影响。"""
import json

import pytest

from app import evaluation as E
from app.db import tx

BASE = {"bm25_k1": 1.5, "bm25_b": 0.75, "synonym_enabled": True,
        "bigram_enabled": True, "top_k": 10}
CAND = dict(BASE, bigram_enabled=False)
JV = "八年级下册"
SV = "必修第一册"
EG = "22222222-2222-2222-2222-222222222222"


# ------------------------------------------------------------ 纯函数

def test_bigram_toggle_changes_terms():
    terms_on = E.config_query_terms("正比例函数", True)
    terms_off = E.config_query_terms("正比例函数", False)
    assert "正比" in terms_on and "例函" in terms_on
    assert all(len(t) == 1 for t in terms_off if t.isalpha())
    assert set(terms_off) == set("正比例函数")


def test_score_query_recall_ndcg_and_required():
    items = E.build_relevance_items([
        {"unit_code": "A", "grade": 3, "required_source": True, "equiv_group": None},
        {"unit_code": "B", "grade": 1, "required_source": False, "equiv_group": None},
    ])
    m = E.score_query(["X", "A", "B"], items, top_k=3)
    assert m["recall_at_k"] == 1.0
    assert 0 < m["ndcg_at_k"] <= 1
    assert m["required_fulfilled"] is True
    assert m["required_recall"] == 1.0

    m2 = E.score_query(["X", "B"], items, top_k=3)
    assert m2["recall_at_k"] == 0.5
    assert m2["required_fulfilled"] is False
    assert m2["required_recall"] == 0.0

    m3 = E.score_query([], items, top_k=3)
    assert m3["no_result"] is True and m3["recall_at_k"] == 0.0


def test_equivalent_group_is_a_set_not_first_annotation_only():
    items = E.build_relevance_items([
        {"unit_code": "A1", "grade": 2, "required_source": False, "equiv_group": EG},
        {"unit_code": "A2", "grade": 2, "required_source": False, "equiv_group": EG},
    ])
    assert len(items) == 1
    assert items[0]["units"] == {"A1", "A2"}
    # 命中等价集合中的任一成员都算覆盖，不能只认第一个标注 A1
    assert E.score_query(["A2"], items, 5)["recall_at_k"] == 1.0
    assert E.score_query(["A1"], items, 5)["recall_at_k"] == 1.0
    assert E.score_query(["B9"], items, 5)["recall_at_k"] == 0.0


def test_paired_bootstrap_fixed_seed_and_cross_zero_not_significant():
    diffs = [0.1, -0.2, 0.15, 0.05, -0.1, 0.2, 0.0, 0.1, -0.05, 0.12]
    r1 = E.paired_bootstrap(diffs, seed=20261114, iters=500)
    r2 = E.paired_bootstrap(diffs, seed=20261114, iters=500)
    assert r1 == r2  # 固定种子可复现
    assert r1["method"] == "paired-percentile-bootstrap(seed=20261114, B=500, n=10)"
    assert r1["sample_size"] == 10
    assert r1["ci_low"] <= r1["point"] <= r1["ci_high"]
    # 区间跨零不得宣布显著获胜
    assert r1["significant"] is False and r1["direction"] == "inconclusive"

    win = E.paired_bootstrap([0.2] * 12, seed=7, iters=300)
    assert win["significant"] is True and win["direction"] == "candidate_better"
    lose = E.paired_bootstrap([-0.2] * 12, seed=7, iters=300)
    assert lose["direction"] == "candidate_worse"


# ------------------------------------------------------------ 夹具辅助

def _make_set(conn, name, scope, plan):
    sid = conn.execute(
        "INSERT INTO eval_query_sets(name, scope, created_by) VALUES (%s,%s,'teacher1') "
        "RETURNING set_id",
        (name, json.dumps(scope, ensure_ascii=False))).fetchone()["set_id"]
    for text, qtype, volume, anns in plan:
        qid = conn.execute(
            "INSERT INTO eval_queries(set_id, query_text, question_type, volume, created_by) "
            "VALUES (%s,%s,%s,%s,'teacher1') RETURNING query_id",
            (sid, text, qtype, volume)).fetchone()["query_id"]
        for unit, grade, req, equiv in anns:
            conn.execute(
                "INSERT INTO eval_annotations"
                "(query_id, unit_code, grade, required_source, equiv_group, annotator) "
                "VALUES (%s,%s,%s,%s,%s,'teacher1')",
                (qid, unit, grade, req, str(equiv) if equiv else None))
    return sid


JUNIOR_SCOPE = {"stages": ["初中"], "subjects": ["数学"], "isbns": [],
                "include_deprecated": False}

MAIN_PLAN = [
    ("函数", "concept", JV, [("P-19-2", 2, False, None), ("EX-19-4", 1, False, None)]),
    ("定义域", "concept", JV, [("P-19-1", 3, False, None), ("P-19-5", 1, False, None)]),
    ("取值范围", "concept", JV,
     [("P-19-1", 2, False, EG), ("P-19-4", 2, False, EG)]),
    ("中位数", "concept", JV, [("P-20-1", 2, False, None), ("EX-20-1", 1, False, None)]),
    ("平均数", "concept", JV, [("P-20-1", 2, False, None), ("EX-20-1", 1, False, None)]),
    ("图象", "concept", JV, [("fig-FIG:19-2", 2, False, None), ("EX-19-5", 1, False, None)]),
    ("数据", "concept", JV, [("P-20-1", 2, False, None), ("P-19-5", 1, False, None)]),
    ("常数", "concept", JV, [("P-20-1", 2, False, None), ("P-19-5", 1, False, None)]),
    ("变量", "concept", JV, [("P-19-2", 2, False, None), ("P-19-1", 1, False, None)]),
    ("正比例", "concept", JV, [("P-19-5", 2, False, None), ("EX-19-4", 1, False, None)]),
    ("票房收入", "example", JV, [("EX-19-4", 3, False, None)]),
    ("一次函数", "required_chapter", JV, [("P-19-4", 3, True, None), ("P-19-1", 1, False, None)]),
    ("正比例函数", "required_chapter", JV, [("P-19-4", 3, True, None), ("EX-19-4", 1, False, None)]),
    ("函数图象", "required_chapter", JV, [("P-19-4", 3, True, None)]),
    ("因变量", "required_chapter", JV, [("P-19-4", 3, True, None), ("P-19-2", 2, False, None)]),
    ("常量与变量", "required_chapter", JV, [("P-19-4", 3, True, None)]),
]


def _configs(conn):
    b = conn.execute(
        "INSERT INTO eval_configs(name, settings, created_by) VALUES ('基线',%s,'editor1') "
        "RETURNING config_id", (json.dumps(BASE),)).fetchone()["config_id"]
    c = conn.execute(
        "INSERT INTO eval_configs(name, settings, created_by) VALUES ('候选',%s,'editor1') "
        "RETURNING config_id", (json.dumps(CAND),)).fetchone()["config_id"]
    return b, c


def _run_to_completion(conn, run_id, batch=4):
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                       (run_id,)).fetchone()
    for _ in range(run["total_queries"] + 2):
        run = E.process_run(conn, run_id, batch)
        if run["status"] == "completed":
            return run
    raise AssertionError("评测未在预期批次内完成")


# ------------------------------------------------------------ 冻结与仲裁

def test_freeze_rejects_unannotated_and_conflicts(seeded):
    with tx() as conn:
        sid = _make_set(conn, "s1", JUNIOR_SCOPE, [
            ("函数", "concept", JV, [("P-19-2", 2, False, None)]),
            ("定义域", "concept", JV, []),
        ])
        with pytest.raises(E.EvalError) as exc:
            E.freeze_query_set(conn, sid)
        assert "定义域" in str(exc.value)

        # 补标注后制造同段落等级分歧
        qid = conn.execute(
            "SELECT query_id FROM eval_queries WHERE set_id=%s AND query_text='定义域'",
            (sid,)).fetchone()["query_id"]
        conn.execute(
            "INSERT INTO eval_annotations(query_id, unit_code, grade, annotator) "
            "VALUES (%s,'P-19-1',3,'teacher1')", (qid,))
        conn.execute(
            "INSERT INTO eval_annotations(query_id, unit_code, grade, annotator) "
            "VALUES (%s,'P-19-1',1,'teacher2')", (qid,))
        with pytest.raises(E.EvalError) as exc:
            E.freeze_query_set(conn, sid)
        assert "等级" in str(exc.value)
        conn.rollback()


def test_adjudication_resolves_dispute(seeded):
    with tx() as conn:
        sid = _make_set(conn, "s2", JUNIOR_SCOPE, [
            ("定义域", "concept", JV, [("P-19-1", 3, False, None)])])
        qid = conn.execute(
            "SELECT query_id FROM eval_queries WHERE set_id=%s",
            (sid,)).fetchone()["query_id"]
        conn.execute(
            "INSERT INTO eval_annotations(query_id, unit_code, grade, annotator) "
            "VALUES (%s,'P-19-1',1,'teacher2')", (qid,))
        # 教研员裁定：全部驳回后写入单一等级
        conn.execute("UPDATE eval_annotations SET status='rejected' WHERE query_id=%s",
                     (qid,))
        conn.execute(
            "INSERT INTO eval_annotations"
            "(query_id, unit_code, grade, annotator, status, adjudicated_by, adjudicated_at) "
            "VALUES (%s,'P-19-1',2,'editor1','adjudicated','editor1',now())", (qid,))
        result = E.freeze_query_set(conn, sid)
        assert result["status"] == "frozen"
        conn.commit()


# ------------------------------------------------------------ 端到端运行

def test_full_run_metrics_core_regression_and_small_sample(seeded):
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "主集", JUNIOR_SCOPE, MAIN_PLAN)
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 20261114, "bootstrap_iters": 500, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        run = _run_to_completion(conn, run["run_id"])
        s = run["summary"]

        # 两侧共享同一快照与同一索引代次集合：配置对比不允许混入新旧教材内容差异
        snap_gids = {g["generation_id"] for g in run["snapshot"]["generations"]}
        side_rows = conn.execute(
            "SELECT DISTINCT ranked_hits FROM eval_run_queries WHERE run_id=%s",
            (run["run_id"],)).fetchall()
        for row in side_rows:
            for hit in row["ranked_hits"]:
                assert hit["generation_id"] in snap_gids

        # 总体：点估计为正但区间跨零，不得称显著
        ci = s["ci"]["ndcg_at_k"]
        assert ci["point"] > 0
        assert ci["ci_low"] < 0 < ci["ci_high"]
        assert ci["significant"] is False
        assert "seed=20261114" in ci["method"] and "B=500" in ci["method"]

        groups = {(g["volume"], g["question_type"]): g for g in s["groups"]}
        concept = groups[(JV, "concept")]
        required = groups[(JV, "required_chapter")]
        example = groups[(JV, "example")]

        # 常见概念组显著改善
        assert concept["sufficient_sample"] is True
        assert concept["ci"]["ndcg_at_k"]["direction"] == "candidate_better"
        # 必考章节组显著退步且触发人工决策告警
        assert required["sufficient_sample"] is True and required["core_group"] is True
        assert required["ci"]["ndcg_at_k"]["point"] < 0
        assert s["core_regression"] is True and s["warnings"]
        # 例题组样本不足，只报计数
        assert example["sufficient_sample"] is False
        assert example["ci"]["ndcg_at_k"]["direction"] == "sample_too_small"
        assert "只报计数" in example["note"]
        conn.commit()


def test_hard_cases_are_kept_not_dropped(seeded):
    # 物理册索引 failed：两套配置都 no_scope，仍计入完成统计
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "物理难例",
                        {"stages": ["初中"], "subjects": ["物理"], "isbns": [],
                         "include_deprecated": False},
                        [("热运动", "required_chapter", "九年级全一册",
                          [("W-P-13-1", 3, True, None)]),
                         ("扩散现象", "concept", "九年级全一册",
                          [("W-EX-13-1", 2, False, None)])])
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 1, "bootstrap_iters": 200, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        run = _run_to_completion(conn, run["run_id"], batch=5)
        assert run["done_queries"] == 2 and run["total_queries"] == 2
        rows = conn.execute(
            "SELECT DISTINCT status FROM eval_run_queries WHERE run_id=%s",
            (run["run_id"],)).fetchall()
        assert {r["status"] for r in rows} == {"no_scope"}
        detail = E.run_detail(conn, run["run_id"])
        assert all(q["hard_failure"] for q in detail["per_query"])
        conn.commit()


def test_timeout_and_error_queries_retained(seeded, monkeypatch):
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "难例注入", JUNIOR_SCOPE, [
            ("定义域", "concept", JV, [("P-19-1", 3, False, None)]),
            ("中位数", "concept", JV, [("P-20-1", 2, False, None)]),
        ])
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 1, "bootstrap_iters": 200, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")

        original = E.config_search

        def flaky(conn_, query, scope, config, syn, excluded, deadline):
            if query == "定义域" and config.get("bigram_enabled") is False:
                raise E._Timeout()
            if query == "中位数":
                raise RuntimeError("模拟检索失败")
            return original(conn_, query, scope, config, syn, excluded, deadline)

        monkeypatch.setattr(E, "config_search", flaky)
        run = _run_to_completion(conn, run["run_id"], batch=5)
        rows = conn.execute(
            "SELECT query_id, config_side, status, error FROM eval_run_queries "
            "WHERE run_id=%s ORDER BY query_id, config_side",
            (run["run_id"],)).fetchall()
        statuses = {(r["query_id"], r["config_side"]): r["status"] for r in rows}
        assert any(v == "timeout" for v in statuses.values())
        assert any(v == "error" for v in statuses.values())
        # 难例没有被删除：四条 (query × side) 行全部保留
        assert len(rows) == 4
        assert run["timeout_queries"] >= 1 and run["error_queries"] >= 1
        conn.commit()


def test_pause_resume_keeps_same_snapshot_and_results(seeded):
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "续接集", JUNIOR_SCOPE, MAIN_PLAN[:6])
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 20261114, "bootstrap_iters": 300, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        E.process_run(conn, run["run_id"], 2)
        paused = E.pause_run(conn, run["run_id"])
        assert paused["status"] == "paused"
        # 暂停状态下 process 不产生新进度，必须显式 resume
        again = E.process_run(conn, run["run_id"], 2)
        assert again["done_queries"] == paused["done_queries"]
        resumed = E.resume_run(conn, run["run_id"], 2)
        assert resumed["status"] == "running"
        completed = _run_to_completion(conn, run["run_id"], 2)
        assert completed["status"] == "completed"

        # 用同一快照重跑一次，逐条指标必须完全一致（历史评测按原输入复现）
        rerun = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 20261114, "bootstrap_iters": 300, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        _run_to_completion(conn, rerun["run_id"], 10)
        d1 = E.run_detail(conn, completed["run_id"])
        d2 = E.run_detail(conn, rerun["run_id"])
        assert [q["delta"] for q in d1["per_query"]] == \
               [q["delta"] for q in d2["per_query"]]
        conn.commit()


def test_snapshot_isolates_later_synonym_change(seeded):
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "快照集", JUNIOR_SCOPE, [
            ("取值范围", "concept", JV, [("P-19-1", 3, False, None)])])
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 3, "bootstrap_iters": 200, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        _run_to_completion(conn, run["run_id"], 5)
        before = E.run_detail(conn, run["run_id"])
        # 运行后新增同义词，历史运行的快照与结果不受影响
        from app import seed as seedmod
        seedmod._synonym(conn, "数学", "取值范围", "值域", "approved")
        conn.commit()
        after = E.run_detail(conn, run["run_id"])
        assert before["run"]["snapshot"]["frozen_token"] == \
               after["run"]["snapshot"]["frozen_token"]
        assert before["per_query"][0]["baseline"]["metrics"] == \
               after["per_query"][0]["baseline"]["metrics"]


def test_disabled_sample_pending_vs_applied(seeded):
    with tx() as conn:
        gen = conn.execute(
            "SELECT g.generation_id FROM index_generations g "
            "JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "WHERE e.isbn='9787100000011' AND e.generation=2").fetchone()
        b, c = _configs(conn)
        plan = [("定义域", "concept", JV,
                 [("P-19-1", 3, False, None), ("P-19-5", 1, False, None)])]

        # 待维护样本：不排除任何段落，候选配置仍能召回 P-19-5
        conn.execute(
            "INSERT INTO eval_disabled_samples(generation_id, unit_code, reason, created_by) "
            "VALUES (%s,'P-19-5','待维护','teacher1')", (gen["generation_id"],))
        sid = _make_set(conn, "停用-待裁定", JUNIOR_SCOPE, plan)
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 1, "bootstrap_iters": 200, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        _run_to_completion(conn, run["run_id"], 5)
        d = E.run_detail(conn, run["run_id"])
        cand_metrics = d["per_query"][0]["candidate"]["metrics"]
        assert cand_metrics["recall_at_k"] == 1.0

        # 裁定应用后：新运行的快照排除 P-19-5，候选召回下降
        conn.execute(
            "UPDATE eval_disabled_samples SET status='applied', decided_by='editor1', "
            "decided_at=now() WHERE unit_code='P-19-5'")
        sid2 = _make_set(conn, "停用-已应用", JUNIOR_SCOPE, plan)
        E.freeze_query_set(conn, sid2)
        run2 = E.create_run(conn, {
            "set_id": sid2, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 1, "bootstrap_iters": 200, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        _run_to_completion(conn, run2["run_id"], 5)
        d2 = E.run_detail(conn, run2["run_id"])
        assert d2["per_query"][0]["candidate"]["metrics"]["recall_at_k"] == 0.5
        conn.commit()


def test_compatibility_report_and_publish_guard(seeded):
    with tx() as conn:
        good = conn.execute(
            "INSERT INTO eval_configs(name, settings, created_by) VALUES ('ok',%s,'e') "
            "RETURNING config_id", (json.dumps(BASE),)).fetchone()["config_id"]
        bad = conn.execute(
            "INSERT INTO eval_configs(name, settings, created_by) VALUES ('bad',%s,'e') "
            "RETURNING config_id",
            (json.dumps(dict(BASE, bm25_b=1.7)),)).fetchone()["config_id"]
        rep = E.compatibility_report(conn, good)
        assert rep["compatible"] is True and rep["generations"]
        bad_rep = E.compatibility_report(conn, bad)
        assert bad_rep["compatible"] is False
        assert any("bm25_b" in p for p in bad_rep["problems"])
        conn.commit()


def test_adoption_requires_completion_and_records_core_override(seeded):
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "决策集", JUNIOR_SCOPE, MAIN_PLAN)
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 20261114, "bootstrap_iters": 300, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        with pytest.raises(E.EvalError):
            E.decide_adoption(conn, run["run_id"], "adopted", "", "editor1")
        _run_to_completion(conn, run["run_id"], 8)
        decided = E.decide_adoption(
            conn, run["run_id"], "adopted", "人工确认采用", "editor1")
        assert decided["adoption_decision"] == "adopted"
        assert "核心分组退步" in decided["decision_note"]
        conn.commit()


# ------------------------------------------------------------ HTTP API

def test_api_full_eval_workflow_and_permissions(client, seeded, teacher_token,
                                                editor_token, auth_headers):
    th, eh = auth_headers(teacher_token), auth_headers(editor_token)
    # 教师建立查询集与查询
    s = client.post("/api/eval/query-sets", headers=th, json={
        "name": "API 集", "scope": JUNIOR_SCOPE}).json()
    sid = s["set_id"]
    q = client.post(f"/api/eval/query-sets/{sid}/queries", headers=th, json={
        "query_text": "定义域", "question_type": "concept",
        "volume": JV}).json()
    # 范围外段落标注被拒
    bad = client.post(f"/api/eval/queries/{q['query_id']}/annotations",
                      headers=th, json={"unit_code": "HS-P-3-1", "grade": 3})
    assert bad.status_code == 422
    ok = client.post(f"/api/eval/queries/{q['query_id']}/annotations",
                     headers=th, json={"unit_code": "P-19-1", "grade": 3,
                                       "required_source": True})
    assert ok.status_code == 200
    # 教师不能仲裁、不能发起运行
    assert client.post(f"/api/eval/query-sets/{sid}/adjudicate", headers=th,
                       json={"query_id": q["query_id"],
                             "annotations": [{"unit_code": "P-19-1", "grade": 3}]}
                       ).status_code == 403
    fr = client.post(f"/api/eval/query-sets/{sid}/freeze", headers=th)
    assert fr.status_code == 200 and fr.json()["status"] == "frozen"

    cb = client.post("/api/eval/configs", headers=eh, json={
        "name": "API基线", "settings": BASE})
    cc = client.post("/api/eval/configs", headers=eh, json={
        "name": "API候选", "settings": CAND})
    assert cb.status_code == 200 and cc.status_code == 200
    # 教师发起运行被拒
    assert client.post("/api/eval/runs", headers=th, json={
        "set_id": sid, "baseline_config_id": cb.json()["config_id"],
        "candidate_config_id": cc.json()["config_id"]}).status_code == 403
    run = client.post("/api/eval/runs", headers=eh, json={
        "set_id": sid, "baseline_config_id": cb.json()["config_id"],
        "candidate_config_id": cc.json()["config_id"],
        "batch_size": 5, "bootstrap_iters": 200}).json()
    assert run["status"] == "completed"
    detail = client.get(f"/api/eval/runs/{run['run_id']}", headers=eh).json()
    assert detail["per_query"][0]["baseline"]["ranked_hits"]
    # 排名结果带原文与逐词项贡献，供前端展开归因
    hit = detail["per_query"][0]["candidate"]["ranked_hits"][0]
    assert hit["text"] and hit["term_contributions"]

    # 发布前兼容核对；不兼容配置被拒绝发布
    rep = client.post(
        f"/api/eval/configs/{cc.json()['config_id']}/compatibility",
        headers=eh).json()
    assert rep["compatible"] is True
    bad_cfg = client.post("/api/eval/configs", headers=eh, json={
        "name": "API坏配置", "settings": dict(BASE, bm25_k1=-1)}).json()
    refused = client.post(
        f"/api/eval/configs/{bad_cfg['config_id']}/publish", headers=eh)
    assert refused.status_code == 409
    published = client.post(
        f"/api/eval/configs/{cc.json()['config_id']}/publish", headers=eh)
    assert published.status_code == 200
    assert published.json()["status"] == "published"


def test_api_pause_resume_and_disabled_samples(client, seeded, editor_token,
                                               auth_headers):
    eh = auth_headers(editor_token)
    sid_resp = client.post("/api/eval/query-sets", headers=eh, json={
        "name": "暂停集", "scope": JUNIOR_SCOPE})
    sid = sid_resp.json()["set_id"]
    for text, unit in [("定义域", "P-19-1"), ("中位数", "P-20-1"),
                       ("平均数", "P-20-1")]:
        q = client.post(f"/api/eval/query-sets/{sid}/queries", headers=eh,
                        json={"query_text": text, "question_type": "concept",
                              "volume": JV}).json()
        client.post(f"/api/eval/queries/{q['query_id']}/annotations",
                    headers=eh, json={"unit_code": unit, "grade": 2})
    client.post(f"/api/eval/query-sets/{sid}/freeze", headers=eh)
    cb = client.post("/api/eval/configs", headers=eh,
                     json={"name": "暂停基线", "settings": BASE}).json()
    cc = client.post("/api/eval/configs", headers=eh,
                     json={"name": "暂停候选", "settings": CAND}).json()
    run = client.post("/api/eval/runs", headers=eh, json={
        "set_id": sid, "baseline_config_id": cb["config_id"],
        "candidate_config_id": cc["config_id"], "batch_size": 1,
        "bootstrap_iters": 200}).json()
    assert run["done_queries"] == 1 and run["status"] == "running"
    paused = client.post(f"/api/eval/runs/{run['run_id']}/pause", headers=eh)
    assert paused.json()["status"] == "paused"
    resumed = client.post(f"/api/eval/runs/{run['run_id']}/resume",
                          headers=eh, json={"batch_size": 5})
    assert resumed.json()["status"] == "completed"

    # 停用样本流程
    gen = client.get("/api/admin/generations", headers=eh).json()
    g2 = next(g for g in gen if g["isbn"] == "9787100000011"
              and g["edition_generation"] == 2)
    sample = client.post("/api/eval/disabled-samples", headers=eh, json={
        "generation_id": g2["generation_id"], "unit_code": "P-DUP",
        "reason": "API 测试停用"}).json()
    assert sample["status"] == "pending"
    decided = client.post(
        f"/api/eval/disabled-samples/{sample['sample_id']}/decide",
        headers=eh, json={"status": "ignored"})
    assert decided.status_code == 200 and decided.json()["status"] == "ignored"


def test_evaluations_do_not_change_normal_search(seeded, client, teacher_token,
                                                 auth_headers):
    """试验配置只在评测域生效，普通检索路径仍使用线上二元组切分。"""
    th = auth_headers(teacher_token)
    before = client.post("/api/search", headers=th,
                         json={"query": "定义域", "stages": ["初中"]}).json()
    # 跑完一整套候选配置评测
    with tx() as conn:
        b, c = _configs(conn)
        sid = _make_set(conn, "不影响线上", JUNIOR_SCOPE, [
            ("定义域", "concept", JV, [("P-19-1", 3, False, None)])])
        E.freeze_query_set(conn, sid)
        run = E.create_run(conn, {
            "set_id": sid, "baseline_config_id": b, "candidate_config_id": c,
            "seed": 1, "bootstrap_iters": 200, "top_k": 10,
            "eval_timeout_ms": 2000}, "editor1")
        _run_to_completion(conn, run["run_id"], 5)
        conn.commit()
    after = client.post("/api/search", headers=th,
                        json={"query": "定义域", "stages": ["初中"]}).json()
    # 线上行为不变：二元组切分下“定义域”仅命中 P-19-1 一条
    assert [h["unit_code"] for h in before["hits"]] == \
           [h["unit_code"] for h in after["hits"]] == ["P-19-1"]
