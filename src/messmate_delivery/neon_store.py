"""Optional Neon PostgreSQL persistence using psycopg 3."""

from __future__ import annotations

import os
from pathlib import Path
import uuid
from datetime import datetime, timezone

from .models import DeliveryIdentity, DeliveryLease, DeliveryRecord, DeliveryStatus


class NeonDeliveryStore:
    def __init__(self, database_url: str | None = None) -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install project dependencies to use Neon PostgreSQL persistence") from exc
        self._psycopg = psycopg
        self._dsn = database_url or os.environ["NEON_DATABASE_URL"]

    def migrate(self) -> None:
        sql_path = Path(__file__).with_name("migrations") / "001_whatsapp_delivery.sql"
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(sql_path.read_text(encoding="utf-8"))

    def prepare(self, identity: DeliveryIdentity, payload_hash: str, template_name: str) -> DeliveryRecord:
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute("""INSERT INTO whatsapp_deliveries
                (delivery_key,recipient_key,plan_date,message_kind,status,payload_hash,template_name)
                VALUES(%s,%s,%s,%s,'prepared',%s,%s) ON CONFLICT(recipient_key,plan_date,message_kind) DO NOTHING""",
                (identity.delivery_key, identity.recipient_key, identity.plan_date, identity.message_kind,payload_hash,template_name))
            row = conn.execute("SELECT status,payload_hash,template_name,attempt_count,provider_message_id,last_error_code FROM whatsapp_deliveries WHERE delivery_key=%s",
                               (identity.delivery_key,)).fetchone()
            if row is None or row[1] != payload_hash:
                raise ValueError("delivery identity conflicts with an existing delivery payload")
            return _record(identity,row)

    def claim(self, identity: DeliveryIdentity, lease_seconds: int = 180) -> DeliveryLease | None:
        token = uuid.uuid4().hex
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute("""UPDATE whatsapp_deliveries SET status='sending',attempt_count=attempt_count+1,
                lease_token=%s,lease_expires_at=now()+(%s * interval '1 second'),updated_at=now()
                WHERE delivery_key=%s AND status='prepared' RETURNING attempt_count""",
                (token,lease_seconds,identity.delivery_key)).fetchone()
            return DeliveryLease(identity.delivery_key,token,row[0]) if row else None

    def finish(self, lease: DeliveryLease,status: DeliveryStatus,*,provider_message_id=None,error_code=None)->bool:
        if status not in {DeliveryStatus.ACCEPTED,DeliveryStatus.FAILED,DeliveryStatus.UNCERTAIN}:
            raise ValueError("Phase 4 may finalize sends only as accepted, failed, or uncertain")
        with self._psycopg.connect(self._dsn) as conn:
            cur=conn.execute("""UPDATE whatsapp_deliveries SET status=%s,provider_message_id=%s,last_error_code=%s,
                lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE delivery_key=%s AND lease_token=%s AND status='sending'""",
                (status.value,provider_message_id,error_code,lease.delivery_key,lease.lease_token))
            return cur.rowcount==1

    def recover_interrupted(self, now: datetime | None = None, *, delivery_key: str | None = None) -> int:
        instant=now or datetime.now(timezone.utc)
        with self._psycopg.connect(self._dsn) as conn:
            cur=conn.execute("""UPDATE whatsapp_deliveries SET status='uncertain',last_error_code='interrupted_send',
                lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE status='sending' AND lease_expires_at < %s
                AND (%s::text IS NULL OR delivery_key=%s)""",(instant,delivery_key,delivery_key))
            return cur.rowcount

    def get(self,identity:DeliveryIdentity)->DeliveryRecord|None:
        with self._psycopg.connect(self._dsn) as conn:
            row=conn.execute("SELECT status,payload_hash,template_name,attempt_count,provider_message_id,last_error_code FROM whatsapp_deliveries WHERE delivery_key=%s",(identity.delivery_key,)).fetchone()
        return _record(identity,row) if row else None


def _record(identity,row):
    return DeliveryRecord(identity,DeliveryStatus(row[0]),row[1],row[2],row[3],row[4],row[5])
