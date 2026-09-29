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
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    baseline_revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    merge_result TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    uploaded_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    UNIQUE(version_id,content_hash)
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
    # Offline batch merge (three-way: baseline x offline log x main)
    # ------------------------------------------------------------------

    @staticmethod
    def _batch_hash(version_id: int, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"version_id": version_id, "baseline_revision": payload.get("baseline_revision"),
             "operations": payload.get("operations"), "baseline_cues": payload.get("baseline_cues")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _check_batch_access(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str,
                            role: str, require_owner: bool = False) -> None:
        if actor != version["owner"] and role != "admin":
            if require_owner:
                raise DomainError("只有项目负责人可以确认离线批次", 403)
            if not conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version["id"], actor)).fetchone():
                raise DomainError("只有项目成员可以查看离线批次", 403)

    def _check_upload_batch(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str, role: str) -> None:
        if actor == version["owner"] or role == "admin":
            return
        if conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')",
                        (version["id"], actor)).fetchone():
            return
        raise DomainError("没有该版本的离线回网权限", 403)

    @staticmethod
    def _cue_fields_from_op(op: dict[str, Any], ci: int, require_full: bool) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        fields["cue_index"] = int(op["cue_index"]) if "cue_index" in op else ci
        for key in ("start_ms", "end_ms"):
            if key in op:
                fields[key] = int(op[key])
            elif require_full:
                raise DomainError(f"新增字幕缺少 {key}")
        if "text" in op:
            fields["text"] = str(op["text"]).strip()
        elif require_full:
            raise DomainError("新增字幕缺少 text")
        if require_full and (fields["start_ms"] < 0 or fields["end_ms"] <= fields["start_ms"] or not fields["text"]):
            raise DomainError("新增字幕时间或内容不合法")
        return fields

    @staticmethod
    def _same_cue(row: sqlite3.Row, cue: dict[str, Any]) -> bool:
        return (int(row["cue_index"]) == int(cue["cue_index"]) and int(row["start_ms"]) == int(cue["start_ms"])
                and int(row["end_ms"]) == int(cue["end_ms"]) and row["text"] == cue["text"])

    def _compute_merge(self, conn: sqlite3.Connection, version: sqlite3.Row,
                       payload: dict[str, Any]) -> tuple[dict[Any, Any], list[dict[str, Any]], dict[str, list[int]]]:
        """Three-way merge. Returns (merged keyed by identity, conflicts, actions).

        Identity is ("b", cue_index) for baseline-derived cues and
        ("n", cue_index) for cues added offline. A merged value of None means
        the cue is deleted in the merge result.
        """
        version_id = int(version["id"])
        try:
            baseline_revision = int(payload.get("baseline_revision"))
        except (TypeError, ValueError) as exc:
            raise DomainError("基线版本必须是整数修订号") from exc
        operations = payload.get("operations")
        if not isinstance(operations, list):
            raise DomainError("操作日志必须是列表")
        raw_baseline = payload.get("baseline_cues")

        if isinstance(raw_baseline, list):
            base: dict[int, dict[str, Any]] = {}
            for c in raw_baseline:
                ci = int(c["cue_index"])
                base[ci] = {"cue_index": ci, "start_ms": int(c["start_ms"]),
                            "end_ms": int(c["end_ms"]), "text": str(c["text"])}
        elif baseline_revision == int(version["revision"]):
            base = {r["cue_index"]: {"cue_index": r["cue_index"], "start_ms": r["start_ms"],
                                     "end_ms": r["end_ms"], "text": r["text"]}
                    for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=?", (version_id,))}
        else:
            base = {}

        off: dict[Any, Any] = {}
        for op in operations:
            if not isinstance(op, dict):
                raise DomainError("操作日志条目不合法")
            kind = str(op.get("op", "")).strip()
            try:
                ci = int(op.get("ref", op.get("cue_index")))
            except (TypeError, ValueError) as exc:
                raise DomainError("操作缺少合法 ref") from exc
            if kind == "delete":
                off[("b", ci)] = None
            elif kind == "add":
                off[("n", ci)] = self._cue_fields_from_op(op, ci, require_full=True)
            elif kind == "update":
                fields = self._cue_fields_from_op(op, ci, require_full=False)
                prior = base.get(ci)
                off[("b", ci)] = {**prior, **fields} if prior is not None else fields
            else:
                raise DomainError(f"不支持的离线操作类型: {kind}")

        main_rows = conn.execute("SELECT * FROM cues WHERE version_id=?", (version_id,)).fetchall()
        main_by_idx = {r["cue_index"]: r for r in main_rows}

        merged: dict[Any, Any] = {}
        conflicts: list[dict[str, Any]] = []

        def field_merge(bv: Any, ov: Any, mv: Any) -> tuple[Any, bool]:
            if ov == mv:
                return ov, False
            if ov == bv:
                return mv, False
            if mv == bv:
                return ov, False
            return mv, True

        for key in sorted({("b", i) for i in base} | set(off), key=lambda k: (k[0], k[1])):
            kind, i = key
            b = base.get(i)
            o = off.get(key, "MISSING")
            m = main_by_idx.get(i)

            if kind == "n":
                if m is None:
                    merged[key] = o
                else:
                    conflicts.append({"id": f"a-{i}", "kind": "add", "cue_index": i,
                                      "baseline_value": None, "offline_value": o, "main_value": dict(m),
                                      "merged_value": dict(m), "choices": ["main", "offline"],
                                      "message": f"字幕 {i} 两侧都新增了同一序号"})
                    merged[key] = {k: m[k] for k in ("cue_index", "start_ms", "end_ms", "text")}
                continue

            if o is None:
                if m is None:
                    merged[key] = None
                elif b is not None and self._same_cue(m, b):
                    merged[key] = None
                else:
                    conflicts.append({"id": f"d-{i}", "kind": "delete", "cue_index": i,
                                      "baseline_value": b, "offline_value": None, "main_value": dict(m),
                                      "merged_value": dict(m), "choices": ["main", "offline"],
                                      "message": f"字幕 {i} 一侧删除一侧修改"})
                    merged[key] = {k: m[k] for k in ("cue_index", "start_ms", "end_ms", "text")}
                continue

            if o == "MISSING":
                merged[key] = None if m is None else {k: m[k] for k in ("cue_index", "start_ms", "end_ms", "text")}
                continue

            if m is None:
                if b is None:
                    merged[key] = o
                else:
                    conflicts.append({"id": f"d-{i}", "kind": "delete", "cue_index": i,
                                      "baseline_value": b, "offline_value": o, "main_value": None,
                                      "merged_value": None, "choices": ["main", "offline"],
                                      "message": f"字幕 {i} 一侧删除一侧修改"})
                    merged[key] = None
                continue

            if b is None:
                if self._same_cue(m, o):
                    merged[key] = {k: m[k] for k in ("cue_index", "start_ms", "end_ms", "text")}
                else:
                    conflicts.append({"id": f"a-{i}", "kind": "add", "cue_index": i,
                                      "baseline_value": None, "offline_value": o, "main_value": dict(m),
                                      "merged_value": dict(m), "choices": ["main", "offline"],
                                      "message": f"字幕 {i} 两侧都新增了同一序号"})
                    merged[key] = {k: m[k] for k in ("cue_index", "start_ms", "end_ms", "text")}
                continue

            final: dict[str, Any] = {}
            for f in ("cue_index", "start_ms", "end_ms", "text"):
                value, bad = field_merge(b[f], o[f], m[f])
                final[f] = value
                if bad:
                    conflicts.append({"id": f"f-{i}-{f}", "kind": "field", "cue_index": i, "field": f,
                                      "baseline_value": b[f], "offline_value": o[f], "main_value": m[f],
                                      "merged_value": value, "choices": ["main", "offline"],
                                      "message": f"字幕 {i} 的 {f} 字段两侧修改不一致"})
            merged[key] = final

        actions: dict[str, list[int]] = {"added": [], "updated": [], "deleted": []}
        for (kind, i), cue in merged.items():
            if cue is None:
                continue
            if kind == "b":
                actions["updated" if i in main_by_idx else "added"].append(i)
            else:
                actions["updated" if i in main_by_idx else "added"].append(i)
        for i in base:
            if merged.get(("b", i)) is None and i in main_by_idx:
                actions["deleted"].append(i)
        return merged, conflicts, actions

    def _validate_merged(self, conn: sqlite3.Connection, version: sqlite3.Row,
                         cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for g in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (int(version["project_id"]),)):
            forbidden = json.loads(g["forbidden_terms"])
            for cue in cues:
                text = str(cue["text"])
                for term in forbidden:
                    if term and term in text:
                        items.append({"id": f"g-{cue['cue_index']}-{term}", "kind": "glossary",
                                      "cue_index": cue["cue_index"], "term": term, "choices": ["accept"],
                                      "message": f"字幕 {cue['cue_index']} 包含禁用译法: {term}"})
                if g["source_term"] in text and g["required_translation"] not in text:
                    items.append({"id": f"g-{cue['cue_index']}-{g['source_term']}", "kind": "glossary",
                                  "cue_index": cue["cue_index"], "term": g["source_term"], "choices": ["accept"],
                                  "message": f"字幕 {cue['cue_index']} 术语 {g['source_term']} 必须使用指定译法 {g['required_translation']}"})
        ordered = sorted(cues, key=lambda c: int(c["start_ms"]))
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if int(a["start_ms"]) < int(b["end_ms"]) and int(b["start_ms"]) < int(a["end_ms"]):
                    items.append({"id": f"t-{a['cue_index']}-{b['cue_index']}", "kind": "timeline",
                                  "cue_index": a["cue_index"], "cue_index_b": b["cue_index"], "choices": ["accept"],
                                  "message": f"字幕 {a['cue_index']} 与 {b['cue_index']} 时间轴交叉"})
        return items

    def _resolve_merged(self, merged: dict[Any, Any], conflicts: list[dict[str, Any]],
                        resolutions: dict[str, str]) -> dict[Any, Any]:
        final = {k: (dict(v) if v is not None else None) for k, v in merged.items()}
        for c in conflicts:
            choice = resolutions.get(c["id"])
            if c["kind"] == "field" and choice == "offline":
                key = ("b", c["cue_index"])
                if final.get(key) is not None:
                    final[key][c["field"]] = c["offline_value"]
            elif c["kind"] == "delete" and choice == "offline":
                key = ("b", c["cue_index"])
                final[key] = dict(c["offline_value"]) if c["offline_value"] is not None else None
            elif c["kind"] == "add" and choice == "offline":
                key = ("n", c["cue_index"])
                final[key] = dict(c["offline_value"])
        return final

    def _apply_merge(self, conn: sqlite3.Connection, version: sqlite3.Row, final: dict[Any, Any]) -> int:
        version_id = int(version["id"])
        base_idx = {i for (k, i) in final if k == "b"}
        main_rows = conn.execute("SELECT * FROM cues WHERE version_id=?", (version_id,)).fetchall()
        main_by_idx = {r["cue_index"]: r for r in main_rows}

        before_id_to_ident: dict[int, tuple[str, int]] = {}
        for r in main_rows:
            before_id_to_ident[r["id"]] = ("b", r["cue_index"]) if r["cue_index"] in base_idx else ("m", r["cue_index"])

        after_ident_to_id: dict[tuple[str, int], int] = {}
        for (kind, i), cue in final.items():
            if cue is None:
                continue
            ci = int(cue["cue_index"])
            if kind in ("b", "n") and i in main_by_idx:
                row = main_by_idx[i]
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?",
                             (ci, int(cue["start_ms"]), int(cue["end_ms"]), str(cue["text"]), "offline-batch", utcnow(), row["id"]))
                after_ident_to_id[(kind, i)] = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, ci, int(cue["start_ms"]), int(cue["end_ms"]), str(cue["text"]), "offline-batch", utcnow()))
                after_ident_to_id[(kind, i)] = cur.lastrowid

        for r in main_rows:
            ident = ("b", r["cue_index"]) if r["cue_index"] in base_idx else ("m", r["cue_index"])
            if ident in after_ident_to_id:
                continue
            if ident[0] == "m" or final.get(ident) is not None:
                after_ident_to_id[ident] = r["id"]

        for (kind, i), cue in final.items():
            if kind == "b" and cue is None and i in main_by_idx:
                conn.execute("DELETE FROM cues WHERE id=?", (main_by_idx[i]["id"],))

        for c in conn.execute("SELECT * FROM comments WHERE version_id=?", (version_id,)).fetchall():
            if c["cue_id"] is None:
                continue
            ident = before_id_to_ident.get(c["cue_id"])
            new_id = after_ident_to_id.get(ident) if ident is not None else None
            if new_id != c["cue_id"]:
                conn.execute("UPDATE comments SET cue_id=? WHERE id=?", (new_id, c["id"]))

        revision = int(version["revision"]) + 1
        conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))

        delivery = conn.execute("SELECT * FROM deliveries WHERE version_id=?", (version_id,)).fetchone()
        if delivery:
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (int(version["project_id"]),))]
            manifest = {"project_id": int(version["project_id"]), "version_id": version_id, "language": version["language"],
                        "version_no": version["version_no"], "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            conn.execute("UPDATE deliveries SET snapshot_hash=?,manifest=? WHERE id=?",
                         (snapshot_hash, json.dumps(manifest, ensure_ascii=False, sort_keys=True), delivery["id"]))
        return revision

    def upload_batch(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DomainError("批次内容不合法")
        try:
            int(payload.get("baseline_revision"))
        except (TypeError, ValueError) as exc:
            raise DomainError("基线版本必须是整数修订号") from exc
        if not isinstance(payload.get("operations"), list):
            raise DomainError("操作日志必须是列表")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            self._check_upload_batch(conn, version, actor, role)
            content_hash = self._batch_hash(version_id, payload)
            existing = conn.execute("SELECT * FROM offline_batches WHERE version_id=? AND content_hash=?",
                                    (version_id, content_hash)).fetchone()
            if existing and existing["status"] in ("confirmed", "pending"):
                return dict(existing)
            if existing:
                batch_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO offline_batches(version_id,baseline_revision,status,content_hash,payload,merge_result,uploaded_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (version_id, int(payload["baseline_revision"]), "pending", content_hash,
                     json.dumps(payload, ensure_ascii=False), "{}", actor, utcnow()))
                batch_id = cur.lastrowid
            try:
                merged, conflicts, actions = self._compute_merge(conn, version, payload)
                final_cues = [v for v in merged.values() if v is not None]
                val_items = self._validate_merged(conn, version, final_cues)
                result = {"merged": final_cues, "conflicts": conflicts + val_items, "actions": actions}
            except DomainError as exc:
                conn.execute("UPDATE offline_batches SET status='failed',error=? WHERE id=?", (str(exc), batch_id))
                raise
            except Exception as exc:
                conn.execute("UPDATE offline_batches SET status='failed',error=? WHERE id=?", (str(exc), batch_id))
                raise DomainError(f"批次导入失败，可重试: {exc}", 409) from exc
            conn.execute("UPDATE offline_batches SET merge_result=?,error='',status='pending' WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), batch_id))
            self._audit(conn, actor, "batch.uploaded", "version", version_id,
                        {"batch_id": batch_id, "conflicts": len(result["conflicts"])})
        return dict(conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone())

    def list_batches(self, version_id: int, actor: str, role: str = "viewer") -> list[dict[str, Any]]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            self._check_batch_access(conn, version, actor, role)
            return [dict(r) for r in conn.execute("SELECT * FROM offline_batches WHERE version_id=? ORDER BY id DESC", (version_id,))]

    def get_batch(self, batch_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise DomainError("离线批次不存在", 404)
            version = self._version(conn, batch["version_id"])
            self._check_batch_access(conn, version, actor, role)
            return dict(batch)

    def retry_batch(self, batch_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise DomainError("离线批次不存在", 404)
            version = self._version(conn, batch["version_id"])
            self._check_upload_batch(conn, version, actor, role)
            if batch["status"] != "failed":
                raise DomainError(f"批次状态为 {batch['status']}，无需重试", 409)
            payload = json.loads(batch["payload"])
            try:
                merged, conflicts, actions = self._compute_merge(conn, version, payload)
                final_cues = [v for v in merged.values() if v is not None]
                val_items = self._validate_merged(conn, version, final_cues)
                result = {"merged": final_cues, "conflicts": conflicts + val_items, "actions": actions}
            except DomainError:
                raise
            except Exception as exc:
                conn.execute("UPDATE offline_batches SET error=? WHERE id=?", (str(exc), batch_id))
                raise DomainError(f"批次导入失败，可重试: {exc}", 409) from exc
            conn.execute("UPDATE offline_batches SET status='pending',merge_result=?,error='',uploaded_by=?,created_at=? WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), actor, utcnow(), batch_id))
            self._audit(conn, actor, "batch.retried", "version", version["id"], {"batch_id": batch_id})
        return dict(conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone())

    def confirm_batch(self, batch_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        resolutions: dict[str, str] = {}
        if isinstance(payload, dict):
            raw = payload.get("resolutions")
            if isinstance(raw, dict):
                resolutions = {str(k): str(v) for k, v in raw.items()}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise DomainError("离线批次不存在", 404)
            version = self._version(conn, batch["version_id"])
            self._check_batch_access(conn, version, actor, role, require_owner=True)
            if batch["status"] == "confirmed":
                return dict(batch)
            if batch["status"] != "pending":
                raise DomainError(f"批次状态为 {batch['status']}，不能确认；失败批次请先重试", 409)
            body = json.loads(batch["payload"])
            merged, conflicts, actions = self._compute_merge(conn, version, body)
            final_cues = [v for v in merged.values() if v is not None]
            val_items = self._validate_merged(conn, version, final_cues)
            all_conflicts = conflicts + val_items
            final = self._resolve_merged(merged, all_conflicts, resolutions)
            revision = self._apply_merge(conn, version, final)
            result = {"merged": [v for v in final.values() if v is not None], "conflicts": all_conflicts, "actions": actions}
            conn.execute("UPDATE offline_batches SET status='confirmed',confirmed_by=?,confirmed_at=?,merge_result=? WHERE id=?",
                         (actor, utcnow(), json.dumps(result, ensure_ascii=False), batch_id))
            self._audit(conn, actor, "batch.confirmed", "version", version["id"],
                        {"batch_id": batch_id, "revision": revision, "conflicts": len(all_conflicts)})
        return dict(conn.execute("SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone())

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
            actor, role = self._auth()
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
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "offline-batches":
                return self._send({"batches": self.db.list_batches(int(parts[2]), actor, role)})
            if len(parts) == 3 and parts[:2] == ["api", "offline-batches"]:
                return self._send(self.db.get_batch(int(parts[2]), actor, role))
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
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "offline-batches":
                return self._send(self.db.upload_batch(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "offline-batches"] and parts[3] in {"confirm", "retry"}:
                if parts[3] == "confirm":
                    return self._send(self.db.confirm_batch(int(parts[2]), actor, body, role))
                return self._send(self.db.retry_batch(int(parts[2]), actor, role))
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
