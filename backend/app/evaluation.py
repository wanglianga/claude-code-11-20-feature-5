"""候选配置评测引擎。

设计约束（对应需求）：
- 两套配置在**相同教材内容与同一查询集**上运行：评测复用既有倒排索引，
  切分（二元组开关）、排序（BM25 k1/b）、同义词开关都在查询期按配置生效，
  评测过程只读索引，绝不写入 postings/index_stats，不影响普通检索。
- 评测开始前固定索引、标注、配置与同义词快照，历史评测永远按原输入复现。
- 多个等价答案以集合表示：同一 equiv_group 命中任一成员即视为覆盖，不偏向首个标注。
- 超时/失败/无适用索引的查询保留在完成统计与逐条结果中，不删除难例。
- 小样本分组只报计数，不宣布改进；总体差异用固定种子的成对重采样给区间，
  区间跨零不得称为显著获胜；核心分组（含必需出处）退步时转人工采用决策。
- 运行可暂停、按同一快照续接；配置发布前再次核对当前索引兼容性。
"""
from __future__ import annotations

import json
import math
import random
import time
import uuid

from . import text as textmod
from .config import settings
from . import search as searchmod

MIN_GROUP_SAMPLE = 5          # 分组可信最小样本量，不足则只报计数
GRADE_GAIN = {1: 1, 2: 3, 3: 7}  # 分级相关性增益（grade 1~3）
VALID_QUESTION_TYPES = ("concept", "required_chapter", "example")


class EvalError(Exception):
    """可直接转为 4xx 的业务错误。"""


class _Timeout(Exception):
    pass


# ---------------------------------------------------------------- 配置化词项

def config_query_terms(norm_query: str, bigram_enabled: bool) -> list[str]:
    """查询侧词项化。bigram_enabled=False 时退回单字/拉丁整词，模拟切分调整。"""
    if bigram_enabled:
        return textmod.query_terms(norm_query)
    terms: list[str] = []
    n = len(norm_query)
    i = 0
    while i < n:
        ch = norm_query[i]
        if textmod.is_cjk(ch):
            terms.append(ch)
            i += 1
        elif ch.isascii() and ch.isalnum():
            j = i
            while j < n and norm_query[j].isascii() and norm_query[j].isalnum():
                j += 1
            terms.append(norm_query[i:j])
            i = j
        else:
            i += 1
    return terms


def _synonym_map_from_snapshot(syn_snapshot: list[dict],
                               subjects: list[str]) -> dict[str, list[dict]]:
    """同义词只取运行快照里的已审词表，与运行后新增/审核的词完全隔离。"""
    mapping: dict[str, list[dict]] = {}
    subj_set = set(subjects)
    for row in syn_snapshot:
        if row["subject"] not in subj_set:
            continue
        keys = set(textmod.query_terms(row["term_norm"]))
        for key in keys:
            bucket = mapping.setdefault(key, [])
            existing = {(b["term"], b["via"]) for b in bucket}
            for term in textmod.query_terms(row["synonym_norm"]):
                if (term, key) not in existing:
                    bucket.append({"term": term,
                                   "display": row.get("synonym_display", term),
                                   "subject": row["subject"], "via": key})
    return mapping


def _expand_with_snapshot(query_terms: list[str], syn_snapshot: list[dict],
                          subjects: list[str], enabled: bool) -> list[dict]:
    used: list[dict] = [{"term": t, "source": "original", "via": None}
                        for t in query_terms]
    if not enabled:
        return used
    syn_map = _synonym_map_from_snapshot(syn_snapshot, subjects)
    seen = set(query_terms)
    for item in list(used):
        for syn in syn_map.get(item["term"], []):
            if syn["term"] not in seen:
                seen.add(syn["term"])
                used.append({"term": syn["term"], "source": "synonym",
                             "via": item["term"]})
    return used


# ---------------------------------------------------------------- 配置化检索

