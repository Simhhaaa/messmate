# Phase 4 Neon migration and concurrency verification

Run this only against a **disposable Neon test branch/database**. The test
applies the migration and writes a uniquely keyed delivery row. It does not
send a WhatsApp message. Do not use production delivery data for this check.

## 1. Prepare the isolated database

Create a Neon project and a separate test branch/database. Copy its pooled or
direct PostgreSQL connection URL from the Neon dashboard, with SSL enabled
(Neon connection strings normally include `sslmode=require`). Do not paste the
URL into source, a terminal command argument, or a log.

From the repository root, install the optional PostgreSQL driver and enter the
URL through a hidden prompt:

```sh
python3.12 -m venv .venv-neon
source .venv-neon/bin/activate
python -m pip install -e .
python -m pip install -r requirements-neon.txt
read -s "MESSMATE_TEST_NEON_DATABASE_URL?Neon test database URL: "
export MESSMATE_TEST_NEON_DATABASE_URL
```

The value is read by the integration test and is never printed by the test.
Keep the test database isolated because it receives one synthetic row with an
`uncertain` final state to exercise interrupted-delivery recovery.

## 2. Apply and inspect the migration

Run the database integration test. It invokes `NeonDeliveryStore.migrate()`,
which applies `src/messmate_delivery/migrations/001_whatsapp_delivery.sql`:

```sh
python -m unittest discover -s tests -p test_delivery.py -v
```

The Neon test must run (not show `skipped`) and pass. To inspect the resulting
constraints using a local `psql` session connected to the test database, run:

```sql
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'whatsapp_deliveries'::regclass
ORDER BY conname;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'whatsapp_deliveries'
ORDER BY indexname;
```

Confirm that the schema contains the primary key on `delivery_key`, the unique
key on `(recipient_key, plan_date, message_kind)`, the allowed-status check,
and the `(status, lease_expires_at)` lookup index.

The integration test performs two store preparations for the same identity,
then races two separate Neon store instances to claim it. Exactly one claim
must succeed. It expires only that test delivery's lease, verifies recovery to
`uncertain`, and confirms that this record cannot be claimed again. The test
scopes recovery to its own unique delivery key so it cannot alter other rows.

## 3. Run the complete suite

```sh
python -m unittest discover -s tests -v
```

For a local run without a Neon test URL, the database integration case is
expected to skip. A skipped case is not evidence of Neon production readiness.
Do not describe Neon persistence as production-verified until the isolated
database test and the constraint inspection above both pass.
