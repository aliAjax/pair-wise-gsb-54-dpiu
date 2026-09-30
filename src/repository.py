"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS segment_splices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
                    cable TEXT NOT NULL,
                    segment TEXT NOT NULL,
                    splice_point_km REAL NOT NULL,
                    splice_loss_db REAL NOT NULL,
                    occurred_at TEXT NOT NULL,
                    engineer_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    client_token TEXT,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_splices_token
                    ON segment_splices(client_token)
                    WHERE client_token IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_splices_natural
                    ON segment_splices(cable, segment, occurred_at, splice_point_km)
                    WHERE status = 'confirmed';
                CREATE INDEX IF NOT EXISTS idx_splices_segment
                    ON segment_splices(cable, segment, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_splices_record ON segment_splices(record_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], before_commit: Callable[[sqlite3.Connection, int], None] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if before_commit is not None:
                before_commit(connection, record_id)
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 区段接续档案 ----

    @staticmethod
    def _splice_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["data"] = json.loads(item.pop("payload"))
        return item

    def list_splices(self, cable: str = None, segment: str = None, status: str = None, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        clauses, params = [], []
        if cable:
            clauses.append("cable=?")
            params.append(cable)
        if segment:
            clauses.append("segment=?")
            params.append(segment)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM segment_splices%s ORDER BY occurred_at, id LIMIT ?" % where
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._splice_row(row) for row in rows]

    def get_splice(self, splice_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM segment_splices WHERE id=?", (splice_id,)).fetchone()
        if row is None:
            raise NotFound("接续记录不存在")
        return self._splice_row(row)

    def confirmed_splices(self, connection: sqlite3.Connection, cable: str, segment: str) -> List[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM segment_splices WHERE cable=? AND segment=? AND status='confirmed' ORDER BY occurred_at, id",
            (cable, segment),
        ).fetchall()
        return [self._splice_row(row) for row in rows]

    def _insert_splice(self, connection: sqlite3.Connection, entry: Dict[str, Any], record_id: Optional[int], cable: str, segment: str, actor_id: str, status: str, now: str) -> Dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO segment_splices(record_id,cable,segment,splice_point_km,splice_loss_db,occurred_at,engineer_id,source,status,client_token,payload,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, cable, segment, entry["splice_point_km"], entry["splice_loss_db"], entry["occurred_at"], entry["engineer_id"], entry["source"], status, entry.get("client_token"), json.dumps(entry, ensure_ascii=False, sort_keys=True), actor_id, now),
        )
        row = connection.execute("SELECT * FROM segment_splices WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return self._splice_row(row)

    @staticmethod
    def _audit(connection: sqlite3.Connection, record_id: int, actor_id: str, action: str, version: int, details: Dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, actor_id, action, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _persist_record(connection: sqlite3.Connection, record_id: int, version: int, state: str, payload: Dict[str, Any], actor_id: str, now: str) -> None:
        connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
        )

    def submit_splice(self, entry: Dict[str, Any], record: Dict[str, Any], actor_id: str,
                      allowed_states: set, expected_version: Optional[int],
                      decide: Callable[[sqlite3.Connection, Dict[str, Any], List[Dict[str, Any]], float], Tuple[str, Dict[str, Any], str, str]]) -> Dict[str, Any]:
        """原子提交一条接续。

        decide在插入confirmed后、故障单落库前调用，入参为锁内实时记录，返回(新状态, 新payload, 审计动作, 摘要)。
        返回 {"outcome": "accepted"|"pending"|"duplicate", "splice": ..., "record": ...}。
        """
        cable, segment = record["payload"]["cable"], record["payload"]["segment"]
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current = self._row(row)
            if current["state"] not in allowed_states:
                connection.rollback()
                raise Conflict("当前状态不允许提交接续档案")
            if expected_version is not None and int(current["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            token = entry.get("client_token")
            if token:
                existing = connection.execute("SELECT * FROM segment_splices WHERE client_token=?", (token,)).fetchone()
                if existing is not None:
                    # 写入失败后的重试：直接返回已有结果，不重复累加
                    connection.commit()
                    return {"outcome": "duplicate", "splice": self._splice_row(existing), "record": current}
            try:
                splice = self._insert_splice(connection, entry, record["id"], cable, segment, actor_id, "confirmed", now)
            except sqlite3.IntegrityError:
                # 同一物理接续已有confirmed条目：两名工程师同时提交，本条目留现场数据待确认
                cursor = connection.execute(
                    "INSERT INTO segment_splices(record_id,cable,segment,splice_point_km,splice_loss_db,occurred_at,engineer_id,source,status,client_token,payload,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record["id"], cable, segment, entry["splice_point_km"], entry["splice_loss_db"], entry["occurred_at"], entry["engineer_id"], entry["source"], "pending_confirmation", token, json.dumps(entry, ensure_ascii=False, sort_keys=True), actor_id, now),
                )
                pending_row = connection.execute("SELECT * FROM segment_splices WHERE id=?", (int(cursor.lastrowid),)).fetchone()
                self._audit(connection, record["id"], actor_id, "splice_pending", int(current["version"]), {"summary": "同一接续已有确认档案，现场数据留待确认", "occurred_at": entry["occurred_at"], "splice_point_km": entry["splice_point_km"]}, now)
                connection.commit()
                return {"outcome": "pending", "splice": self._splice_row(pending_row), "record": current}
            confirmed = self.confirmed_splices(connection, cable, segment)
            cumulative = round(sum(float(item["splice_loss_db"]) for item in confirmed), 6)
            state, payload, action_name, summary = decide(connection, current, confirmed, cumulative)
            if state != current["state"] or payload != current["payload"]:
                version = int(current["version"]) + 1
                self._persist_record(connection, record["id"], version, state, payload, actor_id, now)
            else:
                version = int(current["version"])
            self._audit(connection, record["id"], actor_id, action_name, version, {"summary": summary, "occurred_at": entry["occurred_at"], "splice_loss_db": entry["splice_loss_db"], "cumulative_splice_loss_db": cumulative, "splice_id": splice["id"]}, now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
            connection.commit()
        return {"outcome": "accepted", "splice": splice, "record": self._row(result), "cumulative_splice_loss_db": cumulative}

    def backfill_splices(self, entries: List[Dict[str, Any]], record: Dict[str, Any], actor_id: str,
                         decide: Callable[[sqlite3.Connection, Dict[str, Any], List[Dict[str, Any]], float], Tuple[str, Dict[str, Any], str, str]]) -> Dict[str, Any]:
        """批量补录历史接续，整批原子提交；已存在的同token条目视为幂等返回。"""
        cable, segment = record["payload"]["cable"], record["payload"]["segment"]
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current = self._row(row)
            if current["state"] not in ("spliced", "tested", "rectification"):
                connection.rollback()
                raise Conflict("当前状态不允许补录历史接续")
            inserted, duplicates = [], []
            existing_confirmed = self.confirmed_splices(connection, cable, segment)
            for entry in entries:
                existing = connection.execute("SELECT * FROM segment_splices WHERE client_token=?", (entry["client_token"],)).fetchone()
                if existing is not None:
                    duplicates.append(self._splice_row(existing))
                    continue
                if existing_confirmed:
                    connection.rollback()
                    raise Conflict("区段接续档案已有确认记录，无需补录")
                try:
                    inserted.append(self._insert_splice(connection, entry, record["id"], cable, segment, actor_id, "confirmed", now))
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise Conflict("区段已有该接续的确认档案") from exc
            if not inserted:
                connection.commit()
                return {"outcome": "duplicate", "splices": duplicates, "record": current}
            confirmed = self.confirmed_splices(connection, cable, segment)
            cumulative = round(sum(float(item["splice_loss_db"]) for item in confirmed), 6)
            state, payload, action_name, summary = decide(connection, current, confirmed, cumulative)
            version = int(current["version"]) + 1
            self._persist_record(connection, record["id"], version, state, payload, actor_id, now)
            self._audit(connection, record["id"], actor_id, action_name, version, {"summary": summary, "count": len(inserted), "cumulative_splice_loss_db": cumulative}, now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
            connection.commit()
        return {"outcome": "accepted", "splices": inserted, "record": self._row(result), "cumulative_splice_loss_db": cumulative}

    def resolve_splice(self, splice_id: int, actor_id: str, resolution: str, note: str,
                       reevaluate: Callable[[sqlite3.Connection, Dict[str, Any], List[Dict[str, Any]], float, str], Tuple[str, Dict[str, Any], str]] = None) -> Dict[str, Any]:
        """对待确认的现场数据作出决定：reject 留痕丢弃；confirm 替换原确认档案并重算受影响故障单。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM segment_splices WHERE id=?", (splice_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("接续记录不存在")
            pending = self._splice_row(row)
            if pending["status"] != "pending_confirmation":
                connection.rollback()
                raise Conflict("该接续记录不在待确认状态")
            if resolution == "reject":
                connection.execute("UPDATE segment_splices SET status='rejected', resolved_at=? WHERE id=?", (now, splice_id))
                if pending["record_id"] is not None:
                    rec = connection.execute("SELECT version FROM records WHERE id=?", (pending["record_id"],)).fetchone()
                    if rec is not None:
                        self._audit(connection, int(pending["record_id"]), actor_id, "splice_rejected", int(rec["version"]), {"summary": "待确认现场数据已拒绝", "splice_id": splice_id, "note": note}, now)
                connection.commit()
                return {"outcome": "rejected", "splice": self.get_splice(splice_id)}
            # confirm：先降格原confirmed，再提升本条，避免与部分唯一索引冲突
            old_rows = connection.execute(
                "SELECT * FROM segment_splices WHERE cable=? AND segment=? AND occurred_at=? AND splice_point_km=? AND status='confirmed'",
                (pending["cable"], pending["segment"], pending["occurred_at"], pending["splice_point_km"]),
            ).fetchall()
            for old in old_rows:
                connection.execute("UPDATE segment_splices SET status='superseded', resolved_at=? WHERE id=?", (now, int(old["id"])))
            connection.execute("UPDATE segment_splices SET status='confirmed', resolved_at=? WHERE id=?", (now, splice_id))
            affected = []
            for old in old_rows:
                if old["record_id"] is not None and int(old["record_id"]) not in affected:
                    affected.append(int(old["record_id"]))
            if pending["record_id"] is not None and int(pending["record_id"]) not in affected:
                affected.append(int(pending["record_id"]))
            record = None
            confirmed = self.confirmed_splices(connection, pending["cable"], pending["segment"])
            cumulative = round(sum(float(item["splice_loss_db"]) for item in confirmed), 6)
            for record_id in affected:
                rec_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                if rec_row is None:
                    continue
                rec = self._row(rec_row)
                if rec["state"] == "restored":
                    # 已恢复流量的历史单仅留审计，不改状态
                    self._audit(connection, record_id, actor_id, "splice_confirmed", int(rec["version"]), {"summary": "接续档案替换入档（历史已恢复单，状态不变）", "splice_id": splice_id, "cumulative_splice_loss_db": cumulative}, now)
                    continue
                new_state, new_payload, summary = reevaluate(rec, confirmed, cumulative, now)
                version = int(rec["version"]) + 1
                self._persist_record(connection, record_id, version, new_state, new_payload, actor_id, now)
                self._audit(connection, record_id, actor_id, "splice_confirmed", version, {"summary": summary, "splice_id": splice_id, "cumulative_splice_loss_db": cumulative}, now)
                if pending["record_id"] is not None and record_id == int(pending["record_id"]):
                    record = self._row(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
            if record is None and pending["record_id"] is not None:
                record = self.get(int(pending["record_id"]))
            connection.commit()
        result = {"outcome": "confirmed", "splice": self.get_splice(splice_id)}
        if record is not None:
            result["record"] = record
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