def config_search(conn, query_text: str, scope: dict, config: dict,
                  syn_snapshot: list[dict], excluded: set[tuple[int, str]],
                  deadline: float) -> dict:
    """在固定索引代次集合上按候选配置执行一次检索，返回排名与状态。

    excluded 为已裁定应用的停用样本 (generation_id, unit_code) 集合；
    待维护（pending）样本不在其中，不影响任何评测。
    """
    started = time.monotonic()
    norm_query, _ = textmod.normalize(query_text)
    q_terms = sorted(set(config_query_terms(norm_query,
                                            config.get("bigram_enabled", True))))
    subjects = searchmod.scope_subjects(conn, scope.get("stages", []),
                                        scope.get("subjects", []))
    used_terms = _expand_with_snapshot(q_terms, syn_snapshot, subjects,
                                       config.get("synonym_enabled", True))
    all_terms = [u["term"] for u in used_terms]
    gens = searchmod.resolve_generations(
        conn, scope.get("isbns", []), scope.get("stages", []),
        scope.get("subjects", []), scope.get("include_deprecated", False))
    gen_ids = [g["gid"] for g in gens]

    if not gens:
        return {"status": "no_scope", "hits": [], "elapsed_ms": _elapsed(started),
                "error": "适用范围内没有可用索引代次", "terms": used_terms}
    if not all_terms:
        return {"status": "ok", "hits": [], "elapsed_ms": _elapsed(started),
                "error": "", "terms": used_terms}

    if time.monotonic() > deadline:
        raise _Timeout()

    units = conn.execute(
        "SELECT u.*, e.status AS edition_status, e.supersede_reason, "
        "t.title AS textbook_title, t.isbn, e.edition_id "
        "FROM structural_units u "
        "JOIN textbook_editions e ON e.edition_id = u.edition_id "
        "JOIN textbooks t ON t.isbn = e.isbn "
        "WHERE u.generation_id = ANY(%s)", (gen_ids,)).fetchall()
    unit_map = {u["unit_id"]: dict(u) for u in units}

    postings = conn.execute(
        "SELECT generation_id, term, unit_id, tf, positions FROM postings "
        "WHERE generation_id = ANY(%s) AND term = ANY(%s)",
        (gen_ids, all_terms)).fetchall()
    stats_rows = conn.execute(
        "SELECT * FROM index_stats WHERE generation_id = ANY(%s)",
        (gen_ids,)).fetchall()
    stats = {r["generation_id"]: r for r in stats_rows}
    total_n = sum(r["doc_count"] for r in stats_rows)
    df_rows = conn.execute(
        "SELECT term, sum(df) AS df FROM term_dfs "
        "WHERE generation_id = ANY(%s) AND term = ANY(%s) GROUP BY term",
        (gen_ids, all_terms)).fetchall()
    df_map = {r["term"]: int(r["df"]) for r in df_rows}

    if time.monotonic() > deadline:
        raise _Timeout()

    k1 = float(config.get("bm25_k1", settings.bm25_k1))
    b = float(config.get("bm25_b", settings.bm25_b))
    per_unit: dict[int, dict] = {}
    for p in postings:
        per_unit.setdefault(p["unit_id"], {})[p["term"]] = {
            "tf": p["tf"], "positions": p["positions"],
            "generation_id": p["generation_id"]}

    hits: list[dict] = []
    for unit_id, matched in per_unit.items():
        unit = unit_map[unit_id]
        if (unit["generation_id"], unit["unit_code"]) in excluded:
            continue  # 已裁定应用的段落停用样本（待定样本不在此集合内）
        st = stats[unit["generation_id"]]
        avgdl = float(st["avgdl"]) or 1.0
        dl = len(unit["norm_text"])
        denom_len = 1 - b + b * dl / avgdl
        score = 0.0
        contributions: dict[str, float] = {}
        orig_positions: dict[str, list[list[int]]] = {}
        for term, hit in matched.items():
            df = df_map.get(term, 0)
            idf = math.log(1 + (total_n - df + 0.5) / (df + 0.5)) if total_n else 0.0
            tf = hit["tf"]
            term_score = idf * (tf * (k1 + 1)) / (tf + k1 * denom_len)
            score += term_score
            contributions[term] = round(term_score, 4)
            orig_positions[term] = searchmod._norm_ranges_to_orig(
                unit["norm_mapping"], hit["positions"])
        summary, summary_range = searchmod._summary_for_unit(
            unit, [tuple(r) for ranges in orig_positions.values() for r in ranges])
        hits.append({
            "unit_id": unit_id, "unit_code": unit["unit_code"],
            "unit_type": unit["unit_type"], "chapter_code": unit["chapter_code"],
            "section_path": unit["section_path"], "title_path": unit["title_path"],
            "isbn": unit["isbn"], "textbook_title": unit["textbook_title"],
            "edition_id": unit["edition_id"], "generation_id": unit["generation_id"],
            "edition_status": unit["edition_status"],
            "score": round(score, 4), "matched_terms": sorted(matched.keys()),
            "term_contributions": contributions, "positions": orig_positions,
            "summary": summary, "summary_range": summary_range, "text": unit["text"],
        })

    hits.sort(key=lambda h: (-h["score"], h["unit_code"]))
    top_k = int(config.get("top_k", 10))
    for rank, hit in enumerate(hits[:top_k], start=1):
        hit["rank"] = rank
    return {"status": "ok", "hits": hits[:top_k], "elapsed_ms": _elapsed(started),
            "error": "", "terms": used_terms}


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ---------------------------------------------------------------- 标注与相关性

