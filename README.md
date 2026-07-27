# VibeMail SDK for Python

Official SDK for the [VibeMail](https://vibemail.ai) transactional email API. Python 3.9+, standard library only — installing it pulls in nothing else.

```bash
pip install vibemail
```

## Send an email

```python
import os
from vibemail import VibeMail

vibemail = VibeMail(os.environ["VIBEMAIL_API_KEY"])

result = vibemail.send(
    from_address="hello@yourdomain.com",
    to="ada@example.com",
    subject="Welcome",
    html="<p>Glad you're here.</p>",
    text="Glad you're here.",
)
print(result["id"])
```

`send()` returns once the API has accepted the message, not once it has been delivered. Use `get(id)` to follow it.

Always send a `text` alternative alongside `html`. Mail with no plain-text part is measurably more likely to be spam-foldered.

`from` is a reserved word in Python, so the argument is spelled `from_address`; it goes out as `from` on the wire.

## Recipients

One message goes to one recipient. To reach several people, use `batch()` — it sends a separate message to each, which is also what stops your recipients from seeing one another's addresses:

```python
result = vibemail.batch([
    {"to": "ada@example.com", "subject": "Welcome", "text": "Hi Ada"},
    {"to": "alan@example.com", "subject": "Welcome", "text": "Hi Alan"},
])

for entry in result["data"]:
    if "error" in entry:
        print("rejected:", entry["error"])
```

Each entry succeeds or fails on its own; one bad address does not sink the rest.

Passing several addresses to `to` raises `ValueError` rather than quietly sending to the first.

## Retries and idempotency

Pass `idempotency_key` on anything you might retry. A repeat carrying the same key returns the original result instead of sending a second copy — which matters because a client that times out has no way of knowing whether the message went out.

```python
vibemail.send(
    to="ada@example.com",
    subject="Receipt",
    text="Thanks!",
    idempotency_key=f"receipt-{order_id}",
)
```

The client retries `429` and `5xx` responses on its own (twice by default, honouring `Retry-After`) and never retries a request the server rejected on its merits. Pass `max_retries=0` to handle that yourself.

## Scheduling

```python
from datetime import datetime, timedelta, timezone

result = vibemail.send(
    to="ada@example.com",
    subject="Reminder",
    text="Standup in 15 minutes.",
    scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),   # or "in 2 hours"
)

vibemail.cancel(result["id"])   # while it is still scheduled
```

Up to 30 days ahead. A naive `datetime` is read as UTC rather than as the server's local time. Once the dispatcher has claimed a message it is on its way out and can no longer be withdrawn.

## Templates

```python
templates = vibemail.list_templates()

vibemail.send(
    to="ada@example.com",
    template="welcome",
    variables={"name": "Ada"},
)
```

Anything you pass explicitly — `subject`, `html`, `text` — overrides the stored template, so you can vary one part without redefining the rest.

## Errors

```python
from vibemail import VibeMailError, VibeMailTimeoutError

try:
    vibemail.send(to="ada@example.com", subject="Hi", text="...")
except VibeMailError as err:
    print(err.status, err.detail)      # 422 "'to' and 'subject' are required"
    if err.is_retryable:               # 429 or 5xx
        ...
except VibeMailTimeoutError:
    ...                                # exceeded `timeout`, default 30s
```

## Options

| Argument | Default | |
| --- | --- | --- |
| `api_key` | — | Required, positional. |
| `base_url` | `https://vibemail.ai` | Point at a self-hosted deployment. |
| `timeout` | `30.0` | Per-request, in seconds. |
| `max_retries` | `2` | Applies to `429` and `5xx` only. |

## API

| | |
| --- | --- |
| `send(**kwargs)` | Send one message. |
| `batch(emails, idempotency_key=None)` | Send many, one recipient each. |
| `get(email_id)` | Status of a send. |
| `cancel(email_id)` | Withdraw a scheduled send. |
| `list_templates()` | Stored templates. |

## License

MIT
