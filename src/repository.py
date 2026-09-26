from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .advisory import ADVISORY_STATES, KINDS, LEVELS, READING_STATES
from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        kinds = ",".join("'" + k + "'" for k in KINDS)
        levels = ",".join("'" + level + "'" for level in LEVELS)
        reading_states = ",".join("'" + s + "'" for s in READING_STATES)
        advisory_states = ",".join("'" + s + "'" for s in ADVISORY_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ({kinds})),
                    payload TEXT NOT NULL,
                    risk_level TEXT NOT NULL CHECK(risk_level IN ({levels})),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ({reading_states})),
                    defect_status TEXT CHECK(defect_status IN ('open','closed')),
                    external_ref TEXT,
                    review_note TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    closed_by TEXT,
                    closed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS advisories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    level TEXT NOT NULL CHECK(level IN ({levels})),
                    triggers TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'signing'
                        CHECK(status IN ({advisory_states})),
                    note TEXT,
                    eng_decision TEXT CHECK(eng_decision IN ('approve','reject')),
                    eng_by TEXT,
                    eng_comment TEXT,
                    eng_at TEXT,
                    road_decision TEXT CHECK(road_decision IN ('approve','reject')),
                    road_by TEXT,
                    road_comment TEXT,
                    road_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, version)
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _reading(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def add_reading(self, item_id: int, kind: str, payload: Dict[str, Any],
                    risk_level: str, defect_status: Optional[str],
                    external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO readings(item_id, kind, payload, risk_level, status,
                       defect_status, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     risk_level, "active", defect_status, external_ref, actor, now),
                )
                reading_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        return self.get_reading(item_id, reading_id)

    def get_reading(self, item_id: int, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=? AND item_id=?",
                (reading_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("数据不存在")
        return self._reading(row)

    def list_readings(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM readings WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._reading(row) for row in rows]

    def review_reading(self, reading_id: int, decision: str, note: Optional[str],
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        status = "reviewed" if decision == "confirm" else "void"
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE readings SET status=?, review_note=?, reviewed_by=?, reviewed_at=?
                   WHERE id=?""",
                (status, note, actor, now, reading_id),
            )
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        return self._reading(row)

    def close_defect(self, reading_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE readings SET defect_status='closed', closed_by=?, closed_at=?
                   WHERE id=?""",
                (actor, now, reading_id),
            )
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        return self._reading(row)

    def open_defect_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM readings
                   WHERE item_id=? AND kind='inspection_defect'
                     AND defect_status='open' AND status!='void'""",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _advisory(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["triggers"] = json.loads(item["triggers"])
        return item

    def latest_advisory(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM advisories WHERE item_id=?
                   ORDER BY version DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return self._advisory(row) if row else None

    def list_advisories(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM advisories WHERE item_id=? ORDER BY version DESC",
                (item_id,),
            ).fetchall()
        return [self._advisory(row) for row in rows]

    def create_advisory(self, item_id: int, version: int, level: str,
                        triggers: List[Dict[str, Any]], status: str,
                        note: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO advisories(item_id, version, level, triggers, status,
                       note, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, version, level,
                     json.dumps(triggers, ensure_ascii=False, sort_keys=True),
                     status, note, actor, now),
                )
                advisory_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("建议版本冲突，请刷新后重试") from exc
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM advisories WHERE id=?", (advisory_id,)).fetchone()
        return self._advisory(row)

    def supersede_advisory(self, advisory_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE advisories SET status='superseded' WHERE id=?", (advisory_id,))

    def update_signoff(self, advisory_id: int, party: str, decision: str,
                       comment: Optional[str], actor: str,
                       status: str) -> Dict[str, Any]:
        if party not in ("eng", "road"):
            raise ValidationError("未知会签方")
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"""UPDATE advisories SET {party}_decision=?, {party}_by=?,
                    {party}_comment=?, {party}_at=?, status=?
                    WHERE id=? AND status IN ('signing','pending_dispute')""",
                (decision, actor, comment, now, status, advisory_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("当前版本不可会签")
            row = self.conn.execute(
                "SELECT * FROM advisories WHERE id=?", (advisory_id,)).fetchone()
        return self._advisory(row)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