def effective_annotations(annotations: list[dict]) -> list[dict]:
    """取有效标注：存在仲裁结果时只认 adjudicated；全部 proposed 时说明未冻结。"""
    adj = [a for a in annotations if a["status"] == "adjudicated"]
    return adj if adj else [a for a in annotations if a["status"] != "rejected"]


def build_relevance_items(annotations: list[dict]) -> list[dict]:
    """把标注展开为相关性条目。

    - 同一 equiv_group 的多个标注构成一个等价集合（任一成员命中即覆盖）；
    - 无 equiv_group 的标注各自为一个条目；
    - 等级取条目内最高等级，required 任一标注声明必需则整条目必需。
    """
    groups: dict[str, dict] = {}
    items: list[dict] = []
    for ann in annotations:
        if ann.get("equiv_group"):
            g = groups.setdefault(ann["equiv_group"],
                                  {"units": set(), "grade": ann["grade"],
                                   "required": False, "equiv_group": ann["equiv_group"]})
            g["units"].add(ann["unit_code"])
            g["grade"] = max(g["grade"], ann["grade"])
            g["required"] = g["required"] or ann["required_source"]
        else:
            items.append({"units": {ann["unit_code"]}, "grade": ann["grade"],
                          "required": ann["required_source"], "equiv_group": None})
    items.extend(groups.values())
    return items


def score_query(ranked_codes: list[str], items: list[dict], top_k: int) -> dict:
    """单查询指标：前列召回、必需出处满足、NDCG@K、无结果标记。"""
    ranked = ranked_codes[:top_k]
    ranked_set = set(ranked)
    total = len(items)
    required = [it for it in items if it["required"]]

    covered = [it for it in items if it["units"] & ranked_set]
    recall = len(covered) / total if total else None
    req_covered = [it for it in required if it["units"] & ranked_set]
    required_recall = (len(req_covered) / len(required)) if required else None
    required_fulfilled = len(req_covered) == len(required) if required else None

    # NDCG：等价集合按最早出现成员的位置占位，理想序列按等级降序
    first_rank: dict[int, int] = {}
    for idx, code in enumerate(ranked, start=1):
        for item_idx, it in enumerate(items):
            if code in it["units"] and item_idx not in first_rank:
                first_rank[item_idx] = idx
    dcg = sum(GRADE_GAIN[it["grade"]] / math.log2(rank + 1)
              for item_idx, rank in first_rank.items()
              for it in [items[item_idx]])
    ideal = sorted((GRADE_GAIN[it["grade"]] for it in items), reverse=True)[:top_k]
    idcg = sum(gain / math.log2(pos + 1) for pos, gain in enumerate(ideal, start=1))
    ndcg = (dcg / idcg) if idcg > 0 else None

    return {
        "recall_at_k": round(recall, 4) if recall is not None else None,
        "ndcg_at_k": round(ndcg, 4) if ndcg is not None else None,
        "required_recall": (round(required_recall, 4)
                            if required_recall is not None else None),
        "required_fulfilled": required_fulfilled,
        "relevant_item_count": total,
        "required_item_count": len(required),
        "covered_item_count": len(covered),
        "no_result": len(ranked) == 0,
    }


# ---------------------------------------------------------------- 成对重采样

def paired_bootstrap(diffs: list[float], seed: int, iters: int) -> dict:
    """固定种子的成对百分位 bootstrap，返回区间与方法说明。"""
    n = len(diffs)
    point = sum(diffs) / n if n else 0.0
    if n < 2 or iters <= 0:
        return {"point": round(point, 4), "ci_low": None, "ci_high": None,
                "significant": False, "method": f"paired-bootstrap(seed={seed},B=0)",
                "sample_size": n, "iters": 0}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        acc = 0.0
        for _ in range(n):
            acc += diffs[rng.randrange(n)]
        means.append(acc / n)
    means.sort()
    lo_idx = max(0, int(0.025 * iters) - 1)
    hi_idx = min(iters - 1, int(0.975 * iters))
    lo, hi = means[lo_idx], means[hi_idx]
    return {
        "point": round(point, 4),
        "ci_low": round(lo, 4), "ci_high": round(hi, 4),
        "significant": (lo > 0 or hi < 0),
        "direction": "candidate_better" if lo > 0 else
                     ("candidate_worse" if hi < 0 else "inconclusive"),
        "method": f"paired-percentile-bootstrap(seed={seed}, B={iters}, n={n})",
        "sample_size": n, "iters": iters,
    }


