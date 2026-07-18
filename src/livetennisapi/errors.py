"""Exception hierarchy for the Live Tennis API.

Every error carries the HTTP status and the parsed body so callers can inspect
the raw response, but the common cases are distinguishable by type alone:

    try:
        analysis = client.get_match_analysis(match_id)
    except UpgradeRequired as exc:
        print(exc.required_tier)   # 'ULTRA'
    except RateLimited as exc:
        time.sleep(exc.retry_after or 60)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "LiveTennisAPIError",
    "APIStatusError",
    "BadRequest",
    "Unauthorized",
    "UpgradeRequired",
    "NotFound",
    "RateLimited",
    "ServerError",
    "ServiceUnavailable",
    "APIConnectionError",
    "APITimeoutError",
]


class LiveTennisAPIError(Exception):
    """Base class for every error raised by this library."""


class APIConnectionError(LiveTennisAPIError):
    """The request never produced a response (DNS, TLS, refused, dropped)."""


class APITimeoutError(APIConnectionError):
    """The request exceeded the configured timeout."""


class APIStatusError(LiveTennisAPIError):
    """The API returned a non-2xx response."""

    #: Populated by subclasses; used to pick the right class for a status.
    status_code: int = 0

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        request_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.headers = dict(headers or {})
        self.request_url = request_url

    @property
    def error_code(self) -> str | None:
        """The API's machine-readable code, e.g. ``upgrade_required``."""
        if isinstance(self.body, Mapping):
            code = self.body.get("error")
            if isinstance(code, str):
                return code
        return None

    def __str__(self) -> str:
        base = f"[{self.status_code}] {self.message}"
        if self.request_url:
            base = f"{base} ({self.request_url})"
        return base


class BadRequest(APIStatusError):
    """400 — a query parameter was malformed."""

    status_code = 400


class Unauthorized(APIStatusError):
    """401 — the key is missing, unknown, or disabled."""

    status_code = 401


class UpgradeRequired(APIStatusError):
    """403 — the endpoint exists but your tier does not unlock it.

    This is not an authentication failure. The key is valid; the plan is too
    low. ``required_tier`` is the lowest tier that unlocks the endpoint, which
    the library infers from the endpoint rather than the response body (the API
    returns only ``{"error": "upgrade_required"}``).
    """

    status_code = 403

    def __init__(self, *args: Any, required_tier: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.required_tier = required_tier

    def __str__(self) -> str:
        base = super().__str__()
        if self.required_tier:
            return (
                f"{base} — this endpoint requires the {self.required_tier} tier. See https://livetennisapi.com/#pricing"
            )
        return base


class NotFound(APIStatusError):
    """404 — no such resource, or no data for it yet."""

    status_code = 404


class RateLimited(APIStatusError):
    """429 — the tier's rate limit window was exceeded.

    ``retry_after`` is the number of seconds the API asked you to wait, parsed
    from the ``Retry-After`` header. It is ``None`` when the header is absent
    or unparseable.
    """

    status_code = 429

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after

    def __str__(self) -> str:
        base = super().__str__()
        if self.retry_after is not None:
            return f"{base} — retry after {self.retry_after:g}s"
        return base


class ServerError(APIStatusError):
    """5xx — the API failed to serve the request."""

    status_code = 500


class ServiceUnavailable(ServerError):
    """503 — the public surface is disabled or the service is down."""

    status_code = 503


#: Status code -> exception class. Anything unmapped falls back by class.
_STATUS_MAP: dict[int, type[APIStatusError]] = {
    400: BadRequest,
    401: Unauthorized,
    403: UpgradeRequired,
    404: NotFound,
    429: RateLimited,
    503: ServiceUnavailable,
}


def error_for_status(status_code: int) -> type[APIStatusError]:
    """Pick the exception class for a status code."""
    if status_code in _STATUS_MAP:
        return _STATUS_MAP[status_code]
    if status_code >= 500:
        return ServerError
    return APIStatusError
