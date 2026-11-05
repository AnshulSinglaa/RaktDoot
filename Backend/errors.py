"""One error format for the whole API: RFC 9457 problem details (it replaced RFC 7807; same media type).

Clients switch on `type` (stable, machine-readable), never on the human text in `title` or `detail`.
"""
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
TYPE_PREFIX = "urn:lifelink:problem:"       # ponytail: a URN, not a docs page. Upgrade path: URLs that resolve to docs.


class Problem(Exception):
    """A deliberate API error. `slug` becomes the stable `type`."""

    def __init__(self, status: int, slug: str, title: str, detail: str | None = None) -> None:
        self.status, self.slug, self.title, self.detail = status, slug, title, detail


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
        return problem_response(exc.status, TYPE_PREFIX + exc.slug, exc.title, exc.detail, request)

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
        # Never send exception text to the client: it leaks internals. The server still logs the traceback.
        # ponytail: no request id in the body yet; correlation ids arrive in 3.8.
        return problem_response(500, "about:blank", "Internal Server Error", None, request)