# ---------------------------------------------------------------- 快照与冻结

def _freeze_snapshot(conn, set_row: dict) -> dict:
    queries = conn.execute(
        "SELECT * FROM eval_queries WHERE set_id=%s AND status='active' "
        "ORDER BY query_id", (set_row["set_id"],)).fetchall()
    q_snap: list[dict] = []
    for q in queries:
        anns = conn.execute(
            "SELECT * FROM eval_annotations WHERE query_id=%s "
            "ORDER BY annotation_id", (q["query_id"],)).fetchall()
        q_snap.append({
            "query_id": q["query_id"], "query_text": q["query_text"],
            "question_type": q["question_type"], "volume": q["volume"],
            "note": q["note"],
            "annotations": [_ann_json(a) for a in anns],
        })
    syn_rows = conn.execute(
        "SELECT subject, term_norm, synonym_norm, synonym_display "
        "FROM synonyms WHERE status='approved'").fetchall()
    gen_rows = conn.execute(
        "SELECT g.generation_id, g.edition_id, g.isbn, g.status, "
        "g.total_units, e.generation AS edition_generation "
        "FROM index_generations g JOIN textbook_editions e "
        "ON e.edition_id=g.edition_id ORDER BY g.generation_id").fetchall()
    return {
        "set_id": set_row["set_id"], "name": set_row["name"],
        "scope": dict(set_row["scope"]), "queries": q_snap,
        "synonyms": [dict(r) for r in syn_rows],
        "generations": [dict(r) for r in gen_rows],
        "frozen_token": uuid.uuid4().hex,
    }


def _ann_json(row: dict) -> dict:
    return {
        "annotation_id": row["annotation_id"], "unit_code": row["unit_code"],
        "grade": row["grade"], "required_source": row["required_source"],
        "equiv_group": str(row["equiv_group"]) if row["equiv_group"] else None,
        "annotator": row["annotator"],
        "note": row["note"], "status": row["status"],
    }


def freeze_query_set(conn, set_id: int) -> dict:
    row = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                       (set_id,)).fetchone()
    if not row:
        raise EvalError("查询集不存在")
    if row["status"] != "draft":
        raise EvalError(f"查询集当前状态为 {row['status']}，仅草稿可冻结")
    queries = conn.execute(
        "SELECT * FROM eval_queries WHERE set_id=%s AND status='active'",
        (set_id,)).fetchall()
    if not queries:
        raise EvalError("查询集没有任何有效查询，无法冻结")
    unannotated, disputes = [], []
    for q in queries:
        anns = conn.execute(
            "SELECT * FROM eval_annotations WHERE query_id=%s",
            (q["query_id"],)).fetchall()
        effective = effective_annotations([dict(a) for a in anns])
        if not effective:
            unannotated.append(q["query_text"])
            continue
        # 同段落不同等级 / 等价集合互相重叠才是必须仲裁的分歧；
        # 无冲突的 proposed 标注视为教师间一致意见，可直接随查询集冻结。
        grade_map: dict[str, set[int]] = {}
        group_units: dict[str, set[str]] = {}
        for a in anns:
            if a["status"] == "rejected":
                continue
            grade_map.setdefault(a["unit_code"], set()).add(a["grade"])
            if a["equiv_group"]:
                group_units.setdefault(a["equiv_group"], set()).add(a["unit_code"])
        if any(len(v) > 1 for v in grade_map.values()):
            disputes.append({"query": q["query_text"],
                             "reason": "同一段落存在多个等级标注"})
        overlaps = list(group_units.values())
        for i in range(len(overlaps)):
            for j in range(i + 1, len(overlaps)):
                if overlaps[i] & overlaps[j]:
                    disputes.append({"query": q["query_text"],
                                     "reason": "等价集合之间存在重叠段落"})
    if unannotated or disputes:
        raise EvalError(json.dumps(
            {"unannotated": unannotated, "disputes": disputes},
            ensure_ascii=False))
    conn.execute(
        "UPDATE eval_query_sets SET status='frozen', frozen_at=now() "
        "WHERE set_id=%s", (set_id,))
    return {"set_id": set_id, "status": "frozen"}


# ---------------------------------------------------------------- 运行处理

