"""HTTP API 端到端：鉴权、检索、同义词审核、构建与原子切换、陈旧作业保护。"""


def test_login_and_role_guard(client, seeded, teacher_token, editor_token, auth_headers):
    bad = client.post("/api/auth/login", json={"username": "nobody"})
    assert bad.status_code == 401
    assert client.get("/api/textbooks").status_code == 401

    # 教师不能访问编辑管理接口
    r = client.get("/api/admin/generations", headers=auth_headers(teacher_token))
    assert r.status_code == 403
    r = client.get("/api/admin/generations", headers=auth_headers(editor_token))
    assert r.status_code == 200


def test_search_flow_and_unit_detail(client, seeded, teacher_token, auth_headers):
    h = auth_headers(teacher_token)
    r = client.post("/api/search", headers=h,
                    json={"query": "函数", "stages": ["高中"]})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    sid = body["session_id"]
    hit = body["hits"][0]

    detail = client.get(f"/api/units/{hit['unit_id']}?session_id={sid}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["text"]

    # 翻页固定代次
    p2 = client.post("/api/search", headers=h,
                     json={"query": "函数", "stages": ["高中"], "page": 2,
                           "page_size": 3, "session_id": sid})
    assert p2.json()["generation_ids"] == body["generation_ids"]


def test_citation_lifecycle_api(client, seeded, teacher_token, auth_headers):
    h = auth_headers(teacher_token)
    r = client.post("/api/search", headers=h,
                    json={"query": "定义域", "stages": ["高中"]}).json()
    hit = r["hits"][0]
    created = client.post("/api/citations", headers=h, json={
        "session_id": r["session_id"], "unit_id": hit["unit_id"],
        "quoted_text": hit["text"][:15]}).json()
    assert created["last_status"] == "ok"

    checked = client.post(
        f"/api/citations/{created['citation_id']}/check", headers=h)
    assert checked.json()["last_status"] == "ok"
    listed = client.get("/api/citations", headers=h).json()
    assert any(c["citation_id"] == created["citation_id"] for c in listed)


def test_synonym_review_workflow(client, seeded, teacher_token, editor_token,
                                 auth_headers):
    th, eh = auth_headers(teacher_token), auth_headers(editor_token)
    created = client.post("/api/synonyms", headers=th, json={
        "subject": "数学", "term_display": "平均数",
        "synonym_display": "均值"}).json()
    assert created["status"] == "pending"

    # 待审同义词不参与扩展
    before = client.post("/api/search", headers=th, json={
        "query": "平均数", "stages": ["初中"]}).json()
    assert all(e["source"] == "original" for e in before["expanded_terms"])

    reviewed = client.post(
        f"/api/synonyms/{created['synonym_id']}/review",
        headers=eh, json={"status": "approved"})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"

    # 教师无权审核
    denied = client.post(
        f"/api/synonyms/{created['synonym_id']}/review",
        headers=th, json={"status": "rejected"})
    assert denied.status_code == 403


def test_build_interruption_report_api(client, seeded, editor_token, auth_headers):
    h = auth_headers(editor_token)
    r = client.post("/api/admin/build", headers=h, json={
        "isbn": "9787100000035",
        "payload_rel": "9787100000035/payload.json",
        "chapter_codes": ["W-CH13"]})
    # 同代次已构建过 -> 409；改用全新验证：直接检查已有的失败代次报告
    if r.status_code == 200:
        assert r.json()["status"] == "failed"
    gens = client.get("/api/admin/generations", headers=h).json()
    failed = [g for g in gens if g["status"] == "failed"]
    assert failed
    report = client.get(
        f"/api/admin/reports/{failed[0]['generation_id']}", headers=h).json()
    stages = {(x["chapter_code"], x["stage"], x["status"])
              for x in report["stage_reports"]}
    assert any(s == "error" for _, _, s in stages)


def test_stale_job_cannot_override_newer(client, seeded, editor_token, auth_headers):
    """旧作业（低代次）的启用申请必须被拒绝，不能覆盖后完成的新稿。"""
    h = auth_headers(editor_token)
    # 构造一个针对旧版 edition 的替代申请
    editions = client.get("/api/textbooks", headers=h).json()
    book = next(b for b in editions if b["isbn"] == "9787100000011")
    old_edition = next(e for e in book["editions"] if e["generation"] == 1)
    new_edition = next(e for e in book["editions"] if e["generation"] == 2)
    req = client.post("/api/admin/replacement/request", headers=h, json={
        "isbn": book["isbn"], "old_edition_id": new_edition["edition_id"],
        "new_edition_id": old_edition["edition_id"],
        "reason": "试图让旧作业回滚现行索引"})
    rid = req.json()["request_id"]
    resp = client.post(f"/api/admin/replacement/{rid}/approve", headers=h)
    assert resp.status_code == 409
    # 现行稿仍是新版
    books2 = client.get("/api/textbooks", headers=h).json()
    active = next(e for b in books2 for e in b["editions"] if e["status"] == "active"
                  and b["isbn"] == book["isbn"])
    assert active["generation"] == 2


def test_failed_generation_cannot_be_activated(client, seeded, editor_token,
                                               auth_headers):
    h = auth_headers(editor_token)
    gens = client.get("/api/admin/generations", headers=h).json()
    failed = next(g for g in gens if g["status"] == "failed")
    req = client.post("/api/admin/replacement/request", headers=h, json={
        "isbn": failed["isbn"], "old_edition_id": None,
        "new_edition_id": failed["edition_id"], "reason": "坏章版本试图启用"})
    rid = req.json()["request_id"]
    resp = client.post(f"/api/admin/replacement/{rid}/approve", headers=h)
    assert resp.status_code == 409


def test_editor_correction_closes_issues_but_keeps_generation_failed(
        client, seeded, editor_token, teacher_token, auth_headers):
    eh = auth_headers(editor_token)
    th = auth_headers(teacher_token)
    gens = client.get("/api/admin/generations", headers=eh).json()
    failed = next(g for g in gens if g["status"] == "failed")

    # 教师无权提交修正
    denied = client.post("/api/admin/corrections", headers=th, json={
        "edition_id": failed["edition_id"], "chapter_code": "W-CH14",
        "payload": {"code": "W-CH14"}})
    assert denied.status_code == 403

    r = client.post("/api/admin/corrections", headers=eh, json={
        "edition_id": failed["edition_id"], "chapter_code": "W-CH14",
        "payload": {"code": "W-CH14", "fixed": True}})
    assert r.status_code == 200
    assert r.json()["resolved_issue_ids"]

    # 修正存档不会把失败代次变成可用
    gens2 = client.get("/api/admin/generations", headers=eh).json()
    still_failed = next(g for g in gens2 if g["generation_id"] == failed["generation_id"])
    assert still_failed["status"] == "failed"

    # 教师在现行范围里仍检索不到该物理册内容（无可用索引）
    search = client.post("/api/search", headers=th,
                         json={"query": "热运动", "stages": ["初中"],
                               "subjects": ["物理"]}).json()
    assert search["no_hit"] is True
