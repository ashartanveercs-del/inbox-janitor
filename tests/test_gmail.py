"""The Gmail adapter against a scripted HTTP transport: no network, no Google account."""

import base64
import email
import json
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from inbox_janitor.mailbox import MailboxError, NotFound, Outgoing
from inbox_janitor.mailbox.gmail import (
    MAX_HTML_CHARS,
    GmailMailbox,
    OAuthToken,
    html_to_text,
    parse_message,
)
from inbox_janitor.net import HttpResponse

API = "https://gmail.googleapis.com/gmail/v1/users/me/"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def ok(payload, status=200) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode())


class FakeGoogle:
    """Routes (method, path) to scripted responses; records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[dict] = []
        self.tokens_issued = 0

    def on(self, method: str, path: str, *responses) -> None:
        self.routes[(method, path)] = list(responses)

    def __call__(self, method, url, *, headers, body, timeout):
        parts = urlsplit(url)
        call = {"method": method, "url": url, "path": parts.path, "query": parse_qs(parts.query),
                "headers": dict(headers), "body": body}
        self.calls.append(call)
        if url == TOKEN_URI:
            self.tokens_issued += 1
            return ok({"access_token": f"access-{self.tokens_issued}", "expires_in": 3600})
        path = parts.path.removeprefix("/gmail/v1/users/me/")
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected request {method} {path}")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response

    def api_calls(self):
        return [c for c in self.calls if c["url"] != TOKEN_URI]


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def gmail(google: FakeGoogle, sleeps=None, clock=lambda: 1_000.0) -> GmailMailbox:
    token = OAuthToken(client_id="client-id.example", client_secret="not-a-real-secret",
                       refresh_token="not-a-real-refresh-token", http=google, clock=clock)
    return GmailMailbox(token, http=google, sleep=sleeps if sleeps is not None else Sleeps())


# -- auth and retries ----------------------------------------------------------------------

def test_the_access_token_is_refreshed_once_and_reused():
    google = FakeGoogle()
    google.on("GET", "profile", ok({"emailAddress": "Owner@HarborLane.example.com"}))
    box = gmail(google)
    assert box.address() == "owner@harborlane.example.com"
    box.address()
    assert google.tokens_issued == 1
    refresh = google.calls[0]
    assert parse_qs(refresh["body"].decode()) == {
        "grant_type": ["refresh_token"], "refresh_token": ["not-a-real-refresh-token"],
        "client_id": ["client-id.example"], "client_secret": ["not-a-real-secret"]}
    assert all(c["headers"]["Authorization"] == "Bearer access-1" for c in google.api_calls())


def test_a_401_gets_one_fresh_token():
    google = FakeGoogle()
    google.on("GET", "profile", HttpResponse(401, {}, b""), ok({"emailAddress": "o@example.com"}))
    assert gmail(google).address() == "o@example.com"
    assert google.tokens_issued == 2


def test_rate_limits_and_server_errors_are_retried_with_backoff():
    google, sleeps = FakeGoogle(), Sleeps()
    google.on("GET", "profile", HttpResponse(429, {}, b""), HttpResponse(503, {}, b""),
              ok({"emailAddress": "o@example.com"}))
    assert gmail(google, sleeps).address() == "o@example.com"
    assert sleeps == [1, 2]


def test_a_persistent_failure_raises_without_echoing_the_body():
    google = FakeGoogle()
    google.on("GET", "messages/m1", HttpResponse(500, {}, b'{"error": "Subject: Payroll 2026"}'))
    with pytest.raises(MailboxError) as err:
        gmail(google).fetch("m1")
    assert err.value.status == 500 and "Payroll" not in str(err.value)
    assert len(google.api_calls()) == 3


def test_a_network_failure_is_retried_then_reported():
    google = FakeGoogle()
    google.on("GET", "profile", OSError("connection reset"))
    with pytest.raises(MailboxError) as err:
        gmail(google).address()
    assert err.value.status is None


def test_a_missing_message_is_not_found():
    google = FakeGoogle()
    google.on("POST", "messages/gone/untrash", HttpResponse(404, {}, b""))
    with pytest.raises(NotFound):
        gmail(google).untrash("gone")


def test_a_refused_refresh_is_a_mailbox_error():
    class NoToken(FakeGoogle):
        def __call__(self, method, url, **kw):
            if url == TOKEN_URI:
                return HttpResponse(400, {}, b'{"error": "invalid_grant"}')
            return super().__call__(method, url, **kw)

    with pytest.raises(MailboxError, match="sign-in"):
        gmail(NoToken()).address()


# -- reading -------------------------------------------------------------------------------

def test_search_builds_the_query_and_follows_pages():
    google = FakeGoogle()
    google.on("GET", "messages", ok({"messages": [{"id": "a"}, {"id": "b"}],
                                     "nextPageToken": "p2"}),
              ok({"messages": [{"id": "c"}]}))
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert gmail(google).search("spam", since) == ["a", "b", "c"]
    first, second = google.api_calls()
    assert first["query"]["q"] == [f"in:spam after:{int(since.timestamp())}"]
    assert first["query"]["includeSpamTrash"] == ["true"]
    assert second["query"]["pageToken"] == ["p2"]


def test_inbox_search_leaves_spam_and_trash_out():
    google = FakeGoogle()
    google.on("GET", "messages", ok({}))
    assert gmail(google).search("inbox", datetime(2026, 10, 1, tzinfo=UTC)) == []
    assert "includeSpamTrash" not in google.api_calls()[0]["query"]


def full_message():
    return {
        "id": "18c2f0a1", "threadId": "18c2f0a1", "internalDate": "1760277600000",
        "labelIds": ["INBOX", "UNREAD", "Label_7"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Authentication-Results",
                 "value": "mx.google.com; dkim=pass header.d=flour.example.com"},
                {"name": "Authentication-Results",
                 "value": "forged.example.com; dmarc=pass header.from=flour.example.com"},
                {"name": "From", "value": "=?utf-8?q?Caf=C3=A9_Mill?= <Billing@Flour.example.com>"},
                {"name": "To", "value": "owner@harborlane.example.com"},
                {"name": "Subject", "value": "=?utf-8?q?Invoice_1043_=E2=80=93_September?="},
                {"name": "Message-ID", "value": "<inv-1043@flour.example.com>"},
                {"name": "In-Reply-To", "value": "<q-77@harborlane.example.com>"},
                {"name": "References", "value": "<q-76@harborlane.example.com> "
                                                "<q-77@harborlane.example.com>"},
                {"name": "List-Unsubscribe", "value": "<mailto:unsub@flour.example.com>"},
            ],
            "parts": [
                {"partId": "0", "mimeType": "multipart/alternative", "parts": [
                    {"partId": "0.0", "mimeType": "text/plain",
                     "body": {"size": 40, "data": b64("Invoice attached.\nDue in 30 days.")}},
                    {"partId": "0.1", "mimeType": "text/html",
                     "body": {"size": 60, "data": b64("<p>Invoice <b>attached</b></p>")}},
                ]},
                {"partId": "1", "mimeType": "application/pdf", "filename": "invoice-1043.pdf",
                 "body": {"attachmentId": "att1", "size": 48213}},
            ],
        },
    }


def test_fetch_turns_gmails_payload_into_a_message():
    google = FakeGoogle()
    google.on("GET", "messages/18c2f0a1", ok(full_message()))
    google.on("GET", "labels", ok({"labels": [{"id": "INBOX", "name": "INBOX"},
                                              {"id": "Label_7", "name": "Janitor/Invoices"}]}))
    m = gmail(google).fetch("18c2f0a1")
    assert google.api_calls()[0]["query"]["format"] == ["full"]
    assert (m.ref, m.sender, m.sender_name) == ("18c2f0a1", "billing@flour.example.com",
                                                "Café Mill")
    assert m.subject == f"Invoice 1043 {chr(0x2013)} September"     # RFC 2047, an en dash
    assert m.body == "Invoice attached.\nDue in 30 days."
    assert m.labels == {"INBOX", "UNREAD", "Janitor/Invoices"}
    assert m.received == datetime(2025, 10, 12, 14, 0, tzinfo=UTC)
    assert m.header("authentication-results").startswith("mx.google.com")   # topmost wins
    assert m.message_id == "<inv-1043@flour.example.com>"
    assert m.in_reply_to == "<q-77@harborlane.example.com>"
    assert m.references == ("<q-76@harborlane.example.com>", "<q-77@harborlane.example.com>")
    assert [(a.name, a.mime, a.size) for a in m.attachments] == [
        ("invoice-1043.pdf", "application/pdf", 48213)]


def test_an_html_only_body_is_reduced_to_text():
    google = FakeGoogle()
    data = full_message()
    data["payload"] = {"mimeType": "text/html",
                       "headers": [{"name": "From", "value": "a@b.example.com"}],
                       "body": {"data": b64("<style>p{}</style><script>x()</script>"
                                            "<p>Hello&nbsp;there</p><br>Bye")}}
    google.on("GET", "messages/18c2f0a1", ok(data))
    google.on("GET", "labels", ok({"labels": []}))
    body = gmail(google).fetch("18c2f0a1").body
    assert "x()" not in body and "p{}" not in body
    assert body.split() == ["Hello", "there", "Bye"]


@pytest.mark.parametrize(("markup", "text"), [
    ("<p>One</p><p>Two</p>", "One\n Two"),
    ("<SCRIPT type='x'>if (a > b) run()</SCRIPT>Shown", "Shown"),
    ("<style >p > a {}</style >Kept<script>x()</script>", "Kept"),
    ("a<br/>b<BR>c<br />d", "a\nb\nc\nd"),
    ("<div>x</div><li>y</li><tr>z</tr>", "x\n y\n z"),
    ("Tom &amp; Jerry &lt;3", "Tom & Jerry <3"),
    ("an <script>unclosed opener keeps its text", "an  unclosed opener keeps its text"),
    ("1 < 2, no tag closes here", "1 < 2, no tag closes here"),
    ('<a title="x>y">link</a>', 'y">link'),
])
def test_html_is_reduced_to_its_text(markup, text):
    assert html_to_text(markup) == text


@pytest.mark.parametrize("markup", ["<script" * 15_000, "<style>" * 15_000, "<" * 100_000,
                                    "<p" * 50_000, "<script>" + "<" * 100_000],
                         ids=["script-openers", "style-openers", "bare-lt", "open-tags",
                              "unclosed-script"])
def test_html_from_a_stranger_is_reduced_in_linear_time(markup):
    """Any sender can make the body HTML-only, and every inbox and spam message is read: a
    pattern that backtracks (thousands of unclosed openers) would hang the run."""
    started = time.perf_counter()
    html_to_text(markup)
    assert time.perf_counter() - started < 1.0


def test_only_the_start_of_a_huge_html_body_is_read():
    markup = "<p>" + "word " * 100_000 + "</p>"
    assert len(html_to_text(markup)) <= MAX_HTML_CHARS


@pytest.mark.parametrize("charset", ["undefined", "idna", "no-such-charset"])
def test_a_charset_that_cannot_decode_falls_back_to_utf8(charset):
    """The sender writes the charset. Some names Python knows still refuse to decode
    ('undefined' always raises, 'idna' refuses errors='replace'): that must never make the
    message unreadable, or one stranger's email would fail every run."""
    data = full_message()
    data["payload"] = {"mimeType": "text/plain",
                       "headers": [{"name": "From", "value": "a@b.example.com"},
                                   {"name": "Content-Type",
                                    "value": f'text/plain; charset="{charset}"'}],
                       "body": {"data": b64("Invoice attached.")}}
    assert parse_message(data, {}).body == "Invoice attached."