def create_run(conn, body: dict, username: str) -> dict:
    set_row = conn.execute("SELECT * FROM eval_query_sets WHERE set_id=%s",
                           (body["set_id"],)).fetchone()
    if not set_row:
        raise EvalError("查询集不存在")
    if set_row["status"] != "frozen":
        raise EvalError("查询集未冻结：评测前必须先固定标注与适用范围")
    base = conn.execute("SELECT * FROM eval_configs WHERE config_id=%s",
                        (body["baseline_config_id"],)).fetchone()
    cand = conn.execute("SELECT * FROM eval_configs WHERE config_id=%s",
                        (body["candidate_config_id"],)).fetchone()
    if not base or not cand:
        raise EvalError("基线或候选配置不存在")
    if base["status"] == "retired" or cand["status"] == "retired":
        raise EvalError("已退役配置不得用于评测")
    if base["config_id"] == cand["config_id"]:
        raise EvalError("基线与候选配置必须不同")

    snapshot = _freeze_snapshot(conn, set_row)
    snapshot["baseline_config"] = {"config_id": base["config_id"],
                                   "name": base["name"],
                                   "settings": dict(base["settings"])}
    snapshot["candidate_config"] = {"config_id": cand["config_id"],
                                    "name": cand["name"],
                                    "settings": dict(cand["settings"])}
    applied = conn.execute(
        "SELECT generation_id, unit_code FROM eval_disabled_samples "
        "WHERE status='applied'").fetchall()
    snapshot["disabled_units_applied"] = [dict(r) for r in applied]

    run = conn.execute(
        "INSERT INTO eval_runs(set_id, baseline_config_id, candidate_config_id, "
        "status, seed, bootstrap_iters, top_k, eval_timeout_ms, snapshot, "
        "total_queries, created_by) "
        "VALUES (%s,%s,%s,'running',%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (body["set_id"], base["config_id"], cand["config_id"],
         int(body.get("seed", 20261114)), int(body.get("bootstrap_iters", 1000)),
         int(body.get("top_k", 10)), int(body.get("eval_timeout_ms", 2000)),
         json.dumps(snapshot, ensure_ascii=False),
         len(snapshot["queries"]), username)).fetchone()
    return dict(run)


def _processed_query_ids(conn, run_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT query_id FROM eval_run_queries WHERE run_id=%s",
        (run_id,)).fetchall()
    return {r["query_id"] for r in rows}


def process_run(conn, run_id: int, batch_size: int) -> dict:
    """处理一个批次；已完成的 (run, query, side) 行幂等跳过，支持暂停后续接。"""
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                       (run_id,)).fetchone()
    if not run:
        raise EvalError("评测运行不存在")
    if run["status"] in ("completed", "failed", "paused"):
        return dict(run)
    snap = run["snapshot"]
    excluded = {(d["generation_id"], d["unit_code"])
                for d in snap.get("disabled_units_applied", [])}
    done = _processed_query_ids(conn, run_id)
    pending = [q for q in snap["queries"] if q["query_id"] not in done]
    batch = pending[:max(1, batch_size)]

    timeout_ms = run["eval_timeout_ms"]
    sides = (("baseline", snap["baseline_config"]["settings"]),
             ("candidate", snap["candidate_config"]["settings"]))
    for q in batch:
        for side, config in sides:
            cfg = dict(config)
            cfg["top_k"] = run["top_k"]
            deadline = time.monotonic() + timeout_ms / 1000.0
            status, error, hits, elapsed, metrics = "ok", "", [], 0, {}
            try:
                result = config_search(
                    conn, q["query_text"], snap["scope"], cfg,
                    snap["synonyms"], excluded, deadline)
                status, hits, elapsed, error = (
                    result["status"], result["hits"], result["elapsed_ms"],
                    result["error"])
                items = build_relevance_items(
                    effective_annotations(q["annotations"]))
                if items:
                    metrics = score_query(
                        [h["unit_code"] for h in hits], items, run["top_k"])
                else:
                    metrics = {"unannotated": True}
            except _Timeout:
                status, error, elapsed = "timeout", f"超过 {timeout_ms}ms 预算", timeout_ms
            except Exception as exc:  # 失败查询保留，不中断整批评测
                status, error = "error", str(exc)[:300]
            conn.execute(
                "INSERT INTO eval_run_queries"
                "(run_id, query_id, config_side, status, error, elapsed_ms, "
                " ranked_hits, metrics) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (run_id, query_id, config_side) DO NOTHING",
                (run_id, q["query_id"], side, status, error, elapsed,
                 json.dumps(hits, ensure_ascii=False),
                 json.dumps(metrics, ensure_ascii=False)))

    done_now = _processed_query_ids(conn, run_id)
    total = run["total_queries"]
    status_counts = conn.execute(
        "SELECT config_side, status, count(DISTINCT query_id) AS c "
        "FROM eval_run_queries WHERE run_id=%s GROUP BY 1,2", (run_id,)).fetchall()
    counts: dict[str, dict[str, int]] = {"baseline": {}, "candidate": {}}
    for r in status_counts:
        counts[r["config_side"]][r["status"]] = r["c"]
    finished = len(done_now) >= total
    new_status = "completed" if finished else ("paused" if run["status"] == "paused"
                                               else "running")
    summary = build_summary(conn, run) if finished else {}
    conn.execute(
        "UPDATE eval_runs SET done_queries=%s, status=%s, summary=%s, "
        "ok_queries=%s, timeout_queries=%s, error_queries=%s, finished_at=%s "
        "WHERE run_id=%s",
        (len(done_now), new_status, json.dumps(summary, ensure_ascii=False),
         sum(c.get("ok", 0) for c in counts.values()),
         sum(c.get("timeout", 0) for c in counts.values()),
         sum(c.get("error", 0) + c.get("no_scope", 0) for c in counts.values()),
         _now_or_none(finished), run_id))
    return dict(conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                             (run_id,)).fetchone())


