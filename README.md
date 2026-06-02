# Titan — Multi-Tenant Open Library Catalog Service

A backend service for a consortium of public libraries. Each library (a
**tenant**) gets its own catalog, ingested from the public
[Open Library](https://openlibrary.org/developers/api) API, plus a way for
patrons to submit reading lists. Tenant data is isolated end to end — one
library's catalog and submissions never appear in another's API responses.

The service does three things:

1. **Catalog ingestion** — fetch works from Open Library by author or subject,
   enrich them into complete records, and store them per tenant. Ingestion runs
   in the background and is tracked as a job.
2. **Catalog retrieval** — list, paginate, filter, search, and fetch book
   detail and per-book version history.
3. **Reading-list submissions** — accept patron reading lists, resolve the
   referenced books, and persist the submission with PII hashed/masked.

---

## Architecture

The system is a small set of cooperating processes around a Postgres database:

```
                       ┌───────────────────────────┐
   HTTP client ───────▶│  API  (FastAPI, app.api)   │
   (X-Tenant header)   │  - retrieval (read)        │
                       │  - reading-list submit     │
                       │  - POST /ingest → enqueue  │
                       └─────────────┬──────────────┘
                                     │ enqueue / read
                                     ▼
                       ┌───────────────────────────┐
                       │   Postgres (the DB *and*   │
                       │   the job queue)           │
                       │   ingestion_runs = queue   │
                       └───▲──────────────┬─────────┘
              claim (FOR   │              │ re-enqueue stale
       UPDATE SKIP LOCKED) │              │
                ┌──────────┴───┐   ┌──────┴───────────┐
                │  Worker      │   │  Scheduler        │
                │ app.worker   │   │ app.scheduler     │
                │ runs ingest  │   │ freshness re-sync │
                └──────┬───────┘   └───────────────────┘
                       │ HTTP (rate-limited, retried)
                       ▼
                 Open Library API
```

- The **API** never blocks on Open Library. `POST /ingest` writes a `queued`
  job row and returns `202` immediately. All read endpoints serve from Postgres.
- The **worker** claims queued jobs and runs the ingestion pipeline. It's a
  separate process, so ingestion load never affects API latency. Multiple
  workers can run at once.
- The **scheduler** (optional) periodically re-enqueues jobs whose data has
  gone stale, so catalogs stay fresh with no manual action.

### Key design decisions

**Postgres *is* the job queue.** Rather than adding Redis/Celery, the
`ingestion_runs` table doubles as a durable queue. Workers claim jobs with
`SELECT … FOR UPDATE SKIP LOCKED`, the canonical Postgres pattern: each job is
claimed by exactly one worker, multiple workers never block each other, and
jobs survive a process crash because they're rows, not in-memory tasks. The
row lock is held only for the claim — the slow HTTP work happens after the lock
is released. This same `ingestion_runs` table backs the **activity log**, so
job status and the audit trail are one source of truth.

**Tenant isolation at the storage boundary.** The repository layer is the only
code that touches `tenant_id`; every write is stamped with it and every read is
filtered by it. The natural key `(tenant_id, work_key)` makes ingestion
idempotent per tenant. A book detail/version request that names a valid id
belonging to another tenant returns `404`, not someone else's data.

**Search-first ingestion with selective enrichment.** Open Library's
`/search.json` already returns most fields we need. The pipeline detects which
fields a given record is *missing* and only then makes follow-up requests to
`/works/{id}` (subjects/title) and `/authors/{id}` (names). In practice this
skips enrichment for ~90% of records, dramatically cutting request volume while
still demonstrating multi-endpoint assembly. Resolved authors are cached
globally (Open Library author data is the same for every tenant), maximizing
the rate-limit savings.

**PII is never stored in plaintext.** A patron's email is reduced to an
`HMAC-SHA256(pepper, normalized_email)` — deterministic, so a returning patron
is recognizable for dedup, but irreversible without the server-side pepper, so
a database leak can't be turned back into email addresses. Only the hash and
masked display strings (`j***@g***.com`, `U*** D***`) are persisted; the raw
name/email exist just long enough to derive them.

**Version tracking instead of overwrites.** A book's stable identity
(`BookBase`) is separated from its metadata snapshots (`BookVersion`). On
re-ingestion the new record is diffed against the latest version and a *new*
version is appended only when something changed — so history is preserved and a
**data regression** (a field that had a value and is now missing) is recorded
as a change rather than silently dropped. The catalog views a book as its
current (latest) version.

**Queue-agnostic worker body.** `run_ingestion()` is a pure pipeline that takes
a job and a session. The API (inline enqueue), the worker (claim → run), and
the CLI script all call into the same code — there's no duplicated ingestion
logic, and swapping the queue implementation later changes only the caller.

---

## Data model

| Table | Purpose | Notes |
|-------|---------|-------|
| `tenants` | One row per library | `slug` is the public identifier used in the `X-Tenant` header |
| `books_base` | Stable book identity | unique `(tenant_id, work_key)`; tenant-scoped |
| `book_versions` | Append-only metadata snapshots | `title`, `first_publish_year`, `author_names[]`, `subjects[]`, `cover_url`, `raw` (jsonb); latest = current |
| `authors_cache` | Resolved Open Library authors | **global**, not tenant-scoped (OL data is shared) |
| `ingestion_runs` | Job queue **and** activity log | `status` = `queued → running → succeeded/failed`; holds counts + errors |
| `reading_list_submissions` | Patron reading lists | **no plaintext PII** — `patron_hash` + masked strings + resolved/unresolved work keys |

Tables are created automatically on API/worker startup (`init_db()`); there is
no separate migration step for local development.

---

## Tech stack

- **Python 3.14**, managed with [uv](https://docs.astral.sh/uv/)
- **FastAPI** + **uvicorn** — API
- **SQLAlchemy 2.0** + **psycopg 3** — ORM / Postgres driver
- **Postgres 17** — storage + job queue
- **requests** — Open Library HTTP client
- **pydantic-settings** — env-driven config
- **email-validator** — submission email validation

---

## Setup & run

### Prerequisites
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker (for Postgres)

### 1. Start Postgres
```bash
docker compose up -d
```
Postgres listens on host port **5433** (mapped to the container's 5432) to avoid
clashing with a local Postgres on 5432.

### 2. Install dependencies
```bash
uv sync
```

### 3. Run the processes
Each runs in its own terminal:

```bash
# API
uv run uvicorn app.api:app --reload --port 8000

# Ingestion worker (required for POST /ingest to be processed)
uv run python -m app.worker

# Freshness scheduler (optional — keeps catalogs up to date)
uv run python -m app.scheduler
```

The API auto-creates tables on startup. Open **http://localhost:8000/docs** for
interactive Swagger UI.

> **Reset the database** (e.g. after a schema change): `docker compose down -v`
> then `docker compose up -d`.

### Ingest without the API/worker
For a quick synchronous run against the live API:
```bash
uv run python -m scripts.run_ingest <tenant-slug> author "Ursula K. Le Guin"
uv run python -m scripts.run_ingest <tenant-slug> subject "graph theory"
```

### Configuration

All settings are environment variables prefixed `TITAN_` (also read from a
`.env` file). The defaults work out of the box for local development.

| Variable | Default | Description |
|----------|---------|-------------|
| `TITAN_DATABASE_URL` | `postgresql+psycopg://titan:titan@localhost:5433/titan` | Postgres DSN |
| `TITAN_PII_PEPPER` | dev placeholder | **Set in production** — secret keying the email HMAC |
| `TITAN_REQUEST_MIN_INTERVAL_SECONDS` | `0.2` | Min spacing between Open Library requests (~5 req/s) |
| `TITAN_MAX_RETRIES` | `4` | Retries on 429/5xx/network errors (exponential backoff) |
| `TITAN_MAX_PAGES_PER_RUN` | `5` | Cap on search pages pulled per ingestion |
| `TITAN_SEARCH_PAGE_SIZE` | `100` | Results per search page |
| `TITAN_WORKER_POLL_INTERVAL_SECONDS` | `2.0` | Worker idle poll interval |
| `TITAN_FRESHNESS_INTERVAL_SECONDS` | `86400` | Re-sync a job if its last run is older than this |
| `TITAN_SCHEDULER_TICK_SECONDS` | `30` | Scheduler tick interval |

---

## API

Every catalog operation is scoped to a tenant via the **`X-Tenant`** header
(the tenant's slug). Requests without it get `400`; an unknown slug gets `404`.

| Method | Path | `X-Tenant`? | Description |
|--------|------|:-----------:|-------------|
| `GET` | `/health` | no | Liveness check |
| `POST` | `/tenants` | no | Register a tenant (`{slug, name}`) |
| `POST` | `/ingest` | yes | Enqueue ingestion (`{kind: author\|subject, value}`) → `202` |
| `GET` | `/activity` | yes | Ingestion job log / status |
| `GET` | `/books` | yes | List + paginate + filter + search |
| `GET` | `/books/{id}` | yes | Single book (current version) |
| `GET` | `/books/{id}/versions` | yes | Full version history for a book |
| `POST` | `/reading-lists` | yes | Submit a patron reading list |
| `GET` | `/reading-lists` | yes | Staff view of submissions (masked PII) |

`GET /books` query parameters: `author` (partial match), `subject` (exact),
`year_min`, `year_max`, `q` (keyword over title/author), `limit` (1–100,
default 25), `offset`.

### Walkthrough

```bash
B=http://localhost:8000

# 1. Register a tenant
curl -s -X POST $B/tenants -H 'Content-Type: application/json' \
  -d '{"slug":"demo","name":"Demo Library"}'

# 2. Kick off an ingestion (returns 202 + a "queued" run; the worker processes it)
curl -s -X POST $B/ingest -H 'X-Tenant: demo' -H 'Content-Type: application/json' \
  -d '{"kind":"author","value":"Ursula K. Le Guin"}'

# 3. Watch it move queued -> running -> succeeded
curl -s $B/activity -H 'X-Tenant: demo'

# 4. Browse the catalog
curl -s "$B/books?limit=5"                    -H 'X-Tenant: demo'
curl -s "$B/books?year_min=1960&year_max=1970" -H 'X-Tenant: demo'
curl -s "$B/books?subject=Science%20fiction"   -H 'X-Tenant: demo'
curl -s "$B/books?q=earthsea"                   -H 'X-Tenant: demo'

# 5. Detail + version history (grab an id from the list above)
curl -s "$B/books/<book-id>"          -H 'X-Tenant: demo'
curl -s "$B/books/<book-id>/versions" -H 'X-Tenant: demo'

# 6. Submit a reading list (work IDs and/or ISBNs)
curl -s -X POST $B/reading-lists -H 'X-Tenant: demo' -H 'Content-Type: application/json' \
  -d '{"name":"Jane Doe","email":"jane@example.com",
       "books":["/works/OL59798W","OL59817W","9780441172719","not-a-book"]}'

# 7. Staff view of submissions (masked PII only)
curl -s $B/reading-lists -H 'X-Tenant: demo'
```

A reading-list submission resolves each identifier against the **tenant's own
catalog**: a work ID counts as resolved if it has been ingested, and an ISBN is
mapped to a work key via Open Library's `/isbn` endpoint and then checked
locally. The response confirms exactly which books `resolved` and which were
`unresolved`.

---

## Requirements coverage

**Tier 1 — Core service**
- Ingest & store by author/subject, with author resolution and gap-filling
  follow-up requests across `/search`, `/works`, and `/authors`.
- Retrieval API: list/paginate, filter (author/subject/year range), keyword
  search, single detail.
- Activity log of every ingestion (request, counts, status, errors, timing).

**Tier 2 — Production concerns**
- PII handling: HMAC-hashed email (dedup key) + masked name/email; no plaintext
  stored; response confirms resolved vs unresolved books.
- Background processing: durable Postgres-backed queue + worker; `POST /ingest`
  is non-blocking; status visible via `/activity`; scheduler keeps catalogs
  fresh automatically.

**Tier 3 — Depth**
- Version management: per-work version history, change-only versioning, and
  regression handling, exposed via `GET /books/{id}/versions`.

---

## Project layout

```
app/
  api.py           FastAPI app — routes, tenant dependency, lifespan
  config.py        env-driven settings (TITAN_*)
  db.py            engine, session_scope(), init_db()
  db_models.py     ORM: Tenant, BookBase, BookVersion, AuthorCache,
                   IngestionRun, ReadingListSubmission
  types.py         DB-free domain dataclasses passed between pipeline stages
  openlibrary.py   Open Library HTTP client (rate-limited, retried)
  normalize.py     gap detection + record assembly
  ingestion.py     run_ingestion() — the queue-agnostic worker body
  repository.py    all DB reads/writes; the only layer touching tenant_id
  reading_lists.py reading-list resolution (work IDs / ISBNs → local catalog)
  pii.py           email hashing + name/email masking
  worker.py        background ingestion worker (claim → run)
  scheduler.py     freshness re-sync loop
  schemas.py       Pydantic request/response models
scripts/
  run_ingest.py    CLI to run ingestion synchronously against the live API
docker-compose.yml Postgres 17
```

---

## What I'd do differently with more time

- **One-command startup.** Add a `Dockerfile` and run the API, worker, and
  scheduler as `docker compose` services (plus a `make run`) so the whole stack
  comes up with a single command, rather than three `uv run` processes.
- **Migrations.** Replace `create_all()` with Alembic so schema changes are
  versioned instead of needing a volume reset.
- **Automated tests.** The pipeline was verified manually against the live API;
  it deserves a real test suite — unit tests for normalization/PII/resolution
  and integration tests against an ephemeral Postgres, with Open Library mocked
  at the HTTP boundary.
- **Noisy-neighbor throttling (Tier 3).** Make the worker's claim query
  tenant-fair and add per-tenant rate/usage accounting so one library's large
  ingestion can't monopolize the workers. The current FIFO queue is the
  foundation for this.
- **Progress granularity.** Ingestion counts are committed at the end of a run;
  committing per page would let `/activity` show live progress for long runs.
- **Richer version diffs.** Surface a computed per-version changelog (using the
  existing `diff_versions` helper) directly in the versions endpoint.
- **Authorz & rate limiting on the API itself** (auth per tenant, request
  limits) — out of scope here but needed before this faces real traffic.
- **Project structure.** The current flat `app/` is fine for this size codebase, but as it grows I'd split into submodules (`app.api`, `app.db`, `app.ingestion`, etc.) for better organization and to avoid a monolithic `api.py`.
- **Code auditing.** This app has been largely produced using AI tools. Manual tests and code review was conducted at each stage, but none-the-less this codebase needs more time and testing to be confident in.
- **Document manual tests** For the purposes of an interview, the manual testing documented at each step hasn't been well documented. Largely, these manual tests utilized curl and direct db queries with some in-code print statements. The only of these that has even been somewhat documented is the curl commands, which are in `CURL.md`. A more thorough documentation of the manual testing process and results would be needed to have display the dev process.

---

## Prompt log

Per the project brief, every AI interaction during development is captured in
[`prompt-log.md`](./prompt-log.md), appended automatically via Claude Code hooks.