# -- writing -------------------------------------------------------------------------------

def test_modify_maps_label_names_and_creates_missing_ones_once():
    google = FakeGoogle()
    google.on("GET", "labels", ok({"labels": [{"id": "INBOX", "name": "INBOX"}]}))
    google.on("POST", "labels", ok({"id": "Label_9", "name": "Janitor/Receipts"}))
    google.on("POST", "messages/m1/modify", ok({}))
    google.on("POST", "messages/m2/modify", ok({}))
    box = gmail(google)
    box.modify("m1", add=["Janitor/Receipts"], remove=["INBOX", "Janitor/Never-Created"])
    box.modify("m2", add=["Janitor/Receipts"])
    created = [c for c in google.api_calls() if c["path"].endswith("/labels")
               and c["method"] == "POST"]
    assert len(created) == 1 and json.loads(created[0]["body"])["name"] == "Janitor/Receipts"
    first = json.loads(next(c for c in google.api_calls()
                            if c["path"].endswith("m1/modify"))["body"])
    assert first == {"addLabelIds": ["Label_9"], "removeLabelIds": ["INBOX"]}


def test_trash_and_untrash_post_to_their_endpoints():
    google = FakeGoogle()
    google.on("POST", "messages/m1/trash", ok({}))
    google.on("POST", "messages/m1/untrash", ok({}))
    box = gmail(google)
    box.trash("m1")
    box.untrash("m1")
    assert [c["path"].rsplit("/", 1)[-1] for c in google.api_calls()] == ["trash", "untrash"]


