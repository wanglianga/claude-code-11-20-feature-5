"""检索引擎：范围过滤 -> 同义词快照扩展 -> BM25 -> 稳定排序 -> 诚实的无命中说明。

所有评分只基于可解释的词项命中，保留词项位置与每词项排序贡献，
不调用任何外部生成模型，不会产出教材中不存在的答案。
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

from . import text as textmod
from .config import settings
from .db import tx


# ---------------------------------------------------------------- 同义词

def approved_synonym_map(conn, subjects: list[str]) -> dict[str, list[dict]]:
    """读取查询开始时刻的已审同义词（快照语义：本次查询期间不再变化）。

    术语与同义词都按同一套词项规则拆开，键为术语的每个词项，
    这样查询中的 unigram/bigram 才能触发对应扩展。
    """
    if not subjects:
        return {}
    rows = conn.execute(
        "SELECT subject, term_norm, synonym_norm, synonym_display "
        "FROM synonyms WHERE status='approved' AND subject = ANY(%s)",
        (subjects,),
    ).fetchall()
    mapping: dict[str, list[dict]] = {}
    for row in rows:
        keys = set(textmod.query_terms(row["term_norm"]))
        syn_terms = textmod.query_terms(row["synonym_norm"])
        for key in keys:
            bucket = mapping.setdefault(key, [])
            existing = {(b["term"], b["via"]) for b in bucket}
            for term in syn_terms:
                if (term, key) not in existing:
                    bucket.append({"term": term,
                                   "display": row["synonym_display"],
                                   "subject": row["subject"], "via": key})
    return mapping


def expand_query_terms(conn, query_terms: list[str], subjects: list[str],
                       enabled: bool) -> list[dict]:
    """返回实际使用词项清单，source 标注 original / synonym。

    扩展仅一层、不递归；同义词原文按同一套词项规则拆成 unigram/bigram/拉丁词，
    保证扩展词项在倒排中真实可命中，短词仍只按原字匹配，不放大含义。
    """
    used: list[dict] = [{"term": t, "source": "original", "via": None}
                        for t in query_terms]
    if not enabled:
        return used
    syn_map = approved_synonym_map(conn, subjects)
    seen = set(query_terms)
    for item in list(used):
        for syn in syn_map.get(item["term"], []):
            for term in [syn["term"]]:
                if term not in seen:
                    seen.add(term)
                    used.append({"term": term, "source": "synonym",
                                 "via": item["term"]})
    return used


# ---------------------------------------------------------------- 范围解析

def resolve_generations(conn, isbns: list[str], stages: list[str],
                        subjects: list[str], include_deprecated: bool) -> list[dict]:
    """先过滤适用教材，再确定检索的索引代次。默认只检索现行稿。"""
    sql_parts = ["SELECT e.edition_id, e.generation AS edition_generation, e.isbn, "
                 "e.status, e.supersede_reason, t.title, t.stage, t.subject, "
                 "g.generation_id AS gid "
                 "FROM textbook_editions e "
                 "JOIN textbooks t ON t.isbn = e.isbn "
                 "JOIN index_generations g ON g.edition_id = e.edition_id "
                 "WHERE g.status IN ('ready','archived')"]
    params: list[Any] = []
    if not include_deprecated:
        sql_parts.append("AND e.status = 'active'")
    else:
        sql_parts.append("AND e.status IN ('active','deprecated')")
    if isbns:
        sql_parts.append("AND e.isbn = ANY(%s)")
        params.append(isbns)
    if stages:
        sql_parts.append("AND t.stage = ANY(%s)")
        params.append(stages)
    if subjects:
        sql_parts.append("AND t.subject = ANY(%s)")
        params.append(subjects)
    rows = conn.execute(" ".join(sql_parts), params).fetchall()
    return [dict(r) for r in rows]


def scope_subjects(conn, stages: list[str], subjects: list[str]) -> list[str]:
    if subjects:
        return subjects
    rows = conn.execute(
        "SELECT DISTINCT subject FROM textbooks"
        + (" WHERE stage = ANY(%s)" if stages else ""),
        (stages,) if stages else None,
    ).fetchall()
    return [r["subject"] for r in rows]


# ---------------------------------------------------------------- 摘要与高亮

def _summary_for_unit(unit: dict, hit_orig_ranges: list[tuple[int, int]]) -> tuple[str, list[int]]:
    """摘要只从同一结构单元提取，落在包含最早命中的句子区间内，绝不跨例题/跨单元拼接。"""
    ranges = unit["sentence_ranges"]
    if not ranges:
        return unit["text"], [0, len(unit["text"])]
    earliest = min((s for s, _ in hit_orig_ranges), default=0)
    chosen = ranges[0]
    for s, e in ranges:
        if s <= earliest < e:
            chosen = [s, e]
            break
    return unit["text"][chosen[0]:chosen[1]], chosen


def _norm_ranges_to_orig(mapping: list[int], ranges: list[list[int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in ranges:
        if e <= s:
            continue
        out.append([mapping[s], mapping[e - 1] + 1])
    return out


# ---------------------------------------------------------------- 主检索

def search(conn, req, username: str) -> dict:
    norm_query, _ = textmod.normalize(req.query)
    query_terms = sorted(set(textmod.query_terms(norm_query)))

    # 翻页固定：同一 session 复用首次检索的代次集合与词项快照
    pinned: dict | None = None
    if req.session_id:
        pinned = conn.execute(
            "SELECT * FROM search_sessions WHERE session_id=%s",
            (req.session_id,),
        ).fetchone()

    if pinned:
        gen_ids = list(pinned["filters"]["pinned_generation_ids"])
        gens = [dict(r) for r in conn.execute(
            "SELECT e.*, g.generation_id AS gid, t.title AS textbook_title, t.isbn "
            "FROM index_generations g JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "JOIN textbooks t ON t.isbn=e.isbn WHERE g.generation_id = ANY(%s)",
            (gen_ids,),
        ).fetchall()]
        used_terms = pinned["expanded_terms"]
        synonym_enabled = pinned["synonym_enabled"]
    else:
        gens = resolve_generations(conn, req.isbns, req.stages, req.subjects,
                                   req.include_deprecated)
        subjects = scope_subjects(conn, req.stages, req.subjects)
        used_terms = expand_query_terms(conn, query_terms, subjects,
                                        req.synonym_enabled)
        synonym_enabled = req.synonym_enabled
        gen_ids = [g["gid"] for g in gens]

    filters_desc = {
        "isbns": req.isbns, "stages": req.stages, "subjects": req.subjects,
        "include_deprecated": req.include_deprecated,
        "pinned_generation_ids": gen_ids,
    }
    all_terms = [u["term"] for u in used_terms]

    # 无命中诚实说明的范围描述（不会悄悄取消学段限制）
    scope_bits = []
    scope_bits.append("学段=" + ("、".join(req.stages) if req.stages else "不限"))
    scope_bits.append("学科=" + ("、".join(req.subjects) if req.subjects else "不限"))
    scope_bits.append("书号=" + ("、".join(req.isbns) if req.isbns else "全部适用教材"))
    scope_bits.append("版本=" + ("含停用旧版" if req.include_deprecated else "仅现行稿"))
    collection_scope = "；".join(scope_bits)

    no_hit_reason = ""
    hits: list[dict] = []
    total = 0
    evidence: list[dict] = []

    if not gens:
        no_hit_reason = (
            f"过滤范围内没有可用的现行索引（{collection_scope}）。"
            "未取消任何过滤条件，请调整教材范围或由编辑完成构建/启用。"
        )
    elif not all_terms:
        no_hit_reason = f"查询“{req.query}”归一化后未产生可检索词项（{collection_scope}）。"
    else:
        units = conn.execute(
            "SELECT u.*, e.status AS edition_status, e.supersede_reason, "
            "t.title AS textbook_title, t.isbn, e.edition_id "
            "FROM structural_units u "
            "JOIN textbook_editions e ON e.edition_id = u.edition_id "
            "JOIN textbooks t ON t.isbn = e.isbn "
            "WHERE u.generation_id = ANY(%s)",
            (gen_ids,),
        ).fetchall()
        unit_map = {u["unit_id"]: dict(u) for u in units}

        postings = conn.execute(
            "SELECT generation_id, term, unit_id, tf, positions FROM postings "
            "WHERE generation_id = ANY(%s) AND term = ANY(%s)",
            (gen_ids, all_terms),
        ).fetchall()

        stats_rows = conn.execute(
            "SELECT * FROM index_stats WHERE generation_id = ANY(%s)",
            (gen_ids,),
        ).fetchall()
        stats = {r["generation_id"]: r for r in stats_rows}

        # 词项证据（跨代次汇总 df / idf）
        total_n = sum(r["doc_count"] for r in stats_rows)
        df_rows = conn.execute(
            "SELECT term, sum(df) AS df FROM term_dfs "
            "WHERE generation_id = ANY(%s) AND term = ANY(%s) GROUP BY term",
            (gen_ids, all_terms),
        ).fetchall()
        df_map = {r["term"]: int(r["df"]) for r in df_rows}

        term_meta = {u["term"]: u for u in used_terms}
        evidence: list[dict] = []
        for term in all_terms:
            df = df_map.get(term, 0)
            idf = math.log(1 + (total_n - df + 0.5) / (df + 0.5)) if total_n else 0.0
            meta = term_meta[term]
            evidence.append({"term": term, "source": meta["source"],
                             "via": meta["via"], "df": df, "idf": round(idf, 4),
                             "matched_units": 0})

        # 按单元聚合词项命中并计算 BM25
        per_unit: dict[int, dict] = {}
        for p in postings:
            bucket = per_unit.setdefault(p["unit_id"], {})
            bucket[p["term"]] = {"tf": p["tf"], "positions": p["positions"],
                                 "generation_id": p["generation_id"]}

        for unit_id, matched in per_unit.items():
            unit = unit_map[unit_id]
            st = stats[unit["generation_id"]]
            avgdl = float(st["avgdl"]) or 1.0
            dl = len(unit["norm_text"])
            denom_len = 1 - settings.bm25_b + settings.bm25_b * dl / avgdl
            score = 0.0
            contributions: dict[str, float] = {}
            orig_positions: dict[str, list[list[int]]] = {}
            for term, hit in matched.items():
                df = df_map.get(term, 0)
                idf = math.log(1 + (total_n - df + 0.5) / (df + 0.5))
                tf = hit["tf"]
                term_score = idf * (tf * (settings.bm25_k1 + 1)) / (
                    tf + settings.bm25_k1 * denom_len)
                score += term_score
                contributions[term] = round(term_score, 4)
                mapping = unit["norm_mapping"]
                orig_positions[term] = _norm_ranges_to_orig(mapping, hit["positions"])
            summary, summary_range = _summary_for_unit(
                unit, [tuple(r) for ranges in orig_positions.values() for r in ranges])
            hits.append({
                "unit_id": unit_id,
                "unit_code": unit["unit_code"],
                "unit_type": unit["unit_type"],
                "chapter_path": unit["section_path"].split(" / ")[0],
                "section_path": unit["section_path"],
                "title_path": unit["title_path"],
                "isbn": unit["isbn"],
                "textbook_title": unit["textbook_title"],
                "edition_id": unit["edition_id"],
                "generation_id": unit["generation_id"],
                "edition_status": unit["edition_status"],
                "supersede_reason": unit["supersede_reason"],
                "score": round(score, 4),
                "matched_terms": sorted(matched.keys()),
                "term_contributions": contributions,
                "positions": orig_positions,
                "summary": summary,
                "summary_range": summary_range,
                "sentence_ranges": unit["sentence_ranges"],
                "text": unit["text"],
            })

        # 稳定排序：相关度降序，平分时按稳定段落标识升序
        hits.sort(key=lambda h: (-h["score"], h["unit_code"]))
        total = len(hits)
        for ev in evidence:
            ev["matched_units"] = sum(1 for h in hits if ev["term"] in h["matched_terms"])

        if total == 0:
            no_hit_reason = (
                f"在{collection_scope}范围内，用词项【{'、'.join(all_terms)}】"
                "均未命中任何结构单元。系统未放宽学段/版本过滤，也未生成教材之外的答案；"
                "可尝试关闭同义词扩展比较结果，或更换为教材中的原词。"
            )

        hits = hits[(req.page - 1) * req.page_size: req.page * req.page_size]
        for rank, hit in enumerate(hits, start=(req.page - 1) * req.page_size + 1):
            hit["rank"] = rank

    session_id = req.session_id
    if not pinned:
        session_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO search_sessions"
            "(session_id, username, generation_id, query_text, filters, "
            " query_terms, expanded_terms, synonym_enabled) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (session_id, username, gen_ids[0] if gen_ids else 0, req.query,
             json.dumps(filters_desc, ensure_ascii=False),
             json.dumps(query_terms, ensure_ascii=False),
             json.dumps(used_terms, ensure_ascii=False), synonym_enabled),
        )

    conn.execute(
        "INSERT INTO query_log"
        "(username, query_text, query_terms, expanded_terms, filters, generation_id, "
        " hit_count, no_hit_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (username, req.query, json.dumps(query_terms, ensure_ascii=False),
         json.dumps(used_terms, ensure_ascii=False),
         json.dumps(filters_desc, ensure_ascii=False),
         gen_ids[0] if gen_ids else None, total, no_hit_reason),
    )

    return {
        "session_id": session_id,
        "query": req.query,
        "normalized_query": norm_query,
        "query_terms": query_terms,
        "expanded_terms": evidence if evidence else (
            [{"term": u["term"], "source": u["source"], "via": u["via"],
              "df": 0, "idf": 0.0, "matched_units": 0} for u in used_terms]),
        "synonym_enabled": synonym_enabled,
        "filters": filters_desc,
        "generation_ids": gen_ids,
        "total": total,
        "page": req.page,
        "page_size": req.page_size,
        "hits": hits,
        "no_hit": total == 0,
        "no_hit_reason": no_hit_reason,
        "collection_scope": collection_scope,
    }


# ---------------------------------------------------------------- 原文上下文

def unit_detail(conn, unit_id: int, session_id: str | None) -> dict:
    unit = conn.execute(
        "SELECT u.*, e.status AS edition_status, t.title AS textbook_title, "
        "t.isbn, e.edition_id, g.status AS generation_status "
        "FROM structural_units u "
        "JOIN textbook_editions e ON e.edition_id=u.edition_id "
        "JOIN textbooks t ON t.isbn=e.isbn "
        "JOIN index_generations g ON g.generation_id=u.generation_id "
        "WHERE u.unit_id=%s",
        (unit_id,),
    ).fetchone()
    if not unit:
        raise LookupError("结构单元不存在或其索引代次已不可访问")
    if session_id:
        sess = conn.execute(
            "SELECT filters FROM search_sessions WHERE session_id=%s",
            (session_id,),
        ).fetchone()
        if sess and unit["generation_id"] not in sess["filters"]["pinned_generation_ids"]:
            raise PermissionError("该单元不属于本次检索固定的索引代次，拒绝跨代次拼接原文")

    siblings = conn.execute(
        "SELECT unit_id, unit_code, unit_type, title_path, ordinal_in_book "
        "FROM structural_units WHERE generation_id=%s AND chapter_code=%s "
        "ORDER BY ordinal_in_book, unit_id",
        (unit["generation_id"], unit["chapter_code"]),
    ).fetchall()
    result = dict(unit)
    result["sibling_units"] = [dict(s) for s in siblings]
    return result


# ---------------------------------------------------------------- 引用

def create_citation(conn, req, username: str) -> dict:
    sess = conn.execute(
        "SELECT * FROM search_sessions WHERE session_id=%s",
        (req.session_id,),
    ).fetchone()
    if not sess:
        raise PermissionError("引用必须来自一次已固定代次的检索会话")
    unit = conn.execute(
        "SELECT u.*, e.edition_id FROM structural_units u "
        "JOIN textbook_editions e ON e.edition_id=u.edition_id "
        "WHERE u.unit_id=%s AND u.generation_id = ANY(%s)",
        (req.unit_id, sess["filters"]["pinned_generation_ids"]),
    ).fetchone()
    if not unit:
        raise PermissionError("所见代次中找不到该单元，拒绝从最新结果补段冒充原文")
    if req.quoted_text not in unit["text"]:
        raise ValueError("采用文字必须是原文的精确片段，未通过原文校验")

    locator = req.locator or {"unit_code": unit["unit_code"],
                              "chapter_code": unit["chapter_code"],
                              "section_path": unit["section_path"]}
    row = conn.execute(
        "INSERT INTO citations"
        "(username, generation_id, edition_id, unit_id, unit_code, quoted_text, "
        " content_hash, locator, last_checked_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING *",
        (username, unit["generation_id"], unit["edition_id"], unit["unit_id"],
         unit["unit_code"], req.quoted_text, unit["content_hash"],
         json.dumps(locator, ensure_ascii=False)),
    ).fetchone()
    conn.execute(
        "INSERT INTO citation_checks(citation_id, status, detail) "
        "VALUES (%s,'ok','创建时校验：引用文字与所选代次指纹一致')",
        (row["citation_id"],),
    )
    return _citation_dict(row)


def check_citation(conn, citation_id: int) -> dict:
    cit = conn.execute("SELECT * FROM citations WHERE citation_id=%s",
                       (citation_id,)).fetchone()
    if not cit:
        raise LookupError("引用不存在")
    status, detail = "ok", ""
    gen = conn.execute(
        "SELECT status FROM index_generations WHERE generation_id=%s",
        (cit["generation_id"],),
    ).fetchone()
    unit = conn.execute(
        "SELECT * FROM structural_units WHERE unit_id=%s AND generation_id=%s",
        (cit["unit_id"], cit["generation_id"]),
    ).fetchone()

    if not gen or gen["status"] not in ("ready", "archived") or not unit:
        status, detail = "edition_unavailable", (
            "引用所固定的索引代次已不可访问，无法核对原文；"
            "系统不会从最新版结果中补一段冒充原文，请重新选取引用。")
    elif unit["content_hash"] != cit["content_hash"]:
        status, detail = "drifted", (
            "同一代次内单元内容指纹与引用时不一致，请人工复核出处。")
    elif cit["quoted_text"] not in unit["text"]:
        status, detail = "drifted", "采用文字在原代次单元中已找不到精确匹配。"
    else:
        status, detail = "ok", "复核通过：引用文字、指纹与固定代次原文一致。"

    conn.execute(
        "UPDATE citations SET last_status=%s, last_detail=%s, last_checked_at=now() "
        "WHERE citation_id=%s",
        (status, detail, citation_id),
    )
    conn.execute(
        "INSERT INTO citation_checks(citation_id, status, detail) VALUES (%s,%s,%s)",
        (citation_id, status, detail),
    )
    row = conn.execute("SELECT * FROM citations WHERE citation_id=%s",
                       (citation_id,)).fetchone()
    return _citation_dict(row)


def _citation_dict(row: dict) -> dict:
    return {
        "citation_id": row["citation_id"],
        "generation_id": row["generation_id"],
        "edition_id": row["edition_id"],
        "unit_code": row["unit_code"],
        "quoted_text": row["quoted_text"],
        "content_hash": row["content_hash"],
        "locator": row["locator"],
        "last_status": row["last_status"],
        "last_detail": row["last_detail"],
        "created_at": row["created_at"].isoformat(),
        "last_checked_at": row["last_checked_at"].isoformat()
        if row["last_checked_at"] else None,
    }
