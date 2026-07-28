# VibeMail SDK for Python

Official SDK for the [VibeMail](https://vibemail.ai) transactional email API. Python 3.9+, standard library only, installing it pulls in nothing else.

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

One message goes to one recipient. To reach several people, use `batch()`, it sends a separate message to each, which is also what stops your recipients from seeing one another's addresses:

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

Pass `idempotency_key` on anything you might retry. A repeat carrying the same key returns the original result instead of sending a second copy, which matters because a client that times out has no way of knowing whether the message went out.

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

Anything you pass explicitly - `subject`, `html`, `text`, overrides the stored template, so you can vary one part without redefining the rest.

## Domains

Mail sent from your own domain needs that domain added and verified. Adding one returns the DNS records to publish; it stays unverified until they resolve.

```python
domain = vibemail.create_domain("yourdomain.com")

for purpose, record in domain.get("dns_records", {}).items():
    print(purpose, record["type"], record["host"], record["value"])
# verification TXT _vibemail-verify.yourdomain.com vm-verify-...
# mx           MX  yourdomain.com                 mail.vibemail.ai
# spf          TXT yourdomain.com                 v=spf1 include:mail.vibemail.ai -all
# dmarc        TXT _dmarc.yourdomain.com          v=DMARC1; p=quarantine; ...
```

`dns_records` is keyed by purpose, not a list: `verification`, `mx`, `spf` and `dmarc`.
Each carries `type`, `host`, `value`, and `priority` where the type takes one. Publish all
four; the domain verifies once they resolve.

Come back for the same records at any time, along with whether verification has gone through:

```python
d = vibemail.get_domain(domain["id"])
print(d["is_verified"])
```

Listing is paged. `total` counts every domain on the account, not just the page you asked for:

```python
page = vibemail.list_domains(limit=25, offset=0)
print(page["total"], len(page["data"]))

vibemail.delete_domain(domain["id"])
```

Removing a domain does not affect mail already sent from it.

## Contacts

```python
vibemail.create_contact(
    "ada@example.com",
    name="Ada Lovelace",
    notes="met at the analytical engine demo",
)

page = vibemail.list_contacts(search="ada", limit=50)
vibemail.delete_contact(page["data"][0]["id"])
```

`search` matches against address and name.

## Suppressions

Addresses that will not be sent to: hard bounces, spam complaints, and anything blocked by hand. A send to a suppressed address is dropped rather than attempted, which is what keeps a bad list from taking your sending reputation with it.

```python
page = vibemail.list_suppressions(limit=100)

for entry in page["data"]:
    print(entry["email"], entry["reason"])   # "manual", or "hard bounce: ..."
```

`reason` is free text, not a fixed set: `manual` for anything added by hand or through the API, and `hard bounce: ...` carrying the remote server's own wording when the queue gave up on an address.

## Paging

`list_domains`, `list_contacts` and `list_suppressions` all return the same envelope:

```python
{"object": "list", "total": 128, "limit": 50, "offset": 0, "data": [...]}
```

`limit` defaults to 50 and is capped at 100. Walk the whole set by stepping `offset` until you have `total`:

```python
everything = []
offset = 0
while True:
    page = vibemail.list_contacts(limit=100, offset=offset)
    everything.extend(page["data"])
    if len(everything) >= page["total"]:
        break
    offset += 100
```

## Errors

```python
from vibemail import VibeMail, VibeMailError, VibeMailTimeoutError

try:
    vibemail.send(to="ada@example.com", subject="Hi", text="...")
except VibeMailError as err:
    print(err.status, err.detail)     # 422, "'to' and 'subject' are required"
    if err.is_retryable:              # 429 or 5xx
        ...
except VibeMailTimeoutError:
    ...                               # exceeded `timeout`, default 30s
```

| Status | Means |
| --- | --- |
| `400` | The request was malformed, such as a domain that is not a domain. |
| `401` | The API key is missing, wrong, or revoked. |
| `402` | A plan limit was reached. `err.detail` says which. Not retried. |
| `404` | No such record, or not yours. |
| `409` | Already exists, such as a domain someone has registered. |
| `422` | Required fields missing, or a schedule more than 30 days out. |
| `429` | Rate limited. Retried automatically, honouring `Retry-After`. |
| `5xx` | Our fault. Retried automatically. |

## Options

```python
vibemail = VibeMail(
    "vm_live_...",
    base_url="https://vibemail.ai",
    timeout=30.0,
    max_retries=2,
)
```

| Option | Default | |
| --- | --- | --- |
| `api_key` | — | Required, positional. |
| `base_url` | `https://vibemail.ai` | Point at a self-hosted deployment. |
| `timeout` | `30.0` | Per-request, in seconds. |
| `max_retries` | `2` | Applies to `429` and `5xx` only. |

## API

| | |
| --- | --- |
| `send(**fields)` | Send one message. |
| `batch(emails, idempotency_key=None)` | Send many, one recipient each. |
| `get(email_id)` | Status of a send. |
| `cancel(email_id)` | Withdraw a scheduled send. |
| `list_templates()` | Stored templates. |
| `list_domains(limit=None, offset=None)` | A page of sending domains. |
| `get_domain(domain_id)` | One domain, with the records that verify it. |
| `create_domain(domain)` | Add a domain. |
| `delete_domain(domain_id)` | Remove a domain. |
| `list_contacts(search=None, limit=None, offset=None)` | A page of contacts. |
| `create_contact(email, name=None, notes=None)` | Store a contact. |
| `delete_contact(contact_id)` | Forget a contact. |
| `list_suppressions(limit=None, offset=None)` | Addresses that will not be sent to. |

### Send fields

| Field | |
| --- | --- |
| `to` | Recipient. One per message; use `batch()` for many. |
| `from_address` | Sender. Must be an address your account owns. Defaults to the account address. |
| `subject` | |
| `text` | Plain-text body. Always send one alongside `html`. |
| `html` | HTML body. |
| `template` | Name or id of a stored template. |
| `variables` | Values substituted into the template. |
| `tags` | Labels carried through to analytics. |
| `track_opens` | Injects a tracking pixel into HTML sends. Defaults to on. |
| `track_clicks` | Rewrites links through the redirector. Defaults to on. |
| `scheduled_at` | ISO 8601, a `datetime`, or a relative offset like `"in 2 hours"`. Up to 30 days. |
| `idempotency_key` | A retry with the same key returns the original result. |

A naive `datetime` is treated as UTC. An aware one is converted, not relabelled.

## Not in the SDK yet

Webhooks and analytics are served by the API but are not wrapped here. They will be added when the shapes settle; until then reach them with `urllib` or `requests` and the same bearer token.

## License

MIT
