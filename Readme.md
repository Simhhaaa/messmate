# MESSMATE

Backend-only system for scraping the RU Print Portal, estimating nutrition,
optimizing mess-food plans, and preparing Telegram delivery. WhatsApp remains
available as an explicitly selectable alternative. There is no
frontend or continuously running server.

## Setup

Requires Python 3.12+.

```sh
python -m pip install -e .
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" playwright install chromium
```

## Run

The default target date is the current date in `Asia/Kolkata`. To request a
future weekday menu explicitly:

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" python -m messmate_scraper --target-date 2026-11-06
```

Raw snapshots are written under `data/raw_snapshots/YYYY-MM-DD/`, one JSON file
per attempt. Successful snapshots contain the raw page HTML and visible text
captured after each meal section is expanded. Failed attempts retain diagnostic
page text/HTML when the browser could provide it. Each captured state is also
written as a separate `.html` and `.txt` sidecar so it can be inspected without
extracting it from the JSON file.

Retry settings can be supplied as flags or environment variables:

| Flag | Environment variable | Default |
|---|---|---:|
| `--attempts` | `MESSMATE_MENU_RETRY_ATTEMPTS` | 3 |
| `--retry-initial-seconds` | `MESSMATE_MENU_RETRY_INITIAL_SECONDS` | 30 |
| `--retry-multiplier` | `MESSMATE_MENU_RETRY_MULTIPLIER` | 2 |
| `--retry-max-seconds` | `MESSMATE_MENU_RETRY_MAX_SECONDS` | 300 |

Only an empty/unavailable menu or a transient page-load timeout is retried.
Invalid dates and changed page structure fail closed. A successful result
requires all four sections to contain at least one item. The parser only splits
on separators observed in the portal; unmarked text is preserved as-is rather
than divided by guessed punctuation.

## Tests

The parser and retry tests use Python's standard `unittest` library and do not
need a browser. Run them from the project root:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```
# Phase 4: Channel-based message delivery

