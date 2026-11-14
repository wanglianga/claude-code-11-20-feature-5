"""FastAPI 入口：认证、检索、原文、引用、同义词、构建与编辑管理路由。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import importer, search as searchmod
from . import evaluation as evalmod
from .config import settings
from .db import init_schema, tx
from .models import (
    AdoptionIn, AdjudicationIn, AnnotationIn, BuildReport, BuildRequest,
    CitationCreate, CitationOut, ConfigIn, DisabledSampleIn, EvalQueryIn,
    LoginRequest, QuerySetIn, ReplacementIn, RunCreateIn, SearchRequest,
    SynonymIn, SynonymOut, TextbookOut, UnitOut, UserOut,
)


# ---------------------------------------------------------------- 简易令牌

def make_token(username: str) -> str:
    payload = {"u": username, "exp": int(time.time()) + settings.token_ttl_hours * 3600}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode()
    sig = hmac.new(settings.token_secret.encode(), body.encode(),
                   hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 令牌")
    token = authorization[7:]
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(401, "令牌格式错误")
    expect = hmac.new(settings.token_secret.encode(), body.encode(),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        raise HTTPException(401, "令牌签名无效")
    payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    if payload["exp"] < int(time.time()):
        raise HTTPException(401, "令牌已过期")
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=%s",
                           (payload["u"],)).fetchone()
    if not row:
        raise HTTPException(401, "用户不存在")
    return dict(row)


def require_editor(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可执行该操作")
    return user


# ---------------------------------------------------------------- 生命周期

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    if settings.auto_seed:
        from .seed import seed_all
        seed_all()
    yield


app = FastAPI(title="教材检索与引用核查平台 API", version="1.0.0",
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)


# ---------------------------------------------------------------- 认证与元数据

@app.post("/api/auth/login", response_model=dict)
def login(req: LoginRequest):
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=%s",
                           (req.username,)).fetchone()
    if not row:
        raise HTTPException(401, f"用户 {req.username} 不存在，可用 editor1 / teacher1")
    return {"token": make_token(row["username"]),
            "user": UserOut(**row).model_dump()}


@app.get("/api/meta")
def meta(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT DISTINCT subject, stage FROM textbooks ORDER BY 1,2").fetchall()
    return {"subjects": sorted({r["subject"] for r in rows}),
            "stages": sorted({r["stage"] for r in rows})}


# ---------------------------------------------------------------- 教材与版本

@app.get("/api/textbooks")
def list_textbooks(user: dict = Depends(current_user)):
    with tx() as conn:
        books = conn.execute("SELECT * FROM textbooks ORDER BY isbn").fetchall()
        out: list[dict] = []
        for b in books:
            editions = conn.execute(
                "SELECT e.*, g.status AS gen_status, g.generation_id "
                "FROM textbook_editions e "
                "JOIN index_generations g ON g.edition_id=e.edition_id "
                "WHERE e.isbn=%s ORDER BY e.generation DESC", (b["isbn"],)).fetchall()
            active = next((e for e in editions if e["status"] == "active"), None)
            out.append({
                **TextbookOut(
                    isbn=b["isbn"], title=b["title"], subject=b["subject"],
                    stage=b["stage"], volume=b["volume"], semester=b["semester"],
                    publisher=b["publisher"],
                    active_generation_id=active["generation_id"] if active else None,
                    active_edition_id=active["edition_id"] if active else None,
                    active_status=active["gen_status"] if active else None,
                    has_older=any(e["status"] == "deprecated" for e in editions),
                ).model_dump(),
                "editions": [dict(e) for e in editions],
            })
        return out


# ---------------------------------------------------------------- 检索与原文

@app.post("/api/search")
def do_search(req: SearchRequest, user: dict = Depends(current_user)):
    with tx() as conn:
        return searchmod.search(conn, req, user["username"])


@app.get("/api/units/{unit_id}")
def get_unit(unit_id: int, session_id: str | None = Query(default=None),
             user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            return searchmod.unit_detail(conn, unit_id, session_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            raise HTTPException(409, str(exc))


# ---------------------------------------------------------------- 引用

@app.post("/api/citations")
def add_citation(req: CitationCreate, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            row = searchmod.create_citation(conn, req, user["username"])
            conn.commit()
            return row
        except PermissionError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))


@app.get("/api/citations")
def list_citations(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM citations WHERE username=%s ORDER BY citation_id DESC",
            (user["username"],)).fetchall()
        return [searchmod._citation_dict(r) for r in rows]


@app.post("/api/citations/{citation_id}/check")
def recheck_citation(citation_id: int, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            row = searchmod.check_citation(conn, citation_id)
            conn.commit()
            return row
        except LookupError as exc:
            raise HTTPException(404, str(exc))


# ---------------------------------------------------------------- 同义词

@app.get("/api/synonyms")
def list_synonyms(status: str = "all", user: dict = Depends(current_user)):
    sql = "SELECT * FROM synonyms"
    params: tuple = ()
    if status != "all":
        sql += " WHERE status=%s"
        params = (status,)
    sql += " ORDER BY synonym_id DESC"
    with tx() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [SynonymOut(**r).model_dump() for r in rows]


@app.post("/api/synonyms")
def create_synonym(req: SynonymIn, user: dict = Depends(current_user)):
    from .text import normalize
    term_norm, _ = normalize(req.term_display.strip())
    syn_norm, _ = normalize(req.synonym_display.strip())
    if not term_norm or not syn_norm:
        raise HTTPException(422, "术语与同义词不能为空")
    with tx() as conn:
        try:
            row = conn.execute(
                "INSERT INTO synonyms"
                "(subject, term_norm, term_display, synonym_norm, synonym_display, "
                " status, created_by) VALUES (%s,%s,%s,%s,%s,'pending',%s) RETURNING *",
                (req.subject, term_norm, req.term_display.strip(), syn_norm,
                 req.synonym_display.strip(), user["username"]),
            ).fetchone()
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"该学科同义词已存在：{exc}")
    return SynonymOut(**row).model_dump()


@app.post("/api/synonyms/{synonym_id}/review")
def review_synonym(synonym_id: int, body: dict,
                   editor: dict = Depends(require_editor)):
    decision = body.get("status")
    if decision not in ("approved", "rejected"):
        raise HTTPException(422, "status 必须是 approved 或 rejected")
    with tx() as conn:
        row = conn.execute(
            "UPDATE synonyms SET status=%s, reviewed_by=%s, reviewed_at=now() "
            "WHERE synonym_id=%s RETURNING *",
            (decision, editor["username"], synonym_id),
        ).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "同义词不存在")
    return SynonymOut(**row).model_dump()


# ---------------------------------------------------------------- 构建与代次

@app.post("/api/admin/build", response_model=BuildReport)
def build_book(req: BuildRequest, editor: dict = Depends(require_editor)):
    payload_path = os.path.join(
        settings.data_dir, req.payload_rel or f"{req.isbn}/payload.json")
    if not os.path.exists(payload_path):
        raise HTTPException(404, f"数据包不存在：{payload_path}")
    try:
        report = importer.build_from_payload(
            payload_path, chapter_codes=req.chapter_codes,
            username=editor["username"])
        return report
    except importer.BuildError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/admin/generations")
def list_generations(editor: dict = Depends(require_editor)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT g.*, e.generation AS edition_generation, t.title, t.isbn "
            "FROM index_generations g "
            "JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "JOIN textbooks t ON t.isbn=e.isbn "
            "ORDER BY g.generation_id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["started_at"] = r["started_at"].isoformat()
            d["finished_at"] = r["finished_at"].isoformat() if r["finished_at"] else None
            out.append(d)
        return out


@app.get("/api/admin/reports/{generation_id}")
def build_report(generation_id: int, editor: dict = Depends(require_editor)):
    with tx() as conn:
        gen = conn.execute(
            "SELECT * FROM index_generations WHERE generation_id=%s",
            (generation_id,)).fetchone()
        if not gen:
            raise HTTPException(404, "代次不存在")
        reports = conn.execute(
            "SELECT chapter_code, stage, status, counts, error FROM index_chapter_reports "
            "WHERE generation_id=%s ORDER BY id", (generation_id,)).fetchall()
        issues = conn.execute(
            "SELECT * FROM parse_issues WHERE generation_id=%s ORDER BY issue_id",
            (generation_id,)).fetchall()
        return {"generation": dict(gen),
                "stage_reports": [dict(r) for r in reports],
                "issues": [dict(i) for i in issues]}


@app.get("/api/admin/issues")
def list_issues(status: str = "open", editor: dict = Depends(require_editor)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM parse_issues WHERE status=%s ORDER BY generation_id, issue_id",
            (status,)).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/admin/issues/{issue_id}/resolve")
def resolve_issue(issue_id: int, editor: dict = Depends(require_editor)):
    with tx() as conn:
        row = conn.execute(
            "UPDATE parse_issues SET status='resolved' WHERE issue_id=%s RETURNING *",
            (issue_id,)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "问题不存在")
    return dict(row)


@app.post("/api/admin/corrections")
def submit_correction(body: dict, editor: dict = Depends(require_editor)):
    """编辑提交坏章修正稿：登记修正并关闭该章待修正问题。

    修正稿只作存档与问题闭环；重新入索引需以新一代次数据包重新构建，
    失败代次始终不会被直接“补章”变成可用，避免半新半旧的混合索引。
    """
    edition_id = body.get("edition_id")
    chapter_code = body.get("chapter_code")
    payload = body.get("payload")
    if not edition_id or not chapter_code or payload is None:
        raise HTTPException(422, "edition_id、chapter_code、payload 均为必填")
    with tx() as conn:
        edition = conn.execute(
            "SELECT * FROM textbook_editions WHERE edition_id=%s",
            (edition_id,)).fetchone()
        if not edition:
            raise HTTPException(404, "版本不存在")
        conn.execute(
            "INSERT INTO chapter_corrections(edition_id, chapter_code, payload, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (edition_id, chapter_code) DO UPDATE SET "
            "payload=EXCLUDED.payload, created_by=EXCLUDED.created_by, created_at=now()",
            (edition_id, chapter_code,
             json.dumps(payload, ensure_ascii=False), editor["username"]),
        )
        closed = conn.execute(
            "UPDATE parse_issues SET status='resolved' "
            "WHERE edition_id=%s AND chapter_code=%s AND status='open' RETURNING issue_id",
            (edition_id, chapter_code)).fetchall()
        conn.commit()
    return {"edition_id": edition_id, "chapter_code": chapter_code,
            "resolved_issue_ids": [r["issue_id"] for r in closed],
            "note": "修正稿已存档；请用提升 generation 的新数据包重新构建后再申请启用。"}


# ---------------------------------------------------------------- 替代批准与原子切换

@app.post("/api/admin/replacement/request")
def request_replacement(req: ReplacementIn, editor: dict = Depends(require_editor)):
    with tx() as conn:
        row = conn.execute(
            "INSERT INTO replacement_requests"
            "(isbn, old_edition_id, new_edition_id, reason, status) "
            "VALUES (%s,%s,%s,%s,'pending') RETURNING *",
            (req.isbn, req.old_edition_id, req.new_edition_id, req.reason),
        ).fetchone()
        conn.commit()
    return dict(row)


@app.get("/api/admin/replacement")
def list_replacements(editor: dict = Depends(require_editor)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM replacement_requests ORDER BY request_id DESC").fetchall()
        return [dict(r) for r in rows]


@app.post("/api/admin/replacement/{request_id}/approve")
def approve_replacement(request_id: int, editor: dict = Depends(require_editor)):
    """原子切换现行索引：旧版停用归档，新版启用；旧作业不可能覆盖更新的新稿。"""
    with tx() as conn:
        req = conn.execute(
            "SELECT * FROM replacement_requests WHERE request_id=%s FOR UPDATE",
            (request_id,)).fetchone()
        if not req:
            raise HTTPException(404, "替代申请不存在")
        if req["status"] != "pending":
            raise HTTPException(409, f"申请已处理：{req['status']}")

        new_gen = conn.execute(
            "SELECT g.*, e.generation AS edition_generation FROM index_generations g "
            "JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "WHERE g.edition_id=%s FOR UPDATE", (req["new_edition_id"],)).fetchone()
        if not new_gen or new_gen["status"] != "ready":
            raise HTTPException(409, "新稿索引未就绪（构建未成功或不齐全），拒绝切换")

        # 陈旧作业保护：只允许启用该 ISBN 下最大代次的就绪稿
        newest = conn.execute(
            "SELECT max(e.generation) AS m FROM textbook_editions e "
            "JOIN index_generations g ON g.edition_id=e.edition_id "
            "WHERE e.isbn=%s AND g.status='ready'", (req["isbn"],)).fetchone()
        if newest["m"] is not None and new_gen["edition_generation"] < newest["m"]:
            conn.execute(
                "UPDATE replacement_requests SET status='rejected', decided_by=%s, "
                "decided_at=now() WHERE request_id=%s",
                (editor["username"], request_id))
            conn.commit()
            raise HTTPException(409, "存在更新的就绪代次，旧作业不得覆盖新稿，申请已拒绝")

        old = conn.execute(
            "SELECT e.edition_id, g.generation_id FROM textbook_editions e "
            "JOIN index_generations g ON g.edition_id=e.edition_id "
            "WHERE e.isbn=%s AND e.status='active' FOR UPDATE",
            (req["isbn"],)).fetchall()
        for o in old:
            conn.execute(
                "UPDATE textbook_editions SET status='deprecated', "
                "superseded_by=%s, supersede_reason=coalesce(nullif(supersede_reason,''),%s) "
                "WHERE edition_id=%s",
                (req["new_edition_id"], req["reason"] or "已由编辑批准的新版替代",
                 o["edition_id"]))
            conn.execute(
                "UPDATE index_generations SET status='archived' WHERE generation_id=%s",
                (o["generation_id"],))
        conn.execute(
            "UPDATE textbook_editions SET status='active' WHERE edition_id=%s",
            (req["new_edition_id"],))
        conn.execute(
            "UPDATE textbooks SET current_generation_id=%s WHERE isbn=%s",
            (new_gen["generation_id"], req["isbn"]))
        conn.execute(
            "UPDATE replacement_requests SET status='approved', decided_by=%s, "
            "decided_at=now() WHERE request_id=%s",
            (editor["username"], request_id))
        conn.commit()
        return {"ok": True, "isbn": req["isbn"],
                "active_generation_id": new_gen["generation_id"]}


@app.post("/api/admin/replacement/{request_id}/reject")
def reject_replacement(request_id: int, body: dict,
                       editor: dict = Depends(require_editor)):
    with tx() as conn:
        row = conn.execute(
            "UPDATE replacement_requests SET status='rejected', decided_by=%s, "
            "decided_at=now() WHERE request_id=%s RETURNING *",
            (editor["username"], request_id)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "替代申请不存在")
    return dict(row)


@app.post("/api/admin/generations/{generation_id}/retire")
def retire_generation(generation_id: int, editor: dict = Depends(require_editor)):
    """将已归档代次标记为不可访问，用于演示引用固定代次失效后的诚实失败。"""
    with tx() as conn:
        row = conn.execute(
            "UPDATE index_generations SET status='retired' "
            "WHERE generation_id=%s AND status='archived' RETURNING *",
            (generation_id,)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(409, "仅已归档(archived)的代次可退役")
    return dict(row)


# ============================================================
# 候选配置评测（教师建查询集/标注，教研员仲裁，编辑发起对比与采用决策）
# ============================================================

def _require_researcher(user: dict) -> None:
    """教研员职责由编辑角色承担（演示环境不新增角色）。"""
    if user["role"] != "editor":
        raise HTTPException(403, "仅教研员（编辑角色）可裁定标注分歧")


@app.get("/api/eval/query-sets")
def list_query_sets(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT count(*) FROM eval_queries q "
            " WHERE q.set_id=s.set_id AND q.status='active') AS query_count "
            "FROM eval_query_sets s ORDER BY s.set_id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = r["created_at"].isoformat()
            d["frozen_at"] = r["frozen_at"].isoformat() if r["frozen_at"] else None
            out.append(d)
        return out


@app.post("/api/eval/query-sets")
def create_query_set(req: QuerySetIn, user: dict = Depends(current_user)):
    scope = req.scope or {}
    with tx() as conn:
        row = conn.execute(
            "INSERT INTO eval_query_sets(name, description, scope, created_by) "
            "VALUES (%s,%s,%s,%s) RETURNING *",
            (req.name, req.description,
             json.dumps(scope, ensure_ascii=False), user["username"])).fetchone()
        conn.commit()
    return dict(row)


@app.get("/api/eval/query-sets/{set_id}")
def get_query_set(set_id: int, user: dict = Depends(current_user)):
    with tx() as conn:
        s = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                         (set_id,)).fetchone()
        if not s:
            raise HTTPException(404, "查询集不存在")
        queries = conn.execute(
            "SELECT * FROM eval_queries WHERE set_id=%s ORDER BY query_id",
            (set_id,)).fetchall()
        result = dict(s)
        result["queries"] = []
        for q in queries:
            qd = dict(q)
            qd["annotations"] = [dict(a) for a in conn.execute(
                "SELECT * FROM eval_annotations WHERE query_id=%s "
                "ORDER BY annotation_id", (q["query_id"],)).fetchall()]
            result["queries"].append(qd)
        return result


@app.post("/api/eval/query-sets/{set_id}/queries")
def add_eval_query(set_id: int, req: EvalQueryIn,
                   user: dict = Depends(current_user)):
    if req.question_type not in evalmod.VALID_QUESTION_TYPES:
        raise HTTPException(422,
                            f"question_type 取值：{evalmod.VALID_QUESTION_TYPES}")
    with tx() as conn:
        s = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                         (set_id,)).fetchone()
        if not s:
            raise HTTPException(404, "查询集不存在")
        if s["status"] != "draft":
            raise HTTPException(409, "查询集已冻结，不能再增改查询；请复制为新查询集")
        try:
            row = conn.execute(
                "INSERT INTO eval_queries"
                "(set_id, query_text, question_type, volume, note, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (set_id, req.query_text.strip(), req.question_type,
                 req.volume, req.note, user["username"])).fetchone()
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"该查询集已存在相同查询：{exc}")
    return dict(row)


@app.post("/api/eval/queries/{query_id}/disable")
def disable_eval_query(query_id: int, body: dict,
                       user: dict = Depends(current_user)):
    with tx() as conn:
        q = conn.execute("SELECT * FROM eval_queries WHERE query_id=%s",
                         (query_id,)).fetchone()
        if not q:
            raise HTTPException(404, "查询不存在")
        s = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                         (q["set_id"],)).fetchone()
        if s["status"] != "draft":
            raise HTTPException(409, "查询集已冻结，停用须在冻结前完成")
        row = conn.execute(
            "UPDATE eval_queries SET status='disabled', "
            "disabled_reason=%s, updated_at=now() WHERE query_id=%s RETURNING *",
            (body.get("reason", "维护停用"), query_id)).fetchone()
        conn.commit()
    return dict(row)


@app.post("/api/eval/queries/{query_id}/annotations")
def add_annotation(query_id: int, req: AnnotationIn,
                   user: dict = Depends(current_user)):
    with tx() as conn:
        q = conn.execute("SELECT * FROM eval_queries WHERE query_id=%s",
                         (query_id,)).fetchone()
        if not q:
            raise HTTPException(404, "查询不存在")
        s = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                         (q["set_id"],)).fetchone()
        if s["status"] != "draft":
            raise HTTPException(409, "查询集已冻结，标注不可变更")
        # 标注的段落必须真实存在于查询集适用范围对应的索引中
        scope = s["scope"]
        gens = evalmod.searchmod.resolve_generations(
            conn, scope.get("isbns", []), scope.get("stages", []),
            scope.get("subjects", []), scope.get("include_deprecated", False))
        unit = conn.execute(
            "SELECT * FROM structural_units WHERE unit_code=%s "
            "AND generation_id = ANY(%s)",
            (req.unit_code, [g["gid"] for g in gens])).fetchone()
        if not unit:
            raise HTTPException(422, "标注段落不在该查询集适用范围内，拒绝标注")
        try:
            row = conn.execute(
                "INSERT INTO eval_annotations"
                "(query_id, unit_code, grade, required_source, equiv_group, "
                " annotator, note) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (query_id, req.unit_code, req.grade, req.required_source,
                 req.equiv_group or None, user["username"], req.note)).fetchone()
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"你已标注过该段落：{exc}")
    return dict(row)


@app.post("/api/eval/query-sets/{set_id}/adjudicate")
def adjudicate(set_id: int, req: AdjudicationIn,
               user: dict = Depends(current_user)):
    """教研员裁定：以提交的全量标注替换该查询的有效标注，旧标注全部 rejected。"""
    _require_researcher(user)
    with tx() as conn:
        s = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                         (set_id,)).fetchone()
        if not s:
            raise HTTPException(404, "查询集不存在")
        if s["status"] != "draft":
            raise HTTPException(409, "查询集已冻结，不能再裁定")
        q = conn.execute("SELECT * FROM eval_queries WHERE query_id=%s AND set_id=%s",
                         (req.query_id, set_id)).fetchone()
        if not q:
            raise HTTPException(404, "查询不属于该查询集")
        if not req.annotations:
            raise HTTPException(422, "裁定标注不能为空")
        conn.execute(
            "UPDATE eval_annotations SET status='rejected' WHERE query_id=%s",
            (req.query_id,))
        created = []
        for ann in req.annotations:
            row = conn.execute(
                "INSERT INTO eval_annotations"
                "(query_id, unit_code, grade, required_source, equiv_group, "
                " annotator, note, status, adjudicated_by, adjudicated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'adjudicated',%s,now()) RETURNING *",
                (req.query_id, ann.unit_code, ann.grade, ann.required_source,
                 ann.equiv_group or None, user["username"], ann.note,
                 user["username"])).fetchone()
            created.append(dict(row))
        conn.commit()
    return {"query_id": req.query_id, "adjudicated": created}


@app.post("/api/eval/query-sets/{set_id}/freeze")
def freeze_set(set_id: int, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            result = evalmod.freeze_query_set(conn, set_id)
            conn.commit()
        except evalmod.EvalError as exc:
            raise HTTPException(409, str(exc))
    return result


@app.get("/api/eval/configs")
def list_configs(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM eval_configs ORDER BY config_id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = r["created_at"].isoformat()
            d["published_at"] = r["published_at"].isoformat() if r["published_at"] else None
            out.append(d)
        return out


@app.post("/api/eval/configs")
def create_config(req: ConfigIn, user: dict = Depends(current_user)):
    allowed = {"bm25_k1", "bm25_b", "synonym_enabled", "bigram_enabled", "top_k"}
    unknown = set(req.settings) - allowed
    if unknown:
        raise HTTPException(422, f"不支持的配置项：{sorted(unknown)}；允许：{sorted(allowed)}")
    with tx() as conn:
        try:
            row = conn.execute(
                "INSERT INTO eval_configs(name, description, settings, created_by) "
                "VALUES (%s,%s,%s,%s) RETURNING *",
                (req.name, req.description,
                 json.dumps(req.settings, ensure_ascii=False),
                 user["username"])).fetchone()
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"配置名已存在：{exc}")
    return dict(row)


@app.post("/api/eval/configs/{config_id}/compatibility")
def check_compatibility(config_id: int, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            report = evalmod.compatibility_report(conn, config_id)
        except evalmod.EvalError as exc:
            raise HTTPException(404, str(exc))
        conn.execute(
            "UPDATE eval_configs SET compat_report=%s WHERE config_id=%s",
            (json.dumps(report, ensure_ascii=False), config_id))
        conn.commit()
    return report


@app.post("/api/eval/configs/{config_id}/publish")
def publish_config(config_id: int, user: dict = Depends(current_user)):
    """发布前再次核对当前索引兼容；不兼容则拒绝发布。评测结果不改变普通查询路径。"""
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可发布候选配置")
    with tx() as conn:
        cfg = conn.execute("SELECT * FROM eval_configs WHERE config_id=%s",
                           (config_id,)).fetchone()
        if not cfg:
            raise HTTPException(404, "配置不存在")
        report = evalmod.compatibility_report(conn, config_id)
        if not report["compatible"]:
            raise HTTPException(409,
                                "当前索引与配置不兼容，拒绝发布："
                                + "；".join(report["problems"]))
        row = conn.execute(
            "UPDATE eval_configs SET status='published', published_at=now(), "
            "compat_report=%s WHERE config_id=%s RETURNING *",
            (json.dumps(report, ensure_ascii=False), config_id)).fetchone()
        conn.commit()
    return dict(row)


@app.get("/api/eval/runs")
def list_runs(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT r.*, s.name AS set_name, cb.name AS baseline_name, "
            "cc.name AS candidate_name FROM eval_runs r "
            "JOIN eval_query_sets s ON s.set_id=r.set_id "
            "JOIN eval_configs cb ON cb.config_id=r.baseline_config_id "
            "JOIN eval_configs cc ON cc.config_id=r.candidate_config_id "
            "ORDER BY r.run_id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for key in ("started_at", "finished_at", "decided_at", "created_at"):
                if d.get(key) is not None and hasattr(d[key], "isoformat"):
                    d[key] = d[key].isoformat()
            out.append(d)
        return out


@app.post("/api/eval/runs")
def create_run(req: RunCreateIn, user: dict = Depends(current_user)):
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑（检索维护人员）可发起对比评测")
    with tx() as conn:
        try:
            run = evalmod.create_run(conn, req.model_dump(), user["username"])
            conn.commit()
        except evalmod.EvalError as exc:
            raise HTTPException(409, str(exc))
        # 首个批次立即执行，前端可用同一接口轮询续接
        run = evalmod.process_run(conn, run["run_id"], req.batch_size)
        conn.commit()
    return evalmod._run_json(run)


@app.post("/api/eval/runs/{run_id}/process")
def process_run(run_id: int, body: dict | None = None,
                user: dict = Depends(current_user)):
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可驱动评测批次")
    batch = int((body or {}).get("batch_size", 20))
    with tx() as conn:
        try:
            run = evalmod.process_run(conn, run_id, batch)
            conn.commit()
        except evalmod.EvalError as exc:
            raise HTTPException(404, str(exc))
    return evalmod._run_json(run)


@app.post("/api/eval/runs/{run_id}/pause")
def pause_run(run_id: int, user: dict = Depends(current_user)):
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可暂停评测")
    with tx() as conn:
        try:
            run = evalmod.pause_run(conn, run_id)
            conn.commit()
        except evalmod.EvalError as exc:
            raise HTTPException(409, str(exc))
    return evalmod._run_json(run)


@app.post("/api/eval/runs/{run_id}/resume")
def resume_run(run_id: int, body: dict | None = None,
               user: dict = Depends(current_user)):
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可续接评测")
    batch = int((body or {}).get("batch_size", 20))
    with tx() as conn:
        try:
            run = evalmod.resume_run(conn, run_id, batch)
            conn.commit()
        except evalmod.EvalError as exc:
            raise HTTPException(409, str(exc))
    return evalmod._run_json(run)


@app.get("/api/eval/runs/{run_id}")
def get_run(run_id: int, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            return evalmod.run_detail(conn, run_id)
        except evalmod.EvalError as exc:
            raise HTTPException(404, str(exc))


@app.post("/api/eval/runs/{run_id}/decision")
def adoption_decision(run_id: int, req: AdoptionIn,
                      user: dict = Depends(current_user)):
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可登记采用决策")
    with tx() as conn:
        try:
            run = evalmod.decide_adoption(
                conn, run_id, req.decision, req.note, user["username"])
            conn.commit()
        except evalmod.EvalError as exc:
            raise HTTPException(409, str(exc))
    return evalmod._run_json(run)


@app.get("/api/eval/disabled-samples")
def list_disabled_samples(status: str = "all",
                          user: dict = Depends(current_user)):
    sql = ("SELECT d.*, t.title AS textbook_title FROM eval_disabled_samples d "
           "JOIN index_generations g ON g.generation_id=d.generation_id "
           "JOIN textbook_editions e ON e.edition_id=g.edition_id "
           "JOIN textbooks t ON t.isbn=e.isbn")
    params: tuple = ()
    if status != "all":
        sql += " WHERE d.status=%s"
        params = (status,)
    sql += " ORDER BY d.sample_id DESC"
    with tx() as conn:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = r["created_at"].isoformat()
            d["decided_at"] = r["decided_at"].isoformat() if r["decided_at"] else None
            out.append(d)
        return out


@app.post("/api/eval/disabled-samples")
def create_disabled_sample(req: DisabledSampleIn,
                           user: dict = Depends(current_user)):
    """段落停用标记样本：先进入待维护队列，裁定应用后才在后续评测中排除。"""
    with tx() as conn:
        gen = conn.execute(
            "SELECT * FROM index_generations WHERE generation_id=%s",
            (req.generation_id,)).fetchone()
        if not gen:
            raise HTTPException(404, "索引代次不存在")
        unit = conn.execute(
            "SELECT * FROM structural_units WHERE generation_id=%s AND unit_code=%s",
            (req.generation_id, req.unit_code)).fetchone()
        if not unit:
            raise HTTPException(422, "该代次内找不到此段落标识")
        try:
            row = conn.execute(
                "INSERT INTO eval_disabled_samples"
                "(generation_id, unit_code, reason, created_by) "
                "VALUES (%s,%s,%s,%s) RETURNING *",
                (req.generation_id, req.unit_code, req.reason,
                 user["username"])).fetchone()
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"该段落已有停用样本：{exc}")
    return dict(row)


@app.post("/api/eval/disabled-samples/{sample_id}/decide")
def decide_disabled_sample(sample_id: int, body: dict,
                           user: dict = Depends(current_user)):
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可裁定停用样本")
    decision = body.get("status")
    if decision not in ("applied", "ignored"):
        raise HTTPException(422, "status 必须为 applied 或 ignored")
    with tx() as conn:
        row = conn.execute(
            "UPDATE eval_disabled_samples SET status=%s, decided_by=%s, "
            "decided_at=now() WHERE sample_id=%s AND status='pending' RETURNING *",
            (decision, user["username"], sample_id)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(409, "样本不存在或已裁定")
    return dict(row)


# ---------------------------------------------------------------- 前端静态资源

def _resolve_static_dir() -> str:
    configured = os.getenv("STATIC_DIR", "/workspace/20/frontend/dist")
    if os.path.isdir(configured):
        return configured
    repo_local = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
    return repo_local if os.path.isdir(repo_local) else configured


static_dir = _resolve_static_dir()
if os.path.isdir(static_dir):
    from fastapi.staticfiles import StaticFiles
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")),
              name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):  # pragma: no cover - 仅容器内生效
        from fastapi.responses import FileResponse
        target = os.path.join(static_dir, full_path)
        if full_path and os.path.isfile(target):
            return FileResponse(target)
        return FileResponse(os.path.join(static_dir, "index.html"))
