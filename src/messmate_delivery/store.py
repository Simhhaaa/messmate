"""SQLite test store; production persistence is provided by NeonStore."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sqlite3
import threading
import uuid

from .models import DeliveryIdentity, DeliveryLease, DeliveryRecord, DeliveryStatus


class SQLiteDeliveryStore:
    """Small SQLite implementation for local tests; not production Neon storage."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS whatsapp_deliveries (
            delivery_key TEXT PRIMARY KEY, recipient_key TEXT NOT NULL,
            plan_date TEXT NOT NULL, message_kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('prepared','sending','accepted','delivered','failed','uncertain')),
            payload_hash TEXT NOT NULL, template_name TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
            provider_message_id TEXT, last_error_code TEXT, lease_token TEXT, lease_expires_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(recipient_key, plan_date, message_kind)
        );
        """)

    def prepare(self, identity: DeliveryIdentity, payload_hash: str, template_name: str) -> DeliveryRecord:
        now = _now()
        with self._lock:
            self._conn.execute("""INSERT INTO whatsapp_deliveries
                (delivery_key,recipient_key,plan_date,message_kind,status,payload_hash,template_name,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(recipient_key,plan_date,message_kind) DO NOTHING""",
                (identity.delivery_key, identity.recipient_key, identity.plan_date.isoformat(), identity.message_kind,
                 DeliveryStatus.PREPARED.value, payload_hash, template_name, now, now))
            row = self._conn.execute("SELECT * FROM whatsapp_deliveries WHERE delivery_key=?", (identity.delivery_key,)).fetchone()
        if row is None:
            raise RuntimeError("delivery identity conflicts with an existing different key")
        if row["payload_hash"] != payload_hash:
            raise ValueError("a delivery already exists for this recipient/date with a different message")
        return _record(row, identity)

    def claim(self, identity: DeliveryIdentity, lease_seconds: int = 180) -> DeliveryLease | None:
        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute("""UPDATE whatsapp_deliveries SET status='sending', attempt_count=attempt_count+1,
                    lease_token=?, lease_expires_at=?, updated_at=? WHERE delivery_key=? AND status='prepared'""",
                    (token, (now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), identity.delivery_key))
                row = self._conn.execute("SELECT attempt_count FROM whatsapp_deliveries WHERE delivery_key=?", (identity.delivery_key,)).fetchone()
                self._conn.execute("COMMIT")
                return DeliveryLease(identity.delivery_key, token, row[0]) if cur.rowcount == 1 else None
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def finish(self, lease: DeliveryLease, status: DeliveryStatus, *, provider_message_id: str | None = None, error_code: str | None = None) -> bool:
        if status not in {DeliveryStatus.ACCEPTED, DeliveryStatus.FAILED, DeliveryStatus.UNCERTAIN}:
            raise ValueError("Phase 4 may finalize sends only as accepted, failed, or uncertain")
        cur = self._conn.execute("""UPDATE whatsapp_deliveries SET status=?,provider_message_id=?,last_error_code=?,
            lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE delivery_key=? AND lease_token=? AND status='sending'""",
            (status.value, provider_message_id, error_code, _now(), lease.delivery_key, lease.lease_token))
        return cur.rowcount == 1

    def recover_interrupted(self, now: datetime | None = None, *, delivery_key: str | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cur = self._conn.execute("""UPDATE whatsapp_deliveries SET status='uncertain',last_error_code='interrupted_send',
            lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE status='sending' AND lease_expires_at < ?
            AND (? IS NULL OR delivery_key=?)""",
            (now.isoformat(), now.isoformat(), delivery_key, delivery_key))
        return cur.rowcount

    def get(self, identity: DeliveryIdentity) -> DeliveryRecord | None:
        row = self._conn.execute("SELECT * FROM whatsapp_deliveries WHERE delivery_key=?", (identity.delivery_key,)).fetchone()
        return _record(row, identity) if row else None

    def close(self) -> None:
        self._conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(row: sqlite3.Row, identity: DeliveryIdentity) -> DeliveryRecord:
    return DeliveryRecord(identity, DeliveryStatus(row["status"]), row["payload_hash"], row["template_name"],
                          row["attempt_count"], row["provider_message_id"], row["last_error_code"])
