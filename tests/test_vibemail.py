"""Unit tests driven through a stubbed transport, so the suite runs in CI
without credentials and can assert the exact bytes put on the wire."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from urllib.error import HTTPError, URLError

import pytest

from vibemail import VibeMail, VibeMailError, VibeMailTimeoutError, __version__


class Recorder(VibeMail):
    """A client whose single HTTP round trip is replaced by a queue of canned
    outcomes, recording what would have been sent."""

    def __init__(self, outcomes: Optional[List[Any]] = None, **kwargs: Any) -> None:
        kwargs.setdefault("max_retries", 0)
        super().__init__("vm_test", **kwargs)
        self.outcomes = list(outcomes or [{}])
        self.calls: List[dict] = []
        self.sleeps: List[int] = []

    def _open(self, req):  # type: ignore[override]
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": {k.lower(): v for k, v in req.headers.items()},
                "body": json.loads(req.data) if req.data else None,
            }
        )
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return 200, json.dumps(outcome)

    def _sleep_before_retry(self, attempt, retry_after):  # type: ignore[override]
        self.sleeps.append(attempt)  # never actually sleep in tests


def http_error(status: int, body: str = '{"error":"boom"}', retry_after: Optional[str] = None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return HTTPError("https://vibemail.ai/v1/emails", status, "err", headers, None)


class _Body:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode()

    def read(self) -> bytes:
        return self._payload


def http_error_with_body(status: int, body: str = '{"error":"boom"}', retry_after=None):
    err = http_error(status, body, retry_after)
    err.read = _Body(body).read  # type: ignore[method-assign]
    return err


# -- construction ---------------------------------------------------------


def test_api_key_is_required():
    with pytest.raises(ValueError, match="api_key is required"):
        VibeMail("")


def test_base_url_trailing_slash_is_stripped():
    assert VibeMail("k", base_url="http://localhost:8080/").base_url == "http://localhost:8080"


# -- send -----------------------------------------------------------------


def test_send_posts_with_auth_and_user_agent():
    c = Recorder([{"id": "e1", "status": "queued"}])
    assert c.send(to="a@example.com", subject="Hi", text="yo") == {"id": "e1", "status": "queued"}

    call = c.calls[0]
    assert call["url"] == "https://vibemail.ai/v1/emails"
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == "Bearer vm_test"
    assert call["headers"]["user-agent"] == f"vibemail-python/{__version__}"


def test_unset_fields_are_omitted():
    c = Recorder()
    c.send(to="a@example.com", subject="Hi")
    assert c.calls[0]["body"] == {"to": "a@example.com", "subject": "Hi"}


def test_from_address_maps_to_from():
    c = Recorder()
    c.send(to="a@example.com", subject="Hi", from_address="me@mine.com")
    assert c.calls[0]["body"]["from"] == "me@mine.com"


def test_single_element_sequence_is_accepted():
    c = Recorder()
    c.send(to=["solo@example.com"], subject="Hi")
    assert c.calls[0]["body"]["to"] == "solo@example.com"


def test_several_recipients_raise_instead_of_being_dropped():
    c = Recorder()
    with pytest.raises(ValueError, match="takes a single address, got 2"):
        c.send(to=["a@example.com", "b@example.com"], subject="Hi")
    assert c.calls == []


def test_empty_recipient_list_is_rejected():
    with pytest.raises(ValueError, match="must contain a recipient"):
        Recorder().send(to=[], subject="Hi")


def test_idempotency_key_travels_as_a_header():
    c = Recorder()
    c.send(to="a@example.com", subject="Hi", idempotency_key="key-1")
    assert c.calls[0]["headers"]["idempotency-key"] == "key-1"
    assert "idempotency_key" not in c.calls[0]["body"]


def test_false_tracking_flags_survive_compaction():
    """False is meaningful here and must not be dropped as though unset."""
    c = Recorder()
    c.send(to="a@example.com", subject="Hi", track_opens=False, track_clicks=False)
    assert c.calls[0]["body"]["track_opens"] is False
    assert c.calls[0]["body"]["track_clicks"] is False


# -- scheduling -----------------------------------------------------------


def test_aware_datetime_is_sent_as_utc_iso():
    c = Recorder()
    when = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    c.send(to="a@example.com", subject="Hi", scheduled_at=when)
    assert c.calls[0]["body"]["scheduled_at"] == "2030-01-01T12:00:00Z"


def test_naive_datetime_is_treated_as_utc():
    c = Recorder()
    c.send(to="a@example.com", subject="Hi", scheduled_at=datetime(2030, 1, 1, 12, 0))
    assert c.calls[0]["body"]["scheduled_at"] == "2030-01-01T12:00:00Z"


def test_offset_datetime_is_converted_not_relabelled():
    c = Recorder()
    tz = timezone(timedelta(hours=5))
    c.send(to="a@example.com", subject="Hi", scheduled_at=datetime(2030, 1, 1, 12, 0, tzinfo=tz))
    assert c.calls[0]["body"]["scheduled_at"] == "2030-01-01T07:00:00Z"


def test_relative_offset_passes_through():
    c = Recorder()
    c.send(to="a@example.com", subject="Hi", scheduled_at="in 2 hours")
    assert c.calls[0]["body"]["scheduled_at"] == "in 2 hours"


# -- errors ---------------------------------------------------------------


def test_error_carries_status_and_parsed_detail():
    c = Recorder([http_error_with_body(422, '{"error":"\'to\' and \'subject\' are required"}')])
    with pytest.raises(VibeMailError) as excinfo:
        c.send(to="a@example.com")
    assert excinfo.value.status == 422
    assert excinfo.value.detail == "'to' and 'subject' are required"
    assert excinfo.value.is_retryable is False


def test_non_json_error_body_does_not_break():
    c = Recorder([http_error_with_body(502, "<html>bad gateway</html>")])
    with pytest.raises(VibeMailError) as excinfo:
        c.send(to="a@example.com", subject="x")
    assert excinfo.value.status == 502
    assert excinfo.value.detail is None


@pytest.mark.parametrize(
    "status,retryable", [(429, True), (500, True), (503, True), (400, False), (422, False)]
)
def test_retryable_classification(status, retryable):
    assert VibeMailError(status, "{}").is_retryable is retryable


def test_timeout_surfaces_as_timeout_error():
    c = Recorder([URLError(TimeoutError("timed out"))])
    with pytest.raises(VibeMailTimeoutError):
        c.send(to="a@example.com", subject="x")


# -- retries --------------------------------------------------------------


def test_retries_a_503_then_succeeds():
    c = Recorder([http_error_with_body(503), {"id": "e9", "status": "queued"}], max_retries=2)
    assert c.send(to="a@example.com", subject="x")["id"] == "e9"
    assert len(c.calls) == 2


def test_does_not_retry_a_422():
    c = Recorder([http_error_with_body(422), {"id": "never"}], max_retries=3)
    with pytest.raises(VibeMailError):
        c.send(to="a@example.com", subject="x")
    assert len(c.calls) == 1


def test_gives_up_after_max_retries():
    c = Recorder([http_error_with_body(500) for _ in range(4)], max_retries=2)
    with pytest.raises(VibeMailError):
        c.send(to="a@example.com", subject="x")
    assert len(c.calls) == 3


def test_retry_reuses_the_same_idempotency_key():
    c = Recorder([http_error_with_body(500), {}], max_retries=1)
    c.send(to="a@example.com", subject="x", idempotency_key="stable")
    assert [call["headers"]["idempotency-key"] for call in c.calls] == ["stable", "stable"]


# -- batch, get, cancel, templates ----------------------------------------


def test_batch_wraps_messages_and_validates_each():
    c = Recorder([{"data": []}])
    c.batch([
        {"to": "a@example.com", "subject": "One"},
        {"to": "b@example.com", "subject": "Two"},
    ])
    assert c.calls[0]["url"] == "https://vibemail.ai/v1/emails/batch"
    assert c.calls[0]["body"]["emails"][1] == {"to": "b@example.com", "subject": "Two"}


def test_batch_rejects_a_multi_recipient_entry():
    c = Recorder()
    with pytest.raises(ValueError, match="takes a single address"):
        c.batch([{"to": ["a@example.com", "b@example.com"], "subject": "x"}])


def test_get_encodes_the_id():
    c = Recorder([{"id": "a/b"}])
    c.get("a/b")
    assert c.calls[0]["url"] == "https://vibemail.ai/v1/emails/a%2Fb"
    assert c.calls[0]["method"] == "GET"
    assert c.calls[0]["body"] is None


def test_cancel_posts_to_cancel_path():
    c = Recorder([{"id": "e1", "status": "canceled"}])
    assert c.cancel("e1")["status"] == "canceled"
    assert c.calls[0]["url"] == "https://vibemail.ai/v1/emails/e1/cancel"
    assert c.calls[0]["method"] == "POST"


def test_list_templates():
    c = Recorder([[{"id": 1, "name": "welcome"}]])
    assert c.list_templates()[0]["name"] == "welcome"
    assert c.calls[0]["url"] == "https://vibemail.ai/v1/templates"


def test_empty_body_returns_empty_dict():
    class Empty(Recorder):
        def _open(self, req):
            self.calls.append({"url": req.full_url, "method": req.get_method(),
                               "headers": {}, "body": None})
            return 200, ""

    assert Empty().cancel("e1") == {}
