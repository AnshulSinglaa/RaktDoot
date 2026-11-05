"""One error format for the whole API: RFC 9457 problem details (it replaced RFC 7807; same media type).

Clients switch on `type` (stable, machine-readable), never on the human text in `title` or `detail`.
"""
import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
TYPE_PREFIX = "urn:lifelink:problem:"       # ponytail: a URN, not a docs page. Upgrade path: URLs that resolve to docs.
# ponytail: any OSError counts as "database unreachable" (asyncpg raises raw socket errors: refused, reset, DNS, connect
# timeout); this API does no other network or file I/O on a request path. Narrow it if that ever changes.
DB_DOWN = (OSError, PoolTimeout)
log = logging.getLogger("raktdoot.api")


def database_unavailable(exc: Exception) -> bool:
    return isinstance(exc, DB_DOWN) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


class Problem(Exception):
    """A deliberate API error. `slug` becomes the stable `type`."""

    def __init__(self, status: int, slug: str, title: str, detail: str | None = None, headers: dict | None = None) -> None:
        self.status, self.slug, self.title, self.detail, self.headers = status, slug, title, detail, headers


def problem_response(
    status: int, type_: str, title: str, detail: str | None, request: Request, headers: dict | None = None, **extra
) -> JSONResponse:
    body = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body["instance"] = request.url.path         # path only: a query string can carry personal data
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def handle_problem(request: Request, exc: Problem):
        return problem_response(exc.status, TYPE_PREFIX + exc.slug, exc.title, exc.detail, request, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException):
        # Unknown routes (404), wrong method (405) and anything FastAPI itself raises land here.
        try:
            title = HTTPStatus(exc.status_code).phrase
        except ValueError:
            title = "Error"
        detail = str(exc.detail) if exc.detail and str(exc.detail) != title else None
        return problem_response(exc.status_code, "about:blank", title, detail, request, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        # Deliberately NOT echoing pydantic's `input`: it is the caller's raw data (emails, health details).
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return problem_response(
            422, TYPE_PREFIX + "validation-error", "Validation failed", "One or more fields are invalid.",
            request, errors=errors,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        return unexpected_response(request, exc)

    # The database-error classes get their OWN registration: handlers for specific classes run inside the CORS
    # middleware, so the browser can read the 503. The bare `Exception` handler above runs outside it (Starlette's
    # ServerErrorMiddleware), so its response has no CORS headers and a page would only see "network error".
    async def handle_database_error(request: Request, exc: Exception):
        if not database_unavailable(exc):        # e.g. an IntegrityError nobody expected: still a bug, and Starlette
            log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
        else:                                    # does not log what it handles here, so we do
            log.warning("database unavailable on %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return unexpected_response(request, exc)

    for cls in (OSError, PoolTimeout, DBAPIError):
        app.add_exception_handler(cls, handle_database_error)


def unexpected_response(request: Request, exc: Exception) -> JSONResponse:
    if database_unavailable(exc):               # an outage, not a bug: tell clients to retry, and when
        return problem_response(503, TYPE_PREFIX + "database-unavailable", "Service Unavailable",
                                "The service cannot reach its database right now. Try again shortly.", request,
                                headers={"Retry-After": "5"})
    # Never send exception text to the client: it leaks internals. The server still logs the traceback.
    # ponytail: no request id in the body yet; correlation ids arrive in 3.8.
    return problem_response(500, "about:blank", "Internal Server Error", None, request)
