Open Library Catalog Service
First things first: Before writing any code, set up your AI tool to log all interactions to a prompt-log.md file in the root of your repository. This file is required — it will be reviewed as part of your evaluation. Every prompt you send and every response you receive should be captured. Start now.

Scenario
You are a backend engineer at a startup building tools for a consortium of public libraries. The consortium operates multiple library branches, each with their own catalog needs and patron base. They want a unified, multi-tenant catalog service that aggregates book data from Open Library — a free, open-source, community-maintained book database — and makes it searchable and browsable for library patrons and staff.

Each library tenant has its own catalog and patron base. The service must keep tenant data isolated — one library's catalog and reading lists must never leak into another's API responses.

The service has two responsibilities:

Catalog Ingestion — Fetch and store book data from Open Library's public API, scoped to individual library tenants.
Reading List Submissions — Library patrons can submit personal reading lists (name, email, and a list of books) through your service. These submissions contain personally identifiable information (PII) that must be handled carefully.
Your task: build a multi-tenant catalog service in Python that ingests books from Open Library and provides a local API for searching, browsing, and managing reading list submissions. All API operations should be scoped to a specific tenant.

API Reference
Open Library provides a public API documented here:

https://openlibrary.org/developers/api

The API requires no signup or API key — you can start making requests immediately.

A few things to keep in mind:

Open Library's data has been contributed by thousands of volunteers over many years. The data is not always consistent or complete across different endpoints.
The API documentation is spread across multiple pages. You will need to explore.
Rate limiting applies — be respectful with your requests during development.
Do not hard-code or cache sample API responses as test fixtures in place of real API calls. Your service must call the live Open Library API.

Requirements
Tier 1 — Core Service (Required)
Build a working catalog ingestion and retrieval service:

Ingest & Store — Given an author name or subject, fetch works from Open Library, resolve each work's author information, and store the results locally. Each stored book should include at minimum: title, author name(s), first publish year, subjects, and a cover image URL (if available).
Your ingestion logic must handle the case where a single API response does not contain all the fields you need — you may need to make follow-up requests to different endpoints to assemble a complete record.

Retrieval API — Expose endpoints to: - List all stored books with pagination - Filter by author, subject, or publish year range - Search stored books by keyword (title or author) - Get a single book's full detail

Activity Log — Record every ingestion operation: what was requested (author/subject), how many works were fetched, how many succeeded or failed, timestamps, and any errors encountered. Expose this log via an API endpoint.

Tier 2 — Production Concerns (Expected)
PII Handling — Expose an endpoint that accepts reading list submissions from patrons. Each submission includes a patron's name, email address, and a list of Open Library work IDs (or ISBNs) representing books they want to read.
Before persisting any submission, PII fields (name, email) must be masked or irreversibly hashed. The stored record should retain enough information to deduplicate submissions by the same patron (i.e., the same email submitted twice should be recognizable as the same person) without storing the original PII in plaintext.

The submission response should confirm which books were resolved successfully and which could not be found.

Job Queue & Background Processing — Catalog ingestion must not block API responses. Open Library's API has rate limits and can be slow or unreliable — your design should handle this gracefully. Provide visibility into ingestion progress and status. The catalog should also stay fresh over time without requiring manual intervention.
Tier 3 — Depth (Differentiators)
Pick any that interest you — depth on one is better than surface-level on all:

Version Management — When a previously-ingested work is re-fetched (via sync or manual re-ingestion) and its metadata has changed, create a new version rather than overwriting. Track version history per work and expose it via API (e.g., what changed between versions, when each version was recorded). Handle the case where Open Library data regresses (e.g., a field that previously had a value is now missing).

Noisy Neighbor Throttling — Prevent any single tenant from monopolizing shared resources. One tenant's heavy usage should not degrade the experience for other tenants. Provide visibility into per-tenant resource consumption.

Deliverables
Push your code to a public GitHub repository
Your solution must be written in Python
Single-command startup (make run, docker compose up, or equivalent)
Include a README.md with:
Architecture overview and key design decisions
Setup and run instructions
API documentation or examples (curl commands, etc.)
What you would do differently with more time
Include your prompt-log.md capturing all AI tool interactions (required)
Your service should be runnable and testable locally
Evaluation
We evaluate both what you built and how you built it (via your prompt log and commit history). Building with AI is required and expected — what matters is how effectively you direct it.