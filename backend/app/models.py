"""API 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str


class UserOut(BaseModel):
    username: str
    display_name: str
    role: str


class LoginOut(BaseModel):
    token: str
    user: UserOut


class TextbookOut(BaseModel):
    isbn: str
    title: str
    subject: str
    stage: str
    volume: str
    semester: str
    publisher: str
    active_generation_id: int | None
    active_edition_id: int | None
    active_status: str | None
    has_older: bool = False


class EditionOut(BaseModel):
    edition_id: int
    isbn: str
    generation: int
    title: str
    stage: str
    subject: str
    volume: str
    semester: str
    status: str
    supersede_reason: str
    superseded_by: int | None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    isbns: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    include_deprecated: bool = False
    synonym_enabled: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=50)
    session_id: str | None = None  # 固定翻页代次用


class TermEvidence(BaseModel):
    term: str
    source: str  # original | synonym
    via: str | None = None
    df: int
    idf: float
    matched_units: int


class SearchHit(BaseModel):
    unit_id: int
    unit_code: str
    unit_type: str
    chapter_path: str
    section_path: str
    title_path: str
    isbn: str
    textbook_title: str
    edition_id: int
    generation_id: int
    edition_status: str
    supersede_reason: str
    score: float
    rank: int
    matched_terms: list[str]
    term_contributions: dict[str, float]
    positions: dict[str, list[list[int]]]  # term -> 原文偏移区间
    summary: str
    summary_range: list[int]
    sentence_ranges: list[list[int]]
    text: str


class SearchResponse(BaseModel):
    session_id: str
    query: str
    normalized_query: str
    query_terms: list[str]
    expanded_terms: list[TermEvidence]
    synonym_enabled: bool
    filters: dict
    generation_ids: list[int]
    total: int
    page: int
    page_size: int
    hits: list[SearchHit]
    no_hit: bool
    no_hit_reason: str
    collection_scope: str


class UnitOut(BaseModel):
    unit_id: int
    unit_code: str
    unit_type: str
    chapter_path: str
    section_path: str
    title_path: str
    text: str
    sentence_ranges: list[list[int]]
    generation_id: int
    edition_id: int
    content_hash: str
    sibling_units: list[dict]


class CitationCreate(BaseModel):
    session_id: str
    unit_id: int
    quoted_text: str
    locator: dict | None = None


class CitationOut(BaseModel):
    citation_id: int
    generation_id: int
    edition_id: int
    unit_code: str
    quoted_text: str
    content_hash: str
    locator: dict
    last_status: str
    last_detail: str
    created_at: str
    last_checked_at: str | None


class SynonymIn(BaseModel):
    subject: str
    term_display: str
    synonym_display: str


class SynonymOut(BaseModel):
    synonym_id: int
    subject: str
    term_display: str
    synonym_display: str
    status: str
    created_by: str
    reviewed_by: str | None


class ReplacementIn(BaseModel):
    isbn: str
    old_edition_id: int | None = None
    new_edition_id: int
    reason: str = ""


class BuildRequest(BaseModel):
    isbn: str
    payload_rel: str | None = None  # 相对 data_dir 的数据包路径，默认 <isbn>/payload.json
    chapter_codes: list[str] | None = None  # 仅构建部分章节 -> 模拟构建中断/坏章
    fail_incomplete: bool = True


class BuildReport(BaseModel):
    generation_id: int
    status: str
    total_chapters: int
    ok_chapters: int
    failed_chapters: int
    total_units: int
    total_terms: int
    total_postings: int
    error: str
    chapters: list[dict]
    issues: list[dict]


# ---------------------------------------------------------------- 候选配置评测

class QuerySetIn(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    scope: dict = Field(default_factory=dict)


class EvalQueryIn(BaseModel):
    query_text: str = Field(min_length=1)
    question_type: str = "concept"
    volume: str = ""
    note: str = ""


class AnnotationIn(BaseModel):
    unit_code: str = Field(min_length=1)
    grade: int = Field(ge=1, le=3)
    required_source: bool = False
    equiv_group: str | None = None
    note: str = ""


class AdjudicationIn(BaseModel):
    query_id: int
    # 教研员裁定后的有效标注集合（全量覆盖该查询的 proposed 标注）
    annotations: list[AnnotationIn]


class ConfigIn(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    settings: dict


class RunCreateIn(BaseModel):
    set_id: int
    baseline_config_id: int
    candidate_config_id: int
    seed: int = 20261114
    bootstrap_iters: int = Field(default=1000, ge=100, le=10000)
    top_k: int = Field(default=10, ge=1, le=50)
    eval_timeout_ms: int = Field(default=2000, ge=50, le=30000)
    batch_size: int = Field(default=20, ge=1, le=200)


class AdoptionIn(BaseModel):
    decision: str  # adopted | rejected
    note: str = ""


class DisabledSampleIn(BaseModel):
    generation_id: int
    unit_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
