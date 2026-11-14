"""PostgreSQL 连接池与 schema 初始化（启动时幂等建表）。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None

DDL = r"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.users (
    username     text PRIMARY KEY,
    display_name text NOT NULL,
    role         text NOT NULL CHECK (role IN ('editor', 'teacher'))
);

CREATE TABLE IF NOT EXISTS {schema}.textbooks (
    isbn                    text PRIMARY KEY,
    title                   text NOT NULL,
    subject                 text NOT NULL,
    stage                   text NOT NULL,
    volume                  text NOT NULL DEFAULT '',
    semester                text NOT NULL DEFAULT '',
    publisher               text NOT NULL DEFAULT '',
    current_generation_id   bigint,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.textbook_editions (
    edition_id              bigserial PRIMARY KEY,
    isbn                    text NOT NULL REFERENCES {schema}.textbooks(isbn),
    generation              integer NOT NULL,
    title                   text NOT NULL,
    stage                   text NOT NULL,
    subject                 text NOT NULL,
    volume                  text NOT NULL DEFAULT '',
    semester                text NOT NULL DEFAULT '',
    status                  text NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft', 'active', 'deprecated', 'rejected')),
    supersede_reason        text NOT NULL DEFAULT '',
    superseded_by           bigint,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (isbn, generation)
);

CREATE TABLE IF NOT EXISTS {schema}.replacement_requests (
    request_id      bigserial PRIMARY KEY,
    isbn            text NOT NULL,
    old_edition_id  bigint,
    new_edition_id  bigint NOT NULL,
    reason          text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by      text,
    decided_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.index_generations (
    generation_id   bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    isbn            text NOT NULL,
    job_token       text NOT NULL,
    status          text NOT NULL DEFAULT 'building'
                    CHECK (status IN ('building', 'ready', 'failed', 'abandoned',
                                      'archived', 'retired')),
    total_chapters  integer NOT NULL DEFAULT 0,
    ok_chapters     integer NOT NULL DEFAULT 0,
    failed_chapters integer NOT NULL DEFAULT 0,
    total_units     integer NOT NULL DEFAULT 0,
    total_terms     integer NOT NULL DEFAULT 0,
    total_postings  integer NOT NULL DEFAULT 0,
    error           text NOT NULL DEFAULT '',
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

CREATE TABLE IF NOT EXISTS {schema}.chapter_snapshots (
    id              bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL,
    chapter_path    text NOT NULL,
    ordinal         integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('ok', 'error')),
    error           text NOT NULL DEFAULT '',
    payload         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    UNIQUE (generation_id, chapter_code)
);

CREATE TABLE IF NOT EXISTS {schema}.structural_units (
    unit_id         bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL,
    unit_code       text NOT NULL,
    unit_type       text NOT NULL CHECK (unit_type IN ('paragraph', 'example', 'figure_note')),
    section_path    text NOT NULL,
    title_path      text NOT NULL,
    ordinal_in_book integer NOT NULL,
    parent_unit_id  bigint,
    text            text NOT NULL,
    conditions      jsonb NOT NULL DEFAULT '[]'::jsonb,
    content_hash    text NOT NULL,
    norm_text       text NOT NULL,
    norm_mapping    jsonb NOT NULL,
    sentence_ranges jsonb NOT NULL,
    UNIQUE (edition_id, unit_code)
);
CREATE INDEX IF NOT EXISTS idx_units_generation ON {schema}.structural_units (generation_id);
CREATE INDEX IF NOT EXISTS idx_units_edition ON {schema}.structural_units (edition_id);

CREATE TABLE IF NOT EXISTS {schema}.index_chapter_reports (
    id              bigserial PRIMARY KEY,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL,
    stage           text NOT NULL CHECK (stage IN ('received', 'parsed', 'terms', 'verified')),
    status          text NOT NULL CHECK (status IN ('ok', 'error')),
    counts          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    error           text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_gen ON {schema}.index_chapter_reports (generation_id);

CREATE TABLE IF NOT EXISTS {schema}.postings (
    generation_id   bigint NOT NULL,
    term            text NOT NULL,
    unit_id         bigint NOT NULL,
    tf              integer NOT NULL,
    positions       jsonb NOT NULL,
    PRIMARY KEY (generation_id, term, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_postings_unit ON {schema}.structural_units (generation_id, unit_id);
CREATE INDEX IF NOT EXISTS idx_postings_term ON {schema}.postings (generation_id, term);

CREATE TABLE IF NOT EXISTS {schema}.term_dfs (
    generation_id   bigint NOT NULL,
    term            text NOT NULL,
    df              integer NOT NULL,
    collection_tf   integer NOT NULL,
    PRIMARY KEY (generation_id, term)
);

CREATE TABLE IF NOT EXISTS {schema}.index_stats (
    generation_id   bigint PRIMARY KEY,
    doc_count       integer NOT NULL,
    avgdl           numeric NOT NULL,
    bm25_k1         numeric NOT NULL,
    bm25_b          numeric NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.synonyms (
    synonym_id      bigserial PRIMARY KEY,
    subject         text NOT NULL,
    term_norm       text NOT NULL,
    term_display    text NOT NULL,
    synonym_norm    text NOT NULL,
    synonym_display text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    created_by      text NOT NULL,
    reviewed_by     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_at     timestamptz,
    UNIQUE (subject, term_norm, synonym_norm)
);

CREATE TABLE IF NOT EXISTS {schema}.chapter_corrections (
    correction_id   bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    chapter_code    text NOT NULL,
    payload         jsonb NOT NULL,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (edition_id, chapter_code)
);

CREATE TABLE IF NOT EXISTS {schema}.parse_issues (
    issue_id        bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL DEFAULT '',
    unit_code       text NOT NULL DEFAULT '',
    issue_type      text NOT NULL CHECK (issue_type IN (
                        'missing_figure', 'duplicate_id', 'missing_premise',
                        'parse_error', 'chapter_error')),
    detail          text NOT NULL,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_issues_gen ON {schema}.parse_issues (generation_id);

CREATE TABLE IF NOT EXISTS {schema}.search_sessions (
    session_id      uuid PRIMARY KEY,
    username        text NOT NULL,
    generation_id   bigint NOT NULL,
    query_text      text NOT NULL,
    filters         jsonb NOT NULL,
    query_terms     jsonb NOT NULL,
    expanded_terms  jsonb NOT NULL,
    synonym_enabled boolean NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.query_log (
    id              bigserial PRIMARY KEY,
    username        text NOT NULL,
    query_text      text NOT NULL,
    query_terms     jsonb NOT NULL,
    expanded_terms  jsonb NOT NULL,
    filters         jsonb NOT NULL,
    generation_id   bigint,
    hit_count       integer NOT NULL,
    no_hit_reason   text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.citations (
    citation_id     bigserial PRIMARY KEY,
    username        text NOT NULL,
    generation_id   bigint NOT NULL,
    edition_id      bigint NOT NULL,
    unit_id         bigint NOT NULL,
    unit_code       text NOT NULL,
    quoted_text     text NOT NULL,
    content_hash    text NOT NULL,
    locator         jsonb NOT NULL,
    last_status     text NOT NULL DEFAULT 'ok'
                    CHECK (last_status IN ('ok', 'drifted', 'edition_unavailable')),
    last_detail     text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz
);

CREATE TABLE IF NOT EXISTS {schema}.citation_checks (
    id              bigserial PRIMARY KEY,
    citation_id     bigint NOT NULL,
    status          text NOT NULL,
    detail          text NOT NULL,
    checked_at      timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 候选配置评测：查询集 / 标注 / 配置 / 运行快照 / 逐条结果
-- ============================================================

CREATE TABLE IF NOT EXISTS {schema}.eval_query_sets (
    set_id          bigserial PRIMARY KEY,
    name            text NOT NULL,
    description     text NOT NULL DEFAULT '',
    scope           jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'frozen', 'archived')),
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    frozen_at       timestamptz
);

CREATE TABLE IF NOT EXISTS {schema}.eval_queries (
    query_id        bigserial PRIMARY KEY,
    set_id          bigint NOT NULL REFERENCES {schema}.eval_query_sets(set_id),
    query_text      text NOT NULL,
    question_type   text NOT NULL DEFAULT 'concept',
    volume          text NOT NULL DEFAULT '',
    note            text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'disabled')),
    disabled_reason text NOT NULL DEFAULT '',
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (set_id, query_text)
);
CREATE INDEX IF NOT EXISTS idx_eval_queries_set ON {schema}.eval_queries (set_id);

CREATE TABLE IF NOT EXISTS {schema}.eval_annotations (
    annotation_id   bigserial PRIMARY KEY,
    query_id        bigint NOT NULL REFERENCES {schema}.eval_queries(query_id),
    unit_code       text NOT NULL,
    grade           integer NOT NULL CHECK (grade BETWEEN 1 AND 3),
    required_source boolean NOT NULL DEFAULT false,
    equiv_group     uuid,
    annotator       text NOT NULL,
    note            text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'adjudicated', 'rejected')),
    adjudicated_by  text,
    adjudicated_at  timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (query_id, unit_code, annotator)
);
CREATE INDEX IF NOT EXISTS idx_eval_ann_query ON {schema}.eval_annotations (query_id);

CREATE TABLE IF NOT EXISTS {schema}.eval_configs (
    config_id       bigserial PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    description     text NOT NULL DEFAULT '',
    settings        jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate', 'baseline', 'published', 'retired')),
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    compat_report   jsonb NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS {schema}.eval_runs (
    run_id              bigserial PRIMARY KEY,
    set_id              bigint NOT NULL REFERENCES {schema}.eval_query_sets(set_id),
    baseline_config_id  bigint NOT NULL REFERENCES {schema}.eval_configs(config_id),
    candidate_config_id bigint NOT NULL REFERENCES {schema}.eval_configs(config_id),
    status              text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'paused', 'completed', 'failed')),
    seed                integer NOT NULL,
    bootstrap_iters     integer NOT NULL DEFAULT 1000,
    top_k               integer NOT NULL DEFAULT 10,
    eval_timeout_ms     integer NOT NULL DEFAULT 2000,
    snapshot            jsonb NOT NULL,
    total_queries       integer NOT NULL DEFAULT 0,
    done_queries        integer NOT NULL DEFAULT 0,
    ok_queries          integer NOT NULL DEFAULT 0,
    timeout_queries     integer NOT NULL DEFAULT 0,
    error_queries       integer NOT NULL DEFAULT 0,
    summary             jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    adoption_decision   text CHECK (adoption_decision IS NULL
                        OR adoption_decision IN ('adopted', 'rejected')),
    decided_by          text,
    decided_at          timestamptz,
    decision_note       text NOT NULL DEFAULT '',
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_set ON {schema}.eval_runs (set_id);

CREATE TABLE IF NOT EXISTS {schema}.eval_run_queries (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES {schema}.eval_runs(run_id),
    query_id        bigint NOT NULL,
    config_side     text NOT NULL CHECK (config_side IN ('baseline', 'candidate')),
    status          text NOT NULL CHECK (status IN ('ok', 'timeout', 'error', 'no_scope')),
    error           text NOT NULL DEFAULT '',
    elapsed_ms      integer NOT NULL DEFAULT 0,
    ranked_hits     jsonb NOT NULL DEFAULT '[]'::jsonb,
    metrics         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    UNIQUE (run_id, query_id, config_side)
);
CREATE INDEX IF NOT EXISTS idx_eval_rq_run ON {schema}.eval_run_queries (run_id);

CREATE TABLE IF NOT EXISTS {schema}.eval_disabled_samples (
    sample_id       bigserial PRIMARY KEY,
    generation_id   bigint NOT NULL,
    unit_code       text NOT NULL,
    reason          text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'applied', 'ignored')),
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    decided_by      text,
    decided_at      timestamptz,
    UNIQUE (generation_id, unit_code)
);
"""


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={
                "row_factory": dict_row,
                "options": f"-c search_path={settings.search_schema}",
            },
            check=ConnectionPool.check_connection,
        )
    return _pool


@contextmanager
def tx() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    with get_pool().connection() as conn:
        yield conn


def init_schema() -> None:
    with tx() as conn:
        conn.execute(DDL.format(schema=settings.search_schema))
        # 幂等迁移：为已存在的旧表补齐后加字段
        conn.execute(
            "ALTER TABLE " + settings.search_schema +
            ".structural_units ADD COLUMN IF NOT EXISTS conditions jsonb "
            "NOT NULL DEFAULT '[]'::jsonb")
        conn.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(settings.search_schema)
            )
        )
        conn.execute(
            "INSERT INTO users(username, display_name, role) VALUES "
            "('editor1', '王编辑', 'editor'), ('teacher1', '李老师', 'teacher'), "
            "('teacher2', '赵老师', 'teacher') "
            "ON CONFLICT (username) DO NOTHING"
        )
        conn.commit()