def _now_or_none(flag: bool):
    from datetime import datetime, timezone
    return datetime.now(timezone.utc) if flag else None


def pause_run(conn, run_id: int) -> dict:
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                       (run_id,)).fetchone()
    if not run:
        raise EvalError("评测运行不存在")
    if run["status"] in ("completed", "failed"):
        raise EvalError(f"运行已{run['status']}，不能暂停")
    conn.execute("UPDATE eval_runs SET status='paused' WHERE run_id=%s", (run_id,))
    return dict(conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                             (run_id,)).fetchone())


def resume_run(conn, run_id: int, batch_size: int) -> dict:
    """按同一快照续接：已写入的 (run, query, side) 结果原样保留。"""
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                       (run_id,)).fetchone()
    if not run:
        raise EvalError("评测运行不存在")
    if run["status"] not in ("paused", "running"):
        raise EvalError(f"运行状态为 {run['status']}，不可续接")
    conn.execute("UPDATE eval_runs SET status='running' WHERE run_id=%s",
                 (run_id,))
    conn.commit()
    return process_run(conn, run_id, batch_size)


def decide_adoption(conn, run_id: int, decision: str, note: str,
                    username: str) -> dict:
    if decision not in ("adopted", "rejected"):
        raise EvalError("decision 必须为 adopted 或 rejected")
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=%s FOR UPDATE",
                       (run_id,)).fetchone()
    if not run:
        raise EvalError("评测运行不存在")
    if run["status"] != "completed":
        raise EvalError("仅已完成的评测可登记采用决策")
    summary = run["summary"] or {}
    if decision == "adopted" and summary.get("core_regression"):
        # 核心分组退步不禁止人工采用，但必须显式确认并留痕
        note = (note or "") + "【人工确认：存在核心分组退步仍采用】"
    conn.execute(
        "UPDATE eval_runs SET adoption_decision=%s, decided_by=%s, "
        "decision_note=%s, decided_at=now() WHERE run_id=%s",
        (decision, username, note, run_id))
    return dict(conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                             (run_id,)).fetchone())


# ---------------------------------------------------------------- 汇总统计

