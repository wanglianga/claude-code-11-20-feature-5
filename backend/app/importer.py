"""教材导入与索引构建管线。

关键约束：
- 段落与例题是独立结构单元，不做等长切块；长段按句拆开并保存原文偏移区间。
- 缺失图表引用、重复段落标识、例题缺前提 -> parse_issues 待修正。
- 逐章报告 接收/解析/词项/核查 四个阶段数量；坏章保留错误，整本不齐全则构建失败。
- 每个代次在独立 generation 隔离空间构建，成功后由编辑批准时原子切换；
  构建过程绝不改动现行指针，旧作业不可能覆盖后完成的新稿。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from . import text as textmod
from .config import settings
from .db import tx

FIGURE_REF_RE = re.compile(r"\{\{FIG:([^}]+)\}\}")


class BuildError(Exception):
    pass


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_text_file(base_dir: str, rel: str) -> str:
    path = os.path.join(base_dir, rel)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def _report(conn, generation_id: int, chapter_code: str, stage: str,
            status: str, counts: dict | None = None, error: str = "") -> None:
    conn.execute(
        "INSERT INTO index_chapter_reports"
        "(generation_id, chapter_code, stage, status, counts, error) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (generation_id, chapter_code, stage, status, json.dumps(counts or {},
                                                                 ensure_ascii=False), error),
    )


def _issue(conn, edition_id: int, generation_id: int, issue_type: str,
           chapter_code: str = "", unit_code: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO parse_issues"
        "(edition_id, generation_id, chapter_code, unit_code, issue_type, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (edition_id, generation_id, chapter_code, unit_code, issue_type, detail),
    )


def _create_generation(conn, edition_id: int, isbn: str, job_token: str) -> int:
    row = conn.execute(
        "INSERT INTO index_generations(edition_id, isbn, job_token, status) "
        "VALUES (%s,%s,%s,'building') RETURNING generation_id",
        (edition_id, isbn, job_token),
    ).fetchone()
    return row["generation_id"]


def _upsert_book_and_edition(conn, payload: dict) -> tuple[int, str]:
    book = payload["book"]
    conn.execute(
        "INSERT INTO textbooks(isbn, title, subject, stage, volume, semester, publisher) "
        "VALUES (%(isbn)s,%(title)s,%(subject)s,%(stage)s,%(volume)s,%(semester)s,%(publisher)s) "
        "ON CONFLICT (isbn) DO UPDATE SET title=EXCLUDED.title, subject=EXCLUDED.subject, "
        "stage=EXCLUDED.stage, volume=EXCLUDED.volume, semester=EXCLUDED.semester, "
        "publisher=EXCLUDED.publisher",
        book,
    )
    ed = payload["edition"]
    existing = conn.execute(
        "SELECT edition_id FROM textbook_editions WHERE isbn=%s AND generation=%s",
        (book["isbn"], ed["generation"]),
    ).fetchone()
    if existing:
        # 同一代次重复构建视为新作业：旧代次行不再复用，要求数据包提升 generation
        raise BuildError(
            f"ISBN {book['isbn']} 第 {ed['generation']} 代已存在，请提升数据包 generation 后重新构建"
        )
    row = conn.execute(
        "INSERT INTO textbook_editions"
        "(isbn, generation, title, stage, subject, volume, semester, status, supersede_reason) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'draft',%s) RETURNING edition_id",
        (book["isbn"], ed["generation"], book["title"], book["stage"], book["subject"],
         book.get("volume", ""), book.get("semester", ""), ed.get("supersede_reason", "")),
    ).fetchone()
    return row["edition_id"], book["isbn"]


def _collect_units(chapter: dict, base_dir: str) -> list[dict]:
    """展开章节树为扁平单元列表，保留标题路径与结构边界。"""
    units: list[dict] = []
    chapter_path = f"{chapter['code']} {chapter['title']}".strip()
    ordinal = 0

    def walk(section: dict, title_path: str) -> None:
        nonlocal ordinal
        section_path = f"{chapter_path} / {title_path}".strip(" /")
        for raw in section.get("units", []):
            ordinal += 1
            unit = dict(raw)
            if "text_file" in unit:
                unit["text"] = _read_text_file(base_dir, unit["text_file"])
            unit["chapter_path"] = chapter_path
            unit["section_path"] = section_path
            unit["title_path"] = f"{title_path} {unit.get('title', '')}".strip()
            unit["ordinal_in_book"] = ordinal
            units.append(unit)
        for child in section.get("children", []):
            walk(child, f"{title_path} {child['title']}".strip())

    for section in chapter.get("sections", []):
        walk(section, section["title"])
    return units


def _parse_chapter(conn, edition_id: int, generation_id: int, chapter: dict,
                   base_dir: str, figures: dict, seen_ids: set) -> dict:
    """解析单章并写入结构单元，返回该章统计。"""
    code = chapter["code"]
    _report(conn, generation_id, code, "received", "ok",
            {"declared_sections": len(chapter.get("sections", []))})

    if chapter.get("broken"):
        raise BuildError(f"章节 {code} 被标记为损坏（broken=true），无法解析结构边界")

    flat = _collect_units(chapter, base_dir)
    if not flat:
        raise BuildError(f"章节 {code} 没有任何结构单元")

    counts = {"units": 0, "paragraphs": 0, "examples": 0, "long_split": 0,
              "duplicate_ids": 0, "missing_figures": 0, "missing_premise": 0}
    chapter_snapshot_units: list[dict] = []

    for unit in flat:
        uc = unit["id"]
        if uc in seen_ids:
            counts["duplicate_ids"] += 1
            _issue(conn, edition_id, generation_id, "duplicate_id",
                   chapter_code=code, unit_code=uc,
                   detail=f"段落标识 {uc} 在教材内重复，后出现的单元未入索引，待编辑修正")
            continue
        seen_ids.add(uc)

        text_body = unit.get("text", "")
        if not text_body:
            raise BuildError(f"单元 {uc} 正文为空")

        # 例题缺前提：显式声明需要前提条件但未提供
        if unit.get("type") == "example":
            counts["examples"] += 1
            if unit.get("premise_required") and not unit.get("conditions"):
                counts["missing_premise"] += 1
                _issue(conn, edition_id, generation_id, "missing_premise",
                       chapter_code=code, unit_code=uc,
                       detail="例题声明需要前提条件，但数据包未给出 conditions，待编辑补全")
        else:
            counts["paragraphs"] += 1

        # 图表引用核查：正文引用的图必须在图表说明中存在
        for ref in FIGURE_REF_RE.findall(text_body):
            fig_key = ref if ref.startswith("FIG:") else f"FIG:{ref}"
            if fig_key not in figures:
                counts["missing_figures"] += 1
                _issue(conn, edition_id, generation_id, "missing_figure",
                       chapter_code=code, unit_code=uc,
                       detail=f"正文引用图表 {fig_key}，但图表说明缺失，待编辑补图")

        norm_text, mapping = textmod.normalize(text_body)
        ranges = textmod.split_sentences(text_body, settings.long_paragraph_threshold)
        if len(ranges) > 1:
            counts["long_split"] += 1
        # 核查：句子区间必须完整覆盖原文且互不重叠
        covered = "".join(text_body[s:e] for s, e in ranges)
        if covered != text_body:
            raise BuildError(f"单元 {uc} 分句区间未能完整覆盖原文")

        conn.execute(
            "INSERT INTO structural_units"
            "(edition_id, generation_id, chapter_code, unit_code, unit_type, "
            " section_path, title_path, ordinal_in_book, text, conditions, content_hash, "
            " norm_text, norm_mapping, sentence_ranges) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (edition_id, generation_id, code, uc, unit["type"],
             unit["section_path"], unit["title_path"], unit["ordinal_in_book"],
             text_body, json.dumps(unit.get("conditions", []), ensure_ascii=False),
             textmod.fingerprint(text_body), norm_text,
             json.dumps(mapping), json.dumps(ranges)),
        )
        chapter_snapshot_units.append({"id": uc, "type": unit["type"]})
        counts["units"] += 1

    # 图表说明本身作为独立 figure_note 单元入库（不与正文拼接）
    for fig_id, fig in figures.items():
        if fig.get("chapter_code") != code:
            continue
        fc = f"fig-{fig_id}"
        if fc in seen_ids:
            continue
        seen_ids.add(fc)
        caption = f"【图表说明】{fig.get('caption', fig_id)}：{fig.get('description', '')}"
        norm_text, mapping = textmod.normalize(caption)
        ranges = textmod.split_sentences(caption, settings.long_paragraph_threshold)
        conn.execute(
            "INSERT INTO structural_units"
            "(edition_id, generation_id, chapter_code, unit_code, unit_type, "
            " section_path, title_path, ordinal_in_book, text, conditions, content_hash, "
            " norm_text, norm_mapping, sentence_ranges) "
            "VALUES (%s,%s,%s,%s,'figure_note',%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (edition_id, generation_id, code, fc,
             f"{chapter['code']} {chapter['title']} / 图表说明",
             fig.get("caption", fig_id), 10_000 + len(seen_ids),
             caption, json.dumps([], ensure_ascii=False),
             textmod.fingerprint(caption), norm_text,
             json.dumps(mapping), json.dumps(ranges)),
        )
        counts["units"] += 1

    conn.execute(
        "INSERT INTO chapter_snapshots"
        "(edition_id, generation_id, chapter_code, chapter_path, ordinal, status, payload) "
        "VALUES (%s,%s,%s,%s,%s,'ok',%s)",
        (edition_id, generation_id, code,
         f"{chapter['code']} {chapter['title']}", chapter.get("ordinal", 0),
         json.dumps({"units": chapter_snapshot_units}, ensure_ascii=False)),
    )
    _report(conn, generation_id, code, "parsed", "ok", counts)
    return counts


def _build_terms(conn, generation_id: int) -> dict:
    """对代次内全部单元构建倒排 postings 与 df 统计。"""
    units = conn.execute(
        "SELECT unit_id, norm_text FROM structural_units WHERE generation_id=%s",
        (generation_id,),
    ).fetchall()
    total_terms: set[str] = set()
    total_postings = 0
    for row in units:
        tokens = textmod.tokenize(row["norm_text"])
        agg: dict[str, dict] = {}
        for tok in tokens:
            item = agg.setdefault(tok.term, {"tf": 0, "positions": []})
            item["tf"] += 1
            item["positions"].append([tok.start, tok.end])
        for term, item in agg.items():
            conn.execute(
                "INSERT INTO postings(generation_id, term, unit_id, tf, positions) "
                "VALUES (%s,%s,%s,%s,%s)",
                (generation_id, term, row["unit_id"], item["tf"],
                 json.dumps(item["positions"])),
            )
            total_postings += 1
            total_terms.add(term)

    dfs = conn.execute(
        "SELECT term, count(*) AS df, sum(tf) AS collection_tf "
        "FROM postings WHERE generation_id=%s GROUP BY term",
        (generation_id,),
    ).fetchall()
    for row in dfs:
        conn.execute(
            "INSERT INTO term_dfs(generation_id, term, df, collection_tf) "
            "VALUES (%s,%s,%s,%s)",
            (generation_id, row["term"], row["df"], int(row["collection_tf"])),
        )

    stats = conn.execute(
        "SELECT count(*) AS n, coalesce(avg(char_length(norm_text)),0) AS avgdl "
        "FROM structural_units WHERE generation_id=%s",
        (generation_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO index_stats(generation_id, doc_count, avgdl, bm25_k1, bm25_b) "
        "VALUES (%s,%s,%s,%s,%s)",
        (generation_id, stats["n"], float(stats["avgdl"]),
         settings.bm25_k1, settings.bm25_b),
    )
    return {"terms": len(total_terms), "postings": total_postings,
            "units": len(units), "chapters_terms": len(units)}


def build_from_payload(payload_path: str, chapter_codes: list[str] | None = None,
                       username: str = "system") -> dict:
    """主入口：从数据包构建一个新代次（隔离构建，不触碰现行索引）。"""
    payload_dir = os.path.dirname(os.path.abspath(payload_path))
    payload = _read_json(payload_path)
    job_token = str(uuid.uuid4())

    with tx() as conn:
        edition_id, isbn = _upsert_book_and_edition(conn, payload)
        generation_id = _create_generation(conn, edition_id, isbn, job_token)
        conn.commit()

    all_chapters = payload["chapters"]
    all_codes = [c["code"] for c in all_chapters]
    requested = chapter_codes or all_codes
    missing = [c for c in requested if c not in all_codes]
    figures = payload.get("figures", {})

    chapter_results: list[dict] = []
    seen_ids: set[str] = set()
    failed = False

    for chapter in all_chapters:
        code = chapter["code"]
        if code not in requested:
            # 未接收到的章节：构建中断场景，直接记为失败缺口
            failed = True
            with tx() as conn:
                _report(conn, generation_id, code, "received", "error",
                        error="构建中断：该章节未被作业接收，整本索引不齐全")
                _issue(conn, edition_id, generation_id, "chapter_error",
                       chapter_code=code, detail="章节缺失，构建作业未接收到该章")
            chapter_results.append({"chapter_code": code, "status": "error",
                                    "error": "未接收（构建中断）"})
            continue

        # 支持章节外置 JSON 文件；文件缺失即坏章
        chapter_file = chapter.get("file")
        try:
            if chapter_file:
                ch_path = os.path.join(payload_dir, chapter_file)
                if not os.path.exists(ch_path):
                    raise BuildError(f"章节文件 {chapter_file} 不存在（坏章/构建中断）")
                chapter_data = _read_json(ch_path)
                chapter_data.setdefault("code", code)
                chapter_data.setdefault("title", chapter.get("title", code))
            else:
                chapter_data = chapter
            with tx() as conn:
                counts = _parse_chapter(conn, edition_id, generation_id,
                                        chapter_data, payload_dir, figures, seen_ids)
                conn.commit()
            chapter_results.append({"chapter_code": code, "status": "ok",
                                    "counts": counts})
        except Exception as exc:  # 坏章保留错误，继续处理其余章
            failed = True
            with tx() as conn:
                _report(conn, generation_id, code, "parsed", "error", error=str(exc))
                _issue(conn, edition_id, generation_id, "chapter_error",
                       chapter_code=code, detail=str(exc))
                conn.commit()
            chapter_results.append({"chapter_code": code, "status": "error",
                                    "error": str(exc)})

    # 显式指定但树中不存在的章节代码
    for code in missing:
        failed = True
        with tx() as conn:
            _report(conn, generation_id, code, "received", "error",
                    error=f"请求构建的章节 {code} 在章节树中不存在")
            _issue(conn, edition_id, generation_id, "chapter_error",
                   chapter_code=code, detail="章节代码不存在于章节树")
        chapter_results.append({"chapter_code": code, "status": "error",
                                "error": "章节树中不存在"})

    with tx() as conn:
        total_chapters = len(all_codes)
        ok_chapters = sum(1 for r in chapter_results if r["status"] == "ok")
        failed_chapters = total_chapters - ok_chapters
        conn.execute(
            "UPDATE index_generations SET total_chapters=%s, ok_chapters=%s, "
            "failed_chapters=%s WHERE generation_id=%s",
            (total_chapters, ok_chapters, failed_chapters, generation_id),
        )

        if failed:
            conn.execute(
                "UPDATE index_generations SET status='failed', finished_at=now(), "
                "error=%s WHERE generation_id=%s",
                (f"{failed_chapters} 个章节缺失或损坏，章节不齐全，"
                 "整本索引不可用，待编辑修正后重新构建",
                 generation_id),
            )
            conn.execute(
                "UPDATE textbook_editions SET status='rejected' WHERE edition_id=%s",
                (edition_id,),
            )
            conn.commit()
            return _load_report(conn, generation_id, chapter_results)

        # 全部章节解析成功 -> 词项构建
        term_stats = _build_terms(conn, generation_id)
        for r in chapter_results:
            if r["status"] == "ok":
                _report(conn, generation_id, r["chapter_code"], "terms", "ok",
                        {"postings": term_stats["postings"]})
        # 核查阶段：单元数、句子覆盖、图表引用、重复标识
        verify_counts = conn.execute(
            "SELECT count(*) AS units, count(DISTINCT chapter_code) AS chapters "
            "FROM structural_units WHERE generation_id=%s",
            (generation_id,),
        ).fetchone()
        open_issues = conn.execute(
            "SELECT count(*) AS c FROM parse_issues WHERE generation_id=%s AND status='open'",
            (generation_id,),
        ).fetchone()["c"]
        for r in chapter_results:
            if r["status"] == "ok":
                _report(conn, generation_id, r["chapter_code"], "verified", "ok",
                        {"open_issues": open_issues})
        conn.execute(
            "UPDATE index_generations SET status='ready', finished_at=now(), "
            "total_units=%s, total_terms=%s, total_postings=%s WHERE generation_id=%s",
            (term_stats["units"], term_stats["terms"], term_stats["postings"],
             generation_id),
        )
        # 成功构建自动登记“替代/启用申请”，由编辑批准后才切换现行索引
        old_edition = conn.execute(
            "SELECT edition_id FROM textbook_editions WHERE isbn=%s AND status='active' "
            "ORDER BY generation DESC LIMIT 1",
            (isbn,),
        ).fetchone()
        conn.execute(
            "INSERT INTO replacement_requests"
            "(isbn, old_edition_id, new_edition_id, reason, status) "
            "VALUES (%s,%s,%s,%s,'pending')",
            (isbn,
             old_edition["edition_id"] if old_edition else None,
             edition_id, payload["edition"].get("supersede_reason", "")),
        )
        conn.commit()
        return _load_report(conn, generation_id, chapter_results)


def _load_report(conn, generation_id: int, chapter_results: list[dict]) -> dict:
    gen = conn.execute(
        "SELECT * FROM index_generations WHERE generation_id=%s",
        (generation_id,),
    ).fetchone()
    issues = conn.execute(
        "SELECT issue_id, chapter_code, unit_code, issue_type, detail, status "
        "FROM parse_issues WHERE generation_id=%s ORDER BY issue_id",
        (generation_id,),
    ).fetchall()
    reports = conn.execute(
        "SELECT chapter_code, stage, status, counts, error "
        "FROM index_chapter_reports WHERE generation_id=%s ORDER BY id",
        (generation_id,),
    ).fetchall()
    return {
        "generation_id": generation_id,
        "status": gen["status"],
        "total_chapters": gen["total_chapters"],
        "ok_chapters": gen["ok_chapters"],
        "failed_chapters": gen["failed_chapters"],
        "total_units": gen["total_units"],
        "total_terms": gen["total_terms"],
        "total_postings": gen["total_postings"],
        "error": gen["error"],
        "chapters": chapter_results,
        "stage_reports": [dict(r) for r in reports],
        "issues": [dict(i) for i in issues],
    }