The delivery package is backend-only and dry-run-first. Its default service
mode only renders the message and persists a `prepared` record. It does not
contact a messaging provider. Controlled live testing requires `dry_run=False`
and the channel's explicit live-send gate, plus its environment
variables `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and
`WHATSAPP_TEMPLATE_NAME`, plus the currently supported `WHATSAPP_GRAPH_VERSION`.
Never put credentials in source control or logs.

Before using Neon, install the optional driver with
`pip install -r requirements-neon.txt`, set `NEON_DATABASE_URL`, then invoke
`NeonDeliveryStore.migrate()`. The migration creates the delivery table and
unique key `(recipient_key, plan_date, message_kind)`. Delivery identity stores
a SHA-256 recipient key rather than a phone number. Claiming is a conditional
transactional update from `prepared` to `sending`; only one concurrent caller
can claim a given plan.

Delivery statuses are `prepared`, `sending`, `accepted`, `delivered`, `failed`,
and `uncertain`. Phase 4 has no webhook receiver, so application code never
sets `delivered`; an API acceptance is stored as `accepted`. A timed-out or
interrupted send becomes `uncertain`, which is never claimed or automatically
resent. Expired leases are recovered as `uncertain`. Only explicit HTTP 429
rejections are retried, with a small bounded retry count; ambiguous failures
are not retried.

The WhatsApp adapter sends an approved template with a single body parameter.
Configure a matching approved template in Meta before controlled live tests.
The test suite uses mocked adapters and SQLite; the optional
Neon concurrency integration test runs only when
`MESSMATE_TEST_NEON_DATABASE_URL` points at an isolated test database.

Generate the Nov 6, 2026 dry-run message from the saved Phase 1 snapshot:

```sh
python -m messmate_delivery.demo tests/fixtures/nov6_menu_fixture.json
```

The message formatter uses the nutrition module's configured household serving
units, shows estimated gram weights and identifies dry/cooked weights. It
includes the optimizer's reported shortfall; it does not claim the displayed
Nov 6 plan meets the protein target.

## Phase 5: GitHub Actions automation

`.github/workflows/daily-messmate.yml` runs at `18:30 UTC` (`00:00` IST on the
next date) and supports manual dispatch. Manual runs default to dry-run and
accept an optional target date. A scheduled run also stays in dry-run unless
the GitHub Actions Secret `MESSMATE_LIVE_SEND` is exactly `true`. A manual live
run additionally requires setting its `dry_run` input to false. The Python
runner independently enforces these two gates.

Configure these repository or environment Secrets before the workflow can
complete a Neon-backed run. The three private nutrition profile settings are
also required; set them as Actions Secrets and keep them out of tracked files:

- `MESSMATE_USER_HEIGHT_CM`
- `MESSMATE_USER_WEIGHT_KG`
- `MESSMATE_USER_GOAL`

For local runs, put those three values in the ignored `.env.local` file at the
repository root, using one `KEY=value` per line. The optimizer intentionally
has no personal profile defaults and fails with missing setting names when
configuration is incomplete. Nutrition targets remain application policy in
source. Never commit the private file.

- `NEON_DATABASE_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `WHATSAPP_RECIPIENT`
- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_TEMPLATE_NAME`
- `WHATSAPP_TEMPLATE_LANGUAGE`
- `WHATSAPP_GRAPH_VERSION`
- `MESSMATE_LIVE_SEND` (leave unset or set to `false` to keep scheduled runs dry)

The workflow installs Python 3.12, Chromium and the optional Neon driver, runs
the unit suite, scrapes with bounded retries, optimizes the menu, persists the
delivery record to Neon, and uploads a metadata-only operational report for one day. The artifact omits
menu items, meal plans, nutrition totals and recipient details. Artifact
download access follows repository read permissions (and public repositories
may expose artifacts publicly), so the report contains no private plan data.
Raw menu snapshots are kept out of artifacts. Logs include status and exception types only; they
omit phone numbers, credentials and exception text. GitHub
concurrency and the Neon unique delivery identity/atomic claim both guard
against duplicate sends. Live messaging remains disabled until an operator
explicitly configures the live-send Secret and, for a manual run, selects
`dry_run=false`.

## Phase 6: Telegram delivery

Telegram is the default scheduled and manual channel. For scheduled runs, set
the repository Actions Variable `MESSMATE_CHANNEL` to `whatsapp` only when
WhatsApp is explicitly desired; leaving it unset selects Telegram. Manual runs
offer a channel selector that defaults to Telegram. Both channels use the same
dry-run and explicit live-send safety gates. Manual runs default to
`dry_run=true`; scheduled runs remain dry-run unless the existing
`MESSMATE_LIVE_SEND` Secret is exactly `true`.

Configure these Actions Secrets for Telegram delivery:

- `TELEGRAM_BOT_TOKEN` — create/manage the bot through Telegram's BotFather.
- `TELEGRAM_CHAT_ID` — numeric conversation ID (including a leading minus for
  groups) or a supported `@username`.

Start a conversation with the bot (send `/start`) or add it to the target group
before configuring the chat ID. Keep both values in
GitHub Secrets or the local process environment only. Do not put them in source
files, reports, command-line arguments, or chat messages. The Telegram adapter
calls the Bot API `sendMessage` method with plain text and no `parse_mode`; it
rejects messages over Telegram's 4096-character limit. Provider URLs include
the bot token by API design, so the adapter never logs request URLs, request or
response bodies, chat IDs, tokens, or raw provider exceptions. Definite HTTP
429 rejections may be retried using Telegram's `retry_after`; transport or
server failures are marked `uncertain` and are never automatically resent.

WhatsApp remains selectable by setting `MESSMATE_CHANNEL=whatsapp` for the
scheduled workflow or choosing WhatsApp in manual dispatch. It continues to
use its existing recipient, template, and Cloud API Secrets. Channel-specific
delivery identities keep Telegram and WhatsApp records separate while the
existing WhatsApp identity derivation remains unchanged.

## Existing repository note

Testing
