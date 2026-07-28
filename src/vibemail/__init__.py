"""Official VibeMail SDK for Python, transactional email.

The surface here is limited to endpoints the API actually serves. Methods for
analytics and suppressions existed in an earlier draft and returned 404 against
every deployment; they are absent rather than broken, and will return when the
endpoints do.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

__all__ = [
    "VibeMail",
    "VibeMailError",
    "VibeMailTimeoutError",
    "__version__",
]

__version__ = "1.0.0"

DEFAULT_BASE_URL = "https://vibemail.ai"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2

Recipient = Union[str, Sequence[str]]
ScheduledAt = Union[str, datetime]


class VibeMailError(Exception):
    """An error response from the API."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        self.detail: Optional[str] = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                self.detail = parsed.get("error")
        except ValueError:
            pass
        super().__init__(f"VibeMail API {status}: {self.detail or body[:200]}")

    @property
    def is_retryable(self) -> bool:
        """True when retrying the same request could plausibly succeed."""
        return self.status == 429 or self.status >= 500


class VibeMailTimeoutError(Exception):
    """Raised when a request exceeds the configured timeout."""


def _one_recipient(to: Recipient) -> str:
    """The API delivers to a single recipient and derives per-domain rate
    limiting from it. Silently taking the first of several would drop the rest
    with no error anywhere, so several is an error."""
    if isinstance(to, str):
        return to
    addresses = list(to)
    if len(addresses) == 1:
        return addresses[0]
    if not addresses:
        raise ValueError("`to` must contain a recipient address.")
    raise ValueError(
        f"`to` takes a single address, got {len(addresses)}. Use batch() to send to "
        "several recipients, sending one message addressed to many would disclose "
        "the list to all of them."
    )


def _serialize_scheduled_at(value: Optional[ScheduledAt]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        # A naive datetime is ambiguous; assume the caller meant UTC rather than
        # sending a timestamp whose meaning depends on the server's clock.
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _compact(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop unset keys so the request carries only what the caller provided."""
    return {k: v for k, v in payload.items() if v is not None}


def _build_payload(
    *,
    to: Recipient,
    subject: Optional[str] = None,
    text: Optional[str] = None,
    html: Optional[str] = None,
    from_address: Optional[str] = None,
    template: Optional[str] = None,
    variables: Optional[Mapping[str, str]] = None,
    tags: Optional[Iterable[str]] = None,
    track_opens: Optional[bool] = None,
    track_clicks: Optional[bool] = None,
    scheduled_at: Optional[ScheduledAt] = None,
) -> Dict[str, Any]:
    return _compact(
        {
            "to": _one_recipient(to),
            "from": from_address,
            "subject": subject,
            "text": text,
            "html": html,
            "template": template,
            "variables": dict(variables) if variables is not None else None,
            "tags": list(tags) if tags is not None else None,
            "track_opens": track_opens,
            "track_clicks": track_clicks,
            "scheduled_at": _serialize_scheduled_at(scheduled_at),
        }
    )


class VibeMail:
    """Client for the VibeMail transactional email API.

    Uses only the standard library, so installing it pulls in nothing else.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    # -- transport ---------------------------------------------------------

    def _open(self, req: Request) -> tuple[int, str]:
        """One HTTP round trip. Separated so tests can drive the retry and
        error handling without a live socket."""
        with urlopen(req, timeout=self.timeout) as res:  # noqa: S310 - fixed https base
            return res.status, res.read().decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": f"vibemail-python/{__version__}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        last_error: Optional[VibeMailError] = None
        for attempt in range(self.max_retries + 1):
            req = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
            try:
                _status, raw = self._open(req)
                return json.loads(raw) if raw else {}
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = VibeMailError(exc.code, detail)
                if not last_error.is_retryable or attempt == self.max_retries:
                    raise last_error from exc
                self._sleep_before_retry(attempt, exc.headers.get("Retry-After"))
            except TimeoutError as exc:
                raise VibeMailTimeoutError(
                    f"VibeMail request timed out after {self.timeout}s"
                ) from exc
            except URLError as exc:
                # A DNS failure or refused connection is worth one more try, but
                # only within the caller's retry budget.
                if isinstance(exc.reason, TimeoutError):
                    raise VibeMailTimeoutError(
                        f"VibeMail request timed out after {self.timeout}s"
                    ) from exc
                if attempt == self.max_retries:
                    raise
                self._sleep_before_retry(attempt, None)

        assert last_error is not None
        raise last_error

    def _sleep_before_retry(self, attempt: int, retry_after: Optional[str]) -> None:
        """Honour Retry-After when the server sets it, otherwise back off
        exponentially. Jittered so a fleet of senders throttled at the same
        moment does not return in lockstep."""
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30.0))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(0.5 * (2**attempt), 8.0) * (1 + random.random() * 0.1))

    # -- emails ------------------------------------------------------------

    def send(
        self,
        *,
        to: Recipient,
        subject: Optional[str] = None,
        text: Optional[str] = None,
        html: Optional[str] = None,
        from_address: Optional[str] = None,
        template: Optional[str] = None,
        variables: Optional[Mapping[str, str]] = None,
        tags: Optional[Iterable[str]] = None,
        track_opens: Optional[bool] = None,
        track_clicks: Optional[bool] = None,
        scheduled_at: Optional[ScheduledAt] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send one message. Returns once accepted, not once delivered.

        Pass ``idempotency_key`` on anything you might retry: a repeat carrying
        the same key returns the original result rather than sending twice.
        """
        payload = _build_payload(
            to=to,
            subject=subject,
            text=text,
            html=html,
            from_address=from_address,
            template=template,
            variables=variables,
            tags=tags,
            track_opens=track_opens,
            track_clicks=track_clicks,
            scheduled_at=scheduled_at,
        )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return cast(Dict[str, Any], self._request("POST", "/v1/emails", payload, headers))

    def batch(
        self,
        emails: Sequence[Mapping[str, Any]],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send several messages in one request, one recipient each.

        Each mapping takes the same keyword arguments as :meth:`send`.
        """
        payload = {"emails": [_build_payload(**dict(e)) for e in emails]}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return cast(Dict[str, Any], self._request("POST", "/v1/emails/batch", payload, headers))

    def get(self, email_id: str) -> Dict[str, Any]:
        """Status of a send, by the id returned from :meth:`send`."""
        return cast(Dict[str, Any], self._request("GET", f"/v1/emails/{quote(email_id, safe='')}"))

    def cancel(self, email_id: str) -> Dict[str, Any]:
        """Withdraw a scheduled send, while it is still scheduled."""
        path = f"/v1/emails/{quote(email_id, safe='')}/cancel"
        return cast(Dict[str, Any], self._request("POST", path))

    # -- templates ---------------------------------------------------------

    def list_templates(self) -> List[Dict[str, Any]]:
        """Templates stored on the account."""
        return cast(List[Dict[str, Any]], self._request("GET", "/v1/templates"))