def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def build_summary(conn, run: dict) -> dict:
    snap = run["snapshot"]
    rows = conn.execute(
        "SELECT * FROM eval_run_queries WHERE run_id=%s ORDER BY id",
        (run["run_id"],)).fetchall()
    qmeta = {q["query_id"]: q for q in snap["queries"]}
    enriched = []
    for r in rows:
        q = qmeta.get(r["query_id"], {})
        d = dict(r)
        d["_volume"] = q.get("volume", "")
        d["_qtype"] = q.get("question_type", "")
        d["_required"] = any(a.get("required_source")
                             for a in q.get("annotations", []))
        enriched.append(d)

    def block(sub_rows: list[dict]) -> dict:
        diffs = {"recall_at_k": [], "ndcg_at_k": [], "no_result": []}
        per_side: dict[str, dict] = {}
        qids = sorted({r["query_id"] for r in sub_rows})
        for side in ("baseline", "candidate"):
            side_rows = {r["query_id"]: r for r in sub_rows
                         if r["config_side"] == side}
            scored = [r for r in side_rows.values()
                      if r["status"] == "ok" and "recall_at_k" in r["metrics"]]
            per_side[side] = {
                "n_scored": len(scored),
                "status_counts": {s: sum(1 for r in side_rows.values()
                                         if r["status"] == s)
                                  for s in ("ok", "timeout", "error", "no_scope")},
                "recall_at_k": _mean([r["metrics"]["recall_at_k"] for r in scored]),
                "ndcg_at_k": _mean([r["metrics"]["ndcg_at_k"] for r in scored]),
                "no_result_rate": _mean(
                    [1.0 if r["metrics"].get("no_result") else 0.0 for r in scored]),
                "required_recall": _mean(
                    [r["metrics"]["required_recall"] for r in scored
                     if r["metrics"].get("required_recall") is not None]),
            }
        # 成对差异：只取两侧都成功且有标注的同一查询，难例缺失不伪造为 0
        for qid in qids:
            rb = next((r for r in sub_rows if r["query_id"] == qid
                       and r["config_side"] == "baseline"), None)
            rc = next((r for r in sub_rows if r["query_id"] == qid
                       and r["config_side"] == "candidate"), None)
            if not rb or not rc or rb["status"] != "ok" or rc["status"] != "ok":
                continue
            if "recall_at_k" not in rb["metrics"] or "recall_at_k" not in rc["metrics"]:
                continue
            diffs["recall_at_k"].append(
                rc["metrics"]["recall_at_k"] - rb["metrics"]["recall_at_k"])
            diffs["ndcg_at_k"].append(
                rc["metrics"]["ndcg_at_k"] - rb["metrics"]["ndcg_at_k"])
            diffs["no_result"].append(
                (1.0 if rc["metrics"].get("no_result") else 0.0)
                - (1.0 if rb["metrics"].get("no_result") else 0.0))
        ci = {
            "recall_at_k": paired_bootstrap(diffs["recall_at_k"], run["seed"],
                                            run["bootstrap_iters"]),
            "ndcg_at_k": paired_bootstrap(diffs["ndcg_at_k"], run["seed"],
                                          run["bootstrap_iters"]),
            "no_result_rate": paired_bootstrap(diffs["no_result"], run["seed"],
                                               run["bootstrap_iters"]),
        }
        return {"n_total": len(qids), "baseline": per_side["baseline"],
                "candidate": per_side["candidate"], "ci": ci}

    overall = block(enriched)

    # 分组：册次 × 题型；小样本只报计数与均值，不宣布改进
    groups: list[dict] = []
    group_keys = sorted({(r["_volume"], r["_qtype"]) for r in enriched})
    core_regression = False
    warnings: list[str] = []
    for volume, qtype in group_keys:
        sub = [r for r in enriched if r["_volume"] == volume
               and r["_qtype"] == qtype]
        blk = block(sub)
        sufficient = blk["baseline"]["n_scored"] >= MIN_GROUP_SAMPLE and \
            blk["candidate"]["n_scored"] >= MIN_GROUP_SAMPLE
        blk["volume"] = volume
        blk["question_type"] = qtype
        blk["sufficient_sample"] = sufficient
        is_core = any(r["_required"] for r in sub)
        blk["core_group"] = is_core
        if not sufficient:
            blk["note"] = (f"有效样本 {blk['baseline']['n_scored']} < "
                           f"{MIN_GROUP_SAMPLE}，只报计数，不宣布改进")
            for metric in blk["ci"].values():
                metric["significant"] = False
                metric["direction"] = "sample_too_small"
        else:
            ndcg_delta = blk["ci"]["ndcg_at_k"]["point"]
            if is_core and ndcg_delta < 0:
                core_regression = True
                warnings.append(
                    f"核心分组（含必需出处）“{volume or '未分册'} / {qtype}”"
                    f"NDCG 退步 {ndcg_delta:+.4f}，需人工决定是否采用")
        groups.append(blk)

    unannotated = [q["query_id"] for q in snap["queries"]
                   if not effective_annotations(q["annotations"])]
    overall["groups"] = groups
    overall["core_regression"] = core_regression
    overall["warnings"] = warnings
    overall["unannotated_query_ids"] = unannotated
    overall["min_group_sample"] = MIN_GROUP_SAMPLE
    return overall


# ---------------------------------------------------------------- 逐条对比