def test_send_uploads_a_raw_rfc822_message():
    google = FakeGoogle()
    google.on("POST", "messages/send", ok({"id": "sent-1", "threadId": "t1"}))
    out = Outgoing(sender="owner@harborlane.example.com", to="owner@harborlane.example.com",
                   subject="Inbox janitor (trial)", body="Nothing changed.",
                   message_id="<janitor.r1.abc@inbox-janitor.invalid>")
    assert gmail(google).send(out) == "sent-1"
    raw = json.loads(google.api_calls()[0]["body"])["raw"]
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert parsed["Message-ID"] == "<janitor.r1.abc@inbox-janitor.invalid>"
    assert parsed["Subject"] == "Inbox janitor (trial)"


# -- credentials ---------------------------------------------------------------------------

def test_credentials_load_from_an_authorized_user_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"type": "authorized_user", "client_id": "cid.example",
                                "client_secret": "s3cret-value", "refresh_token": "r3fresh"}),
                    encoding="utf-8")
    token = OAuthToken.from_file(path, http=FakeGoogle())
    assert "s3cret" not in repr(token) and "r3fresh" not in repr(token)


def test_incomplete_credentials_are_refused(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"client_id": "cid.example"}), encoding="utf-8")
    with pytest.raises(ValueError, match="refresh_token"):
        OAuthToken.from_file(path, http=FakeGoogle())


def test_the_access_token_is_renewed_before_it_expires():
    google, now = FakeGoogle(), [1_000.0]
    google.on("GET", "profile", ok({"emailAddress": "o@example.com"}))
    box = gmail(google, clock=lambda: now[0])
    box.address()
    now[0] += 3600 - 30                       # inside the last minute of its life
    box.address()
    assert google.tokens_issued == 2
