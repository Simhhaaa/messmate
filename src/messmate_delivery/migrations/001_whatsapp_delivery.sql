CREATE TABLE IF NOT EXISTS whatsapp_deliveries (
    delivery_key TEXT PRIMARY KEY,
    recipient_key TEXT NOT NULL,
    plan_date DATE NOT NULL,
    message_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared','sending','accepted','delivered','failed','uncertain')),
    payload_hash TEXT NOT NULL,
    template_name TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    provider_message_id TEXT,
    last_error_code TEXT,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (recipient_key, plan_date, message_kind)
);

CREATE INDEX IF NOT EXISTS whatsapp_deliveries_status_lease_idx
    ON whatsapp_deliveries(status, lease_expires_at);
