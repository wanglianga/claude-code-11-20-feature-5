"""pytest 全局夹具：独立 schema 隔离，不污染开发库；关闭自动播种，测试自行控制数据。"""
import os
import uuid

os.environ.setdefault("SEARCH_SCHEMA", f"test_{uuid.uuid4().hex[:12]}")
os.environ["AUTO_SEED"] = "false"

import pytest
from fastapi.testclient import TestClient

from app import importer, seed
from app.config import settings
from app.db import get_pool, init_schema, tx
from app.main import app

SCHEMA = settings.search_schema
DATA = settings.data_dir


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_schema()
    yield
    with tx() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    get_pool().close()


@pytest.fixture()
def clean():
    with tx() as conn:
        row = conn.execute(
            "SELECT string_agg(table_name, ', ') AS names FROM information_schema.tables "
            "WHERE table_schema=%s", (SCHEMA,)).fetchone()
        if row and row["names"]:
            conn.execute(f"TRUNCATE TABLE {row['names']} RESTART IDENTITY CASCADE")
        conn.execute(
            "INSERT INTO users(username, display_name, role) VALUES "
            "('editor1', '王编辑', 'editor'), ('teacher1', '李老师', 'teacher'), "
            "('teacher2', '赵老师', 'teacher')")
        conn.commit()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def token_factory(client):
    def _make(username: str) -> str:
        resp = client.post("/api/auth/login", json={"username": username})
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    return _make


@pytest.fixture()
def teacher_token(token_factory):
    return token_factory("teacher1")


@pytest.fixture()
def editor_token(token_factory):
    return token_factory("editor1")


@pytest.fixture()
def auth_headers():
    def _make(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture()
def seeded(clean):
    """播种完整演示场景，返回各代次构建报告。"""
    r1 = importer.build_from_payload(os.path.join(DATA, "9787100000011", "payload.json"))
    with tx() as conn:
        seed._approve(conn, seed._edition_id(conn, r1["generation_id"]))
        conn.commit()
    r2 = importer.build_from_payload(
        os.path.join(DATA, "9787100000011-gen2", "payload.json"))
    with tx() as conn:
        seed._approve(conn, seed._edition_id(conn, r2["generation_id"]))
        conn.commit()
    r3 = importer.build_from_payload(os.path.join(DATA, "9787100000028", "payload.json"))
    with tx() as conn:
        seed._approve(conn, seed._edition_id(conn, r3["generation_id"]))
        conn.commit()
    r4 = importer.build_from_payload(
        os.path.join(DATA, "9787100000035", "payload.json"),
        chapter_codes=["W-CH13"])
    with tx() as conn:
        seed._synonym(conn, "数学", "因变量", "函数值", "approved")
        seed._synonym(conn, "数学", "取值范围", "定义域", "approved")
        seed._synonym(conn, "物理", "热运动", "无规则运动", "approved")
        seed._synonym(conn, "数学", "正比例", "正比关系", "pending")
        conn.commit()
    return {"gen1": r1, "gen2": r2, "gen3": r3, "broken": r4}
