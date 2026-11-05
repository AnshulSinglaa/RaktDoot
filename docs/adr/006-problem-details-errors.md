# ADR 006: Errors as RFC 9457 problem details

Status: accepted (2026-10-10). DRAFT written by the mentor on Ansh's behalf: read it, edit it, own it.

## Context
FastAPI's default errors are `{"detail": "..."}` for HTTP errors and a different, much larger shape for validation errors.
Clients would have to parse human sentences to tell "duplicate email" from "donor not found". The default validation errors
also echo the caller's raw input, which for LifeLink means emails and health details.

## Options
1. Keep FastAPI's defaults.
2. A home-made `{"error": ..., "message": ...}` shape.
3. RFC 9457 problem details (`application/problem+json`; it replaced RFC 7807 with the same media type).

## Decision
Option 3, installed once in `errors.py` and used by every route.
- Body: `type` (stable and machine-readable), `title`, `status`, optional `detail`, `instance` (request path only, since a query string can carry personal data), plus extensions such as `errors`.
- Clients switch on `type` (`urn:lifelink:problem:duplicate-email`, `donor-not-found`, `invalid-cursor`, `idempotency-key-reuse`, `validation-error`, `database-unavailable`), never on `title` or `detail`.
- Deliberate domain errors are raised as `Problem(status, slug, title, detail)`.
- Framework errors (unknown route, wrong method) get the same format with `type: about:blank`.
- Validation problems list `{field, message}` only. Pydantic's `input` is never echoed.
- Unexpected exceptions return a generic 500 problem with no exception text. The traceback still reaches the server logs.

## Consequences
- `type` values are a public contract: renaming one is a breaking change.
- `type` is a URN, not a docs URL. Upgrade path: URLs that resolve to a documentation page per problem.
- A 409 "email already registered" lets a caller probe which emails exist (user enumeration). Acceptable before auth; Stage 2 revisits it.
- No request id in the body yet. Correlation ids arrive with structured logging (3.8).
