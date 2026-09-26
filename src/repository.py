from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
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
                    category TEXT NOT NULL
                        CHECK(category IN ('sensor_peak','inspection_defect','vehicle_load','weather')),
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    review_count INTEGER NOT NULL DEFAULT 0,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    level TEXT NOT NULL
                        CHECK(level IN ('observe','limit_load','restrict','close')),
                    computed_level TEXT NOT NULL,
                    triggers TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','disputed','effective','released','superseded')),
                    engineer_level TEXT, engineer_by TEXT, engineer_at TEXT,
                    authority_level TEXT, authority_by TEXT, authority_at TEXT,
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

    def record_kind_count(self, item_id: int, kind: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND kind=?",
                (item_id, kind),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _reading(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def add_reading(self, item_id: int, category: str, payload: Dict[str, Any],
                    external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO readings(item_id, category, payload, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, category, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     "open", external_ref, actor, now),
                )
                reading_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("登记数据唯一标识已存在") from exc
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("登记数据不存在")
        return self._reading(row)

    def list_readings(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM readings WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._reading(row) for row in rows]

    def mark_reading_reviewed(self, reading_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE readings SET review_count=review_count+1, reviewed_by=?, reviewed_at=?
                   WHERE id=?""",
                (actor, now, reading_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("登记数据不存在")
        return self.get_reading(reading_id)

    def close_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE readings SET status='closed' WHERE id=? AND status='open'",
                (reading_id,),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM readings WHERE id=?", (reading_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("登记数据不存在")
                raise ConflictError("登记数据已关闭")
        return self.get_reading(reading_id)

    def open_defect_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM readings
                   WHERE item_id=? AND category='inspection_defect' AND status='open'""",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def evaluation_readings(self, item_id: int) -> List[Dict[str, Any]]:
        """参评数据：传感/荷载/天气各取最新一条，缺陷取全部未关闭。"""
        self.get_item(item_id)
        with self._lock:
            latest = self.conn.execute(
                """SELECT r.* FROM readings r
                   JOIN (SELECT category, MAX(id) AS mid FROM readings
                         WHERE item_id=? AND category!='inspection_defect'
                         GROUP BY category) t ON r.id=t.mid""",
                (item_id,),
            ).fetchall()
            defects = self.conn.execute(
                """SELECT * FROM readings
                   WHERE item_id=? AND category='inspection_defect' AND status='open'
                   ORDER BY id""",
                (item_id,),
            ).fetchall()
        return [self._reading(row) for row in latest] + [self._reading(row) for row in defects]

    @staticmethod
    def _recommendation(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["triggers"] = json.loads(item["triggers"])
        return item

    def create_recommendation(self, item_id: int, level: str, triggers: List[Dict[str, Any]],
                              actor: str) -> Dict[str, Any]:
        """重算入口：旧版本全部失效留档，新版本待会签。"""
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE recommendations SET status='superseded'
                   WHERE item_id=? AND status!='superseded'""",
                (item_id,),
            )
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM recommendations WHERE item_id=?",
                (item_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            cur = self.conn.execute(
                """INSERT INTO recommendations(item_id, version, level, computed_level, triggers,
                   status, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (item_id, version, level, level,
                 json.dumps(triggers, ensure_ascii=False, sort_keys=True),
                 "pending", actor, now),
            )
            rec_id = int(cur.lastrowid)
        return self.get_recommendation(rec_id)

    def get_recommendation(self, rec_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
        if row is None:
            raise NotFoundError("建议版本不存在")
        return self._recommendation(row)

    def list_recommendations(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM recommendations WHERE item_id=? ORDER BY version DESC",
                (item_id,),
            ).fetchall()
        return [self._recommendation(row) for row in rows]

    def current_recommendation(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM recommendations
                   WHERE item_id=? AND status!='superseded'
                   ORDER BY version DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return self._recommendation(row) if row else None

    def sign_recommendation(self, rec_id: int, side: str, level: str,
                            actor: str) -> Dict[str, Any]:
        """会签：双方意见一致则生效，不一致则待决；失效或已发布版本拒绝签署。"""
        if side not in ("engineer", "authority"):
            raise ConflictError("未知会签方")
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            if row is None:
                raise NotFoundError("建议版本不存在")
            if row["status"] not in ("pending", "disputed"):
                raise ConflictError("当前版本不可会签")
            self.conn.execute(
                f"UPDATE recommendations SET {side}_level=?, {side}_by=?, {side}_at=? WHERE id=?",
                (level, actor, now, rec_id),
            )
            row = self.conn.execute(
                "SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            engineer, authority = row["engineer_level"], row["authority_level"]
            if engineer and authority:
                if engineer == authority:
                    self.conn.execute(
                        "UPDATE recommendations SET status='effective', level=? WHERE id=?",
                        (engineer, rec_id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE recommendations SET status='disputed' WHERE id=?", (rec_id,))
        return self.get_recommendation(rec_id)

    def release_recommendation(self, rec_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE recommendations SET status='released' WHERE id=? AND status='effective'",
                (rec_id,),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM recommendations WHERE id=?", (rec_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("建议版本不存在")
                raise ConflictError("会签未一致，不能直接放行")
        return self.get_recommendation(rec_id)

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