def run_detail(conn, run_id: int) -> dict:
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=%s",
                       (run_id,)).fetchone()
    if not run:
        raise EvalError("评测运行不存在")
    snap = run["snapshot"]
    qmeta = {q["query_id"]: q for q in snap["queries"]}
    rows = conn.execute(
        "SELECT * FROM eval_run_queries WHERE run_id=%s ORDER BY query_id, config_side",
        (run_id,)).fetchall()
    per_query: dict[int, dict] = {}
    for r in rows:
        q = qmeta[r["query_id"]]
        item = per_query.setdefault(r["query_id"], {
            "query_id": r["query_id"], "query_text": q["query_text"],
            "question_type": q["question_type"], "volume": q["volume"],
            "relevance": [{"units": sorted(it["units"]), "grade": it["grade"],
                           "required": it["required"],
                           "equiv_group": it["equiv_group"]}
                          for it in build_relevance_items(
                              effective_annotations(q["annotations"]))],
            "baseline": None, "candidate": None,
            "delta": {}, "hard_failure": False})
        d = {"status": r["status"], "error": r["error"],
             "elapsed_ms": r["elapsed_ms"], "metrics": r["metrics"],
             "ranked_hits": r["ranked_hits"]}
        item[r["config_side"]] = d
        if r["status"] in ("timeout", "error", "no_scope"):
            item["hard_failure"] = True
    for item in per_query.values():
        b, c = item["baseline"], item["candidate"]
        if b and c and b["status"] == "ok" and c["status"] == "ok" and \
                "recall_at_k" in b["metrics"] and "recall_at_k" in c["metrics"]:
            item["delta"] = {
                "recall_at_k": round(c["metrics"]["recall_at_k"]
                                     - b["metrics"]["recall_at_k"], 4),
                "ndcg_at_k": round(c["metrics"]["ndcg_at_k"]
                                   - b["metrics"]["ndcg_at_k"], 4),
                "no_result": (1 if c["metrics"].get("no_result") else 0)
                - (1 if b["metrics"].get("no_result") else 0),
            }
    return {"run": _run_json(run), "summary": run["summary"],
            "per_query": [per_query[k] for k in sorted(per_query)]}


def _run_json(run: dict) -> dict:
    d = dict(run)
    for key in ("started_at", "finished_at", "decided_at", "created_at"):
        if d.get(key) is not None and hasattr(d[key], "isoformat"):
            d[key] = d[key].isoformat()
    d["snapshot"] = {"set_id": run["snapshot"]["set_id"],
                     "name": run["snapshot"]["name"],
                     "scope": run["snapshot"]["scope"],
                     "frozen_token": run["snapshot"]["frozen_token"],
                     "baseline_config": run["snapshot"]["baseline_config"],
                     "candidate_config": run["snapshot"]["candidate_config"],
                     "query_count": len(run["snapshot"]["queries"]),
                     "generation_count": len(run["snapshot"]["generations"]),
                     "generations": run["snapshot"]["generations"],
                     "disabled_units_applied":
                         run["snapshot"].get("disabled_units_applied", [])}
    return d


# ---------------------------------------------------------------- 发布兼容核对

def compatibility_report(conn, config_id: int) -> dict:
    cfg = conn.execute("SELECT * FROM eval_configs WHERE config_id=%s",
                       (config_id,)).fetchone()
    if not cfg:
        raise EvalError("配置不存在")
    settings_json = dict(cfg["settings"])
    problems: list[str] = []
    if not (isinstance(settings_json.get("bm25_k1"), (int, float))
            and settings_json["bm25_k1"] > 0):
        problems.append("bm25_k1 必须为正数")
    if not (isinstance(settings_json.get("bm25_b"), (int, float))
            and 0 <= settings_json["bm25_b"] <= 1):
        problems.append("bm25_b 必须落在 [0,1]")
    if not isinstance(settings_json.get("synonym_enabled"), bool):
        problems.append("synonym_enabled 必须为布尔值")
    if not isinstance(settings_json.get("bigram_enabled"), bool):
        problems.append("bigram_enabled 必须为布尔值")

    gens = conn.execute(
        "SELECT g.*, e.generation AS edition_generation FROM index_generations g "
        "JOIN textbook_editions e ON e.edition_id=g.edition_id "
        "WHERE g.status IN ('ready','archived') ORDER BY g.generation_id").fetchall()
    gen_reports = []
    for g in gens:
        item = {"generation_id": g["generation_id"], "isbn": g["isbn"],
                "status": g["status"], "ok": True, "problems": []}
        st = conn.execute("SELECT * FROM index_stats WHERE generation_id=%s",
                          (g["generation_id"],)).fetchone()
        if not st:
            item["ok"] = False
            item["problems"].append("缺少 index_stats，无法按该索引评分")
        unit_count = conn.execute(
            "SELECT count(*) AS c FROM structural_units WHERE generation_id=%s",
            (g["generation_id"],)).fetchone()["c"]
        if unit_count == 0:
            item["ok"] = False
            item["problems"].append("索引内没有任何结构单元")
        item["unit_count"] = unit_count
        if not item["ok"]:
            problems.append(f"代次 {g['generation_id']}：{'；'.join(item['problems'])}")
        gen_reports.append(item)
    if not gens:
        problems.append("当前没有任何 ready/archived 索引，配置无法核对兼容性")
    return {"config_id": config_id, "compatible": not problems,
            "problems": problems, "generations": gen_reports,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
