"""Subtitle localization quality-control and delivery service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"

MERGE_FIELDS = ("cue_index", "start_ms", "end_ms", "text")
MERGE_NUMERIC_FIELDS = {"cue_index", "start_ms", "end_ms"}


def _three_way(base: dict[str, Any] | None, main: dict[str, Any] | None,
               offline: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[tuple[str, str]]]:
    """Field-level three-way merge. Returns (merged cue or None for deletion, conflicts).

    A conflict tuple is (field, reason); field == "*" means the cue itself is in
    conflict (offline edits a cue main deleted, or offline deletes a cue main edited).
    """
    issues: list[tuple[str, str]] = []
    if main is None:
        if offline is None:
            return None, issues
        if base is not None:
            # Offline edited a cue that has since been deleted on the main version.
            issues.append(("*", "main_deleted"))
        return dict(offline), issues
    if offline is None:
        if base is not None and any(main[field] != base[field] for field in MERGE_FIELDS):
            # Offline deletes a cue that main kept editing.
            issues.append(("*", "offline_delete_vs_main_edit"))
        return None, issues
    merged: dict[str, Any] = {}
    for field in MERGE_FIELDS:
        base_value = base[field] if base else None
        if offline[field] == base_value:
            merged[field] = main[field]
        elif main[field] == base_value:
            merged[field] = offline[field]
        elif offline[field] == main[field]:
            merged[field] = offline[field]
        else:
            # Both sides changed the same field differently: tentatively keep the
            # main value; both values are preserved on the checklist for the owner.
            issues.append((field, "both_changed"))
            merged[field] = main[field]
    return merged, issues


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    user TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
                    assigned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,user,role)
                );
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
                    user TEXT NOT NULL,
                    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS glossaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,source_term)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
                    supersedes_version_id INTEGER REFERENCES versions(id),
                    snapshot_hash TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    delivered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id),
                    batch_uid TEXT NOT NULL,
                    baseline_revision INTEGER NOT NULL CHECK(baseline_revision >= 0),
                    operations TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    revision_before INTEGER,
                    revision_after INTEGER,
                    snapshot_hash TEXT,
                    snapshot_manifest TEXT,
                    merged_at TEXT,
                    merged_by TEXT,
                    uploaded_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,batch_uid)
                );
                CREATE TABLE IF NOT EXISTS merge_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES offline_batches(id) ON DELETE CASCADE,
                    cue_key TEXT NOT NULL,
                    conflict_type TEXT NOT NULL CHECK(conflict_type IN ('field','glossary','timeline','identity')),
                    field TEXT,
                    reason TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolution TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id,cue_key,conflict_type,field)
                );
                """
            )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            source_term = str(payload.get("source_term", "")).strip()
            required = str(payload.get("required_translation", "")).strip()
            forbidden = payload.get("forbidden_terms", [])
            if not source_term or not required or not isinstance(forbidden, list):
                raise DomainError("术语、指定译法和禁用词格式不合法")
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), str(payload.get("notes", "")), utcnow()),
            )
            self._audit(conn, actor, "glossary.saved", "project", project_id, {"source_term": source_term})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required, "forbidden_terms": forbidden}

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            if parent_id is not None:
                parent = conn.execute("SELECT * FROM versions WHERE id=? AND project_id=?", (int(parent_id), project_id)).fetchone()
                if not parent or parent["language"] != language:
                    raise DomainError("父版本不存在或目标语言不一致", 409)
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?", (project_id, language)).fetchone()["value"])
            cur = conn.execute(
                "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, language, next_no, parent_id, actor, utcnow(), utcnow()),
            )
            self._audit(conn, actor, "version.created", "version", cur.lastrowid, {"language": language, "version_no": next_no})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.connect() as conn:
            version = conn.execute("SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)", (version_id, user, assignment_role, actor, utcnow()))
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')", (version["id"], actor)).fetchone())

    def _validate_glossary(self, conn: sqlite3.Connection, project_id: int, text: str) -> None:
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            forbidden = json.loads(row["forbidden_terms"])
            for term in forbidden:
                if term and term in text:
                    raise DomainError(f"字幕包含禁用译法: {term}")
            # The glossary is enforced only when the corresponding source term
            # appears in the localized cue. This keeps it useful without making
            # every cue repeat every glossary word.
            if row["source_term"] in text and row["required_translation"] not in text:
                raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")

    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            self._validate_glossary(conn, int(version["project_id"]), text)
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?", (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id, {"cue_id": saved_id, "revision": revision})
        return dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()) | {"version_revision": revision}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)", (version_id, cue_id, actor, time_ms, body, utcnow()))
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.submitted", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            assigned = conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'", (version_id, actor)).fetchone()
            if not assigned and actor != version["owner"]:
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)", (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.locked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有批准或锁定版本可以交付", 409)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            manifest = {"project_id": version["project_id"], "version_id": version_id, "language": version["language"], "version_no": version["version_no"], "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = conn.execute("SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1", (version["project_id"], version["language"], version_id)).fetchone()
            if previous:
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)", (utcnow(), previous["id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash, json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id, {"snapshot_hash": snapshot_hash})
        return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

    # ------------------------------------------------------------------
    # Offline batch merge
    # ------------------------------------------------------------------

    @staticmethod
    def _int_field(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainError(f"操作中的 {field} 必须是整数")
        return value

    def _cue_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"cue_id": row["id"], "cue_index": row["cue_index"], "start_ms": row["start_ms"],
                "end_ms": row["end_ms"], "text": row["text"]}

    def _glossary_problems(self, conn: sqlite3.Connection, project_id: int, text: str) -> list[str]:
        problems: list[str] = []
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            for term in json.loads(row["forbidden_terms"]):
                if term and term in text:
                    problems.append(f"禁用译法: {term}")
            if row["source_term"] in text and row["required_translation"] not in text:
                problems.append(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")
        return problems

    def _normalize_operations(self, conn: sqlite3.Connection, version: sqlite3.Row,
                              raw_ops: Any) -> list[dict[str, Any]]:
        """Validate and fold the offline op log into one effective op per cue."""
        if not isinstance(raw_ops, list) or not raw_ops:
            raise DomainError("操作日志必须是非空数组")
        folded: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for raw in raw_ops:
            if not isinstance(raw, dict):
                raise DomainError("每条操作必须是对象")
            op_type = raw.get("op")
            cue_key = raw.get("cue_id")
            if cue_key is not None:
                if isinstance(cue_key, bool) or not isinstance(cue_key, int):
                    raise DomainError("cue_id 必须是整数")
                cue_key = str(cue_key)
            else:
                client_key = str(raw.get("client_key", "")).strip()
                if not client_key:
                    raise DomainError("新增字幕必须提供 cue_id 或 client_key")
                cue_key = f"new:{client_key}"
            if op_type not in {"upsert", "delete"}:
                raise DomainError("操作类型只能是 upsert 或 delete")
            op: dict[str, Any] = {"op": op_type, "cue_key": cue_key}
            if op_type == "upsert":
                cue_index = self._int_field(raw.get("cue_index"), "cue_index")
                start_ms = self._int_field(raw.get("start_ms"), "start_ms")
                end_ms = self._int_field(raw.get("end_ms"), "end_ms")
                text = str(raw.get("text", "")).strip()
                if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                    raise DomainError("操作中的字幕时间、序号或内容不合法")
                if raw.get("cue_id") is None and cue_key in folded:
                    raise DomainError(f"同一新增字幕 {cue_key} 在日志中出现多次 upsert")
                op.update({"cue_index": cue_index, "start_ms": start_ms, "end_ms": end_ms, "text": text})
            base = raw.get("base")
            if base is not None:
                if not isinstance(base, dict):
                    raise DomainError("基线快照必须是对象")
                parsed_base: dict[str, Any] | None = {}
                for field in MERGE_FIELDS:
                    value = base.get(field)
                    if field in MERGE_NUMERIC_FIELDS:
                        value = self._int_field(value, f"base.{field}")
                    else:
                        value = str(value or "").strip()
                    parsed_base[field] = value
                op["base"] = parsed_base
            else:
                op["base"] = None
            if cue_key not in folded:
                order.append(cue_key)
            folded[cue_key] = op
        return [folded[key] for key in order]

    def _build_merge_plan(self, conn: sqlite3.Connection, version: sqlite3.Row,
                          operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Three-way merge every op against the current main version.

        Import only computes the plan and checklist; nothing is written to cues.
        """
        main_rows = {str(r["id"]): self._cue_view(r)
                     for r in conn.execute("SELECT * FROM cues WHERE version_id=?", (version["id"],))}
        entries: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []

        def add_item(cue_key: str, conflict_type: str, field: str | None, reason: str,
                     details: dict[str, Any]) -> None:
            for existing in items:
                if existing["cue_key"] == cue_key and existing["conflict_type"] == conflict_type and existing["field"] == field:
                    return
            items.append({"cue_key": cue_key, "conflict_type": conflict_type, "field": field,
                          "reason": reason, "details": details})

        for op in operations:
            key = op["cue_key"]
            offline = op if op["op"] == "upsert" else None
            main = main_rows.get(key)
            if op["base"] is None and not key.startswith("new:"):
                # An offline upsert/delete against an existing cue must carry the
                # baseline snapshot; otherwise the two sides cannot be merged.
                raise DomainError(f"操作 {key} 针对已有字幕，必须携带基线快照 base")
            merged, conflicts = _three_way(op["base"], main, offline)
            merged, conflicts = _three_way(op["base"], main, offline)
            for field, reason in conflicts:
                add_item(key, "identity" if field == "*" else "field",
                         None if field == "*" else field, reason,
                         {"base": op["base"], "main": None if main is None else {f: main[f] for f in MERGE_FIELDS},
                          "offline": None if offline is None else {f: offline[f] for f in MERGE_FIELDS}})
            entries.append({"cue_key": key, "op": op["op"], "base": op["base"],
                            "main": None if main is None else {f: main[f] for f in MERGE_FIELDS},
                            "offline": None if offline is None else {f: offline[f] for f in MERGE_FIELDS},
                            "merged": merged})

        # Structural checks on the tentative merged set.
        tentative: dict[str, dict[str, Any]] = {}
        for key, main in main_rows.items():
            tentative[key] = {f: main[f] for f in MERGE_FIELDS}
        for entry in entries:
            if entry["merged"] is not None:
                tentative[entry["cue_key"]] = entry["merged"]
            else:
                tentative.pop(entry["cue_key"], None)
        ordered = sorted(tentative.items(), key=lambda kv: kv[1]["start_ms"])
        for pos, (key, cue) in enumerate(ordered):
            for other_key, other in ordered[pos + 1:]:
                if other["start_ms"] >= cue["end_ms"]:
                    break
                if other["start_ms"] < cue["end_ms"] and other["end_ms"] > cue["start_ms"]:
                    add_item(key, "timeline", None, "overlap",
                             {"other_key": other_key, "interval": [cue["start_ms"], cue["end_ms"]],
                              "other_interval": [other["start_ms"], other["end_ms"]]})
                    add_item(other_key, "timeline", None, "overlap",
                             {"other_key": key, "interval": [other["start_ms"], other["end_ms"]],
                              "other_interval": [cue["start_ms"], cue["end_ms"]]})
            index_owner = next((ok for ok, oc in tentative.items()
                                if ok != key and oc["cue_index"] == cue["cue_index"]), None)
            if index_owner is not None:
                add_item(key, "timeline", None, "index_conflict",
                         {"cue_index": cue["cue_index"], "other_key": index_owner})
            problems = self._glossary_problems(conn, int(version["project_id"]), cue["text"])
            if problems:
                add_item(key, "glossary", None, "violations", {"problems": problems, "text": cue["text"]})
        return {"entries": entries, "items": items,
                "conflict_count": len(items),
                "generated_at": utcnow()}

    def upload_merge(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> tuple[dict[str, Any], bool]:
        batch_uid = str(payload.get("batch_uid", "")).strip()
        if not batch_uid:
            raise DomainError("批次必须提供 batch_uid")
        try:
            baseline_revision = int(payload.get("baseline_revision"))
        except (TypeError, ValueError) as exc:
            raise DomainError("baseline_revision 必须是整数") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if baseline_revision < 0 or baseline_revision > int(version["revision"]):
                raise DomainError("基线修订号不存在（高于主版本当前修订号）", 409)
            duplicate = conn.execute("SELECT * FROM offline_batches WHERE version_id=? AND batch_uid=?", (version_id, batch_uid)).fetchone()
            if duplicate:
                return self._merge_payload(conn, duplicate), False
            operations = self._normalize_operations(conn, version, payload.get("operations"))
            plan = self._build_merge_plan(conn, version, operations)
            cur = conn.execute(
                "INSERT INTO offline_batches(version_id,batch_uid,baseline_revision,operations,plan,uploaded_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (version_id, batch_uid, baseline_revision,
                 json.dumps(operations, ensure_ascii=False, sort_keys=True),
                 json.dumps(plan, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            batch_id = int(cur.lastrowid)
            for item in plan["items"]:
                conn.execute(
                    "INSERT INTO merge_items(batch_id,cue_key,conflict_type,field,reason,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["cue_key"], item["conflict_type"], item["field"], item["reason"],
                     json.dumps(item["details"], ensure_ascii=False, sort_keys=True), utcnow()),
                )
            self._audit(conn, actor, "merge.uploaded", "batch", batch_id,
                        {"version_id": version_id, "batch_uid": batch_uid,
                         "baseline_revision": baseline_revision, "conflicts": len(plan["items"])})
            row = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            return self._merge_payload(conn, row), True

    def _merge_payload(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        items = [dict(r) for r in conn.execute("SELECT * FROM merge_items WHERE batch_id=? ORDER BY id", (row["id"],))]
        for item in items:
            item["details"] = json.loads(item["details"])
            if item["resolution"]:
                item["resolution"] = json.loads(item["resolution"])
        batch = dict(row)
        batch["plan"] = json.loads(row["plan"])
        batch["operations"] = json.loads(row["operations"])
        if row["snapshot_manifest"]:
            batch["snapshot_manifest"] = json.loads(row["snapshot_manifest"])
        batch["items"] = items
        batch["pending_count"] = sum(1 for i in items if i["status"] == "pending")
        return batch

    def get_merge(self, batch_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise DomainError("离线批次不存在", 404)
            return self._merge_payload(conn, row)

    def list_merges(self, version_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if version_id is None:
                rows = conn.execute("SELECT * FROM offline_batches ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM offline_batches WHERE version_id=? ORDER BY id DESC", (version_id,)).fetchall()
            result = []
            for row in rows:
                pending = conn.execute("SELECT COUNT(*) c FROM merge_items WHERE batch_id=? AND status='pending'", (row["id"],)).fetchone()["c"]
                result.append({"id": row["id"], "version_id": row["version_id"], "batch_uid": row["batch_uid"],
                               "baseline_revision": row["baseline_revision"], "status": row["status"],
                               "pending_count": pending, "revision_after": row["revision_after"],
                               "snapshot_hash": row["snapshot_hash"], "uploaded_by": row["uploaded_by"],
                               "created_at": row["created_at"], "merged_at": row["merged_at"]})
            return result

    def resolve_merge_item(self, item_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM merge_items WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise DomainError("待处理项不存在", 404)
            batch = conn.execute("SELECT * FROM offline_batches WHERE id=?", (item["batch_id"],)).fetchone()
            version = self._version(conn, int(batch["version_id"]))
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以裁决合并清单", 403)
            if batch["status"] != "pending":
                raise DomainError("该批次已经入库，不能再修改裁决", 409)
            decision = str(payload.get("decision", "")).strip()
            if decision not in {"main", "offline", "custom"}:
                raise DomainError("裁决必须是 main、offline 或 custom")
            resolution: dict[str, Any] = {"decision": decision}
            if decision == "custom":
                if item["conflict_type"] == "glossary":
                    custom_text = str(payload.get("text", "")).strip()
                    if not custom_text:
                        raise DomainError("自定义裁决必须提供 text")
                    if self._glossary_problems(conn, int(version["project_id"]), custom_text):
                        raise DomainError("自定义文本仍违反术语表", 409)
                    resolution["text"] = custom_text
                else:
                    values: dict[str, Any] = {}
                    for field in MERGE_FIELDS:
                        if field in payload:
                            value = payload[field]
                            if field in MERGE_NUMERIC_FIELDS:
                                if isinstance(value, bool) or not isinstance(value, int):
                                    raise DomainError(f"{field} 必须是整数")
                            else:
                                value = str(value or "").strip()
                            values[field] = value
                    if not values:
                        raise DomainError("自定义裁决必须提供字段值")
                    if "cue_index" in values and values["cue_index"] < 0:
                        raise DomainError("序号不合法")
                    if "start_ms" in values and values["start_ms"] < 0:
                        raise DomainError("起点不合法")
                    resolution["fields"] = values
            conn.execute("UPDATE merge_items SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE id=?",
                         (json.dumps(resolution, ensure_ascii=False, sort_keys=True), actor, utcnow(), item_id))
            self._audit(conn, actor, "merge.resolved", "merge_item", item_id,
                        {"batch_id": item["batch_id"], "decision": decision})
            return dict(conn.execute("SELECT * FROM merge_items WHERE id=?", (item_id,)).fetchone())

    def confirm_merge(self, batch_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise DomainError("离线批次不存在", 404)
            version = self._version(conn, int(batch["version_id"]))
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以确认入库", 403)
            if batch["status"] != "pending":
                raise DomainError("该批次已经入库，重复确认不会再次结算", 409)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以合并离线批次", 409)

            # Recompute the three-way merge against the freshest main version;
            # resolutions recorded against the uploaded plan are replayed.
            operations = json.loads(batch["operations"])
            plan = self._build_merge_plan(conn, version, operations)
            resolutions: dict[tuple[str, str, str | None], sqlite3.Row] = {}
            for r in conn.execute("SELECT * FROM merge_items WHERE batch_id=? AND status='resolved'", (batch_id,)):
                resolutions[(r["cue_key"], r["conflict_type"], r["field"])] = r

            entries_by_key = {e["cue_key"]: e for e in plan["entries"]}
            main_rows = {str(r["id"]): self._cue_view(r)
                         for r in conn.execute("SELECT * FROM cues WHERE version_id=?", (version["id"],))}
            surviving: dict[str, dict[str, Any]] = {
                key: {f: main[f] for f in MERGE_FIELDS} for key, main in main_rows.items()
            }

            def resolve_or_block(cue_key: str, conflict_type: str, field: str | None) -> sqlite3.Row:
                res = resolutions.get((cue_key, conflict_type, field))
                if res is None:
                    raise DomainError("仍有未裁决的待处理项，不能入库", 409)
                return res

            # First apply the automatic three-way result for every entry; entries
            # with field/identity conflicts are then overwritten by owner decisions.
            conflict_keys = {(i["cue_key"], i["conflict_type"]) for i in plan["items"]
                             if i["conflict_type"] in {"field", "identity"}}
            for entry in plan["entries"]:
                key = entry["cue_key"]
                if (key, "field") in conflict_keys or (key, "identity") in conflict_keys:
                    continue
                if entry["merged"] is None:
                    surviving.pop(key, None)
                else:
                    surviving[key] = entry["merged"]

            # Field / identity conflicts determine each cue's final record.
            pending_field_items = [i for i in plan["items"]
                                   if i["cue_key"] in entries_by_key and i["conflict_type"] in {"field", "identity"}]
            for item in pending_field_items:
                key, issue_field = item["cue_key"], item["field"]
                entry = entries_by_key[key]
                chosen = surviving.get(key) if key in surviving else None
                res = resolve_or_block(key, item["conflict_type"], issue_field)
                decision = json.loads(res["resolution"])
                if issue_field is None:
                    if decision["decision"] == "main":
                        chosen = entry["main"]
                    elif decision["decision"] == "offline":
                        chosen = entry["offline"]
                    else:
                        base = entry["merged"] or entry["offline"] or entry["main"] or {}
                        chosen = {**base, **decision["fields"]}
                else:
                    chosen = dict(chosen or entry["merged"] or {})
                    if decision["decision"] == "main":
                        chosen[issue_field] = entry["main"][issue_field]
                    elif decision["decision"] == "offline":
                        chosen[issue_field] = entry["offline"][issue_field]
                    else:
                        if issue_field not in decision["fields"]:
                            raise DomainError(f"自定义裁决缺少字段 {issue_field}", 409)
                        chosen[issue_field] = decision["fields"][issue_field]
                if chosen is None:
                    surviving.pop(key, None)
                else:
                    surviving[key] = chosen

            # Timeline conflicts: resolution pins the recorded interval/index.
            for item in plan["items"]:
                if item["cue_key"] not in surviving or item["conflict_type"] != "timeline":
                    continue
                key = item["cue_key"]
                res = resolve_or_block(key, "timeline", None)
                decision = json.loads(res["resolution"])
                if decision["decision"] == "offline" and key in entries_by_key and entries_by_key[key]["offline"]:
                    surviving[key] = entries_by_key[key]["offline"]
                elif decision["decision"] == "custom":
                    surviving[key] = {**surviving[key], **decision["fields"]}
                # "main" keeps the current surviving record; a main-only cue has
                # no offline record, so main/offline both leave it untouched.

            # Glossary conflicts: only an explicit resolution can override the
            # project glossary; custom text must already have been validated.
            for item in plan["items"]:
                if item["conflict_type"] != "glossary":
                    continue
                res = resolve_or_block(item["cue_key"], "glossary", None)
                decision = json.loads(res["resolution"])
                if decision["decision"] == "custom":
                    surviving[item["cue_key"]]["text"] = decision["text"]
                elif decision["decision"] == "main":
                    entry = entries_by_key.get(item["cue_key"])
                    surviving[item["cue_key"]]["text"] = entry["main"]["text"] if entry and entry["main"] else surviving[item["cue_key"]]["text"]
                else:
                    surviving[item["cue_key"]]["text"] = entries_by_key[item["cue_key"]]["offline"]["text"]

            # Re-scan the whole merged set: even after an explicit main/offline
            # decision the final text must satisfy the hard glossary rules.
            for key, cue in surviving.items():
                problems = self._glossary_problems(conn, int(version["project_id"]), cue["text"])
                if problems:
                    raise DomainError(f"字幕 {key} 裁决后仍违反术语表: {'; '.join(problems)}", 409)

            # Final validation: owner decisions still have to form a legal timeline.
            for key, cue in surviving.items():
                if cue["start_ms"] < 0 or cue["end_ms"] <= cue["start_ms"] or cue["end_ms"] > int(version["duration_ms"]) or not cue["text"]:
                    raise DomainError(f"裁决后的字幕 {key} 时间或内容不合法", 409)
            ordered = sorted(surviving.items(), key=lambda kv: kv[1]["start_ms"])
            for pos, (key, cue) in enumerate(ordered):
                for other_key, other in ordered[pos + 1:]:
                    if other["start_ms"] >= cue["end_ms"]:
                        break
                    raise DomainError(f"裁决后字幕 {key} 与 {other_key} 仍有时间轴交叉", 409)
                if any(ok != key and oc["cue_index"] == cue["cue_index"] for ok, oc in surviving.items()):
                    raise DomainError(f"裁决后字幕 {key} 仍有重复序号 {cue['cue_index']}", 409)

            revision_before = int(version["revision"])
            changed = 0
            comments_to_reanchor: list[tuple[int, int]] = []
            for entry in plan["entries"]:
                key = entry["cue_key"]
                is_new_key = key.startswith("new:")
                existing = main_rows.get(key)
                final = surviving.get(key)
                if existing is None and (is_new_key or final is not None):
                    # Brand-new offline cue, or an offline edit to a cue main had
                    # deleted and the owner chose to keep the offline version.
                    if final is None:
                        continue
                    cue = final
                    conn.execute(
                        "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (version["id"], cue["cue_index"], cue["start_ms"], cue["end_ms"], cue["text"], batch["uploaded_by"], utcnow()),
                    )
                    new_id = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
                    main_rows[key] = {"cue_id": new_id, **cue}
                    changed += 1
                    continue
                if existing and final is None:
                    # comments.cue_id is ON DELETE SET NULL, so collect anchored
                    # comments before the cue row disappears.
                    for comment in conn.execute("SELECT id,time_ms FROM comments WHERE cue_id=?", (existing["cue_id"],)).fetchall():
                        comments_to_reanchor.append((int(comment["id"]), int(comment["time_ms"])))
                    conn.execute("DELETE FROM cues WHERE id=? AND version_id=?", (existing["cue_id"], version["id"]))
                    changed += 1
                elif existing and final is not None:
                    if any(existing[f] != final[f] for f in MERGE_FIELDS):
                        conn.execute(
                            "UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?",
                            (final["cue_index"], final["start_ms"], final["end_ms"], final["text"], batch["uploaded_by"], utcnow(), existing["cue_id"]),
                        )
                        changed += 1

            # Re-anchor comments after deletions landed: fall back to the cue
            # covering the comment time point, otherwise keep it time-based.
            for comment_id, time_ms in comments_to_reanchor:
                cover = conn.execute(
                    "SELECT id FROM cues WHERE version_id=? AND start_ms<=? AND end_ms>? ORDER BY start_ms LIMIT 1",
                    (version["id"], time_ms, time_ms),
                ).fetchone()
                conn.execute("UPDATE comments SET cue_id=? WHERE id=?", (cover["id"] if cover else None, comment_id))

            revision_after = revision_before + changed
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version["id"],))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            manifest = {"project_id": version["project_id"], "version_id": version["id"], "language": version["language"],
                        "version_no": version["version_no"], "revision": revision_after,
                        "merged_batch_uid": batch["batch_uid"], "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            now = utcnow()
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision_after, now, version["id"]))
            conn.execute(
                "UPDATE offline_batches SET status='merged',revision_before=?,revision_after=?,snapshot_hash=?,snapshot_manifest=?,merged_at=?,merged_by=? WHERE id=?",
                (revision_before, revision_after, snapshot_hash,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True), now, actor, batch_id),
            )
            self._audit(conn, actor, "merge.confirmed", "batch", batch_id,
                        {"version_id": version["id"], "changed": changed,
                         "revision_before": revision_before, "revision_after": revision_after,
                         "snapshot_hash": snapshot_hash})
            row = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            return self._merge_payload(conn, row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_projects():
        return {"project": int(db.list_projects()[0]["id"])}
    project = db.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4", "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    db.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    version = db.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "SubtitleQC/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.db.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.db.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.db.list_deliveries()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.db.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.db.list_comments(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "merges":
                return self._send({"batches": self.db.list_merges(int(parts[2]))})
            if len(parts) == 3 and parts[:2] == ["api", "merges"]:
                return self._send(self.db.get_merge(int(parts[2])))
            if parts == ["api", "merges"]:
                return self._send({"batches": self.db.list_merges()})
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "projects"]:
                return self._send(self.db.create_project(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return self._send(self.db.create_version(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.db.set_glossary(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                return self._send(self.db.assign(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send(self.db.save_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.db.add_comment(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "lock", "deliver"}:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    return self._send(self.db.submit(version_id, actor, role))
                if parts[3] == "lock":
                    return self._send(self.db.lock(version_id, actor, role))
                return self._send(self.db.deliver(version_id, actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                return self._send(self.db.review(int(parts[2]), actor, body, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "merges":
                batch, created = self.db.upload_merge(int(parts[2]), actor, body, role)
                return self._send(batch, 201 if created else 200)
            if len(parts) == 4 and parts[:2] == ["api", "merges"] and parts[3] == "confirm":
                return self._send(self.db.confirm_merge(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "merge-items"] and parts[3] == "resolve":
                return self._send(self.db.resolve_merge_item(int(parts[2]), actor, body, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
