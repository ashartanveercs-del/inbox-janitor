"""Gmail over its REST API, with nothing but the standard library.

Scope: `https://www.googleapis.com/auth/gmail.modify` (read, label, trash, send - never
permanent delete). The OAuth refresh token is read from a file and never written; access
tokens live in memory. Errors carry the operation and the status code only: Gmail's error
bodies can quote message details, and errors end up in logs.
"""

from __future__ import annotations

import base64
import email.utils
import html
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from ..models import Attachment, Message
from ..net import HttpFn, urllib_send
from . import MailboxError, NotFound, Outgoing

API = "https://gmail.googleapis.com/gmail/v1/users/me/"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SYSTEM_LABELS = frozenset({"INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "SENT",
                           "DRAFT"})
BOX_QUERY = {"inbox": "in:inbox", "spam": "in:spam", "sent": "in:sent"}
ATTEMPTS = 3


class OAuthToken:
    """Refresh-token credentials of an installed OAuth app ('authorized user' JSON)."""

    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str,
                 token_uri: str = TOKEN_URI, http: HttpFn = urllib_send,
                 clock: Callable[[], float] = time.time) -> None:
        self._form = {"client_id": client_id, "client_secret": client_secret,
                      "refresh_token": refresh_token}
        self.token_uri = token_uri
        self.http = http
        self.clock = clock
        self._access: str | None = None
        self._expires = 0.0

    def __repr__(self) -> str:
        return "OAuthToken(<secrets hidden>)"

    @classmethod
    def from_file(cls, path: Path | str, *, http: HttpFn = urllib_send) -> OAuthToken:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        missing = [k for k in ("client_id", "client_secret", "refresh_token")
                   if not isinstance(data.get(k), str) or not data[k]]
        if missing:
            raise ValueError(f"{path} is missing {', '.join(missing)}")
        return cls(client_id=data["client_id"], client_secret=data["client_secret"],
                   refresh_token=data["refresh_token"],
                   token_uri=data.get("token_uri") or TOKEN_URI, http=http)

    def invalidate(self) -> None:
        self._access = None

    def access_token(self) -> str:
        if self._access and self.clock() < self._expires - 60:
            return self._access
        body = urlencode({**self._form, "grant_type": "refresh_token"}).encode()
        try:
            response = self.http("POST", self.token_uri, body=body, timeout=30, headers={
                "Content-Type": "application/x-www-form-urlencoded"})
        except OSError:
            raise MailboxError("Google sign-in") from None
        if response.status != 200:
            raise MailboxError("Google sign-in", response.status)
        try:
            data = json.loads(response.body)
            self._access = str(data["access_token"])
            self._expires = self.clock() + float(data.get("expires_in") or 3000)
        except (ValueError, KeyError, TypeError):
            raise MailboxError("Google sign-in") from None
        return self._access


class GmailMailbox:
    serv_id = "mx.google.com"         # the authserv-id Gmail's receiving servers stamp

    def __init__(self, token: OAuthToken, *, http: HttpFn = urllib_send,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 60.0) -> None:
        self.token = token
        self.http = http
        self.sleep = sleep
        self.timeout = timeout
        self._labels: dict[str, str] | None = None        # name -> id

    def __repr__(self) -> str:
        return "GmailMailbox()"

    # -- transport --------------------------------------------------------------------------

    def _call(self, method: str, path: str, operation: str, *,
              params: Mapping[str, Any] | None = None, body: Any = None) -> dict[str, Any]:
        """One 401 gets a fresh token; network errors, 429 and 5xx are retried with backoff;
        404 is NotFound; anything else is a MailboxError with the status only."""
        url = API + path + ("?" + urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        status: int | None = None
        for attempt in range(ATTEMPTS):
            headers = {"Authorization": "Bearer " + self.token.access_token()}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self.http(method, url, headers=headers, body=data,
                                     timeout=self.timeout)
            except OSError:
                status = None
            else:
                status = response.status
                if 200 <= status < 300:
                    return json.loads(response.body) if response.body.strip() else {}
                if status == 404:
                    raise NotFound(operation, 404)
                if status == 401 and attempt == 0:
                    self.token.invalidate()
                    continue
            transient = status is None or status == 429 or status >= 500
            if not transient:
                break
            if attempt < ATTEMPTS - 1:
                self.sleep(2 ** attempt)
        raise MailboxError(operation, status)

    # -- labels --------------------------------------------------------------------------------

    def _label_map(self) -> dict[str, str]:
        if self._labels is None:
            listed = self._call("GET", "labels", "Gmail labels").get("labels") or []
            self._labels = {str(lb.get("name")): str(lb.get("id")) for lb in listed}
        return self._labels

    def _label_id(self, name: str, create: bool) -> str | None:
        if name in SYSTEM_LABELS:
            return name
        known = self._label_map().get(name)
        if known or not create:
            return known
        made = self._call("POST", "labels", "Gmail label create", body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
        self._label_map()[name] = str(made["id"])
        return str(made["id"])

    # -- the Mailbox contract ---------------------------------------------------------------------

    def address(self) -> str:
        return str(self._call("GET", "profile", "Gmail profile").get("emailAddress", "")).lower()

    def search(self, box: str, since: datetime) -> list[str]:
        params: dict[str, Any] = {"q": f"{BOX_QUERY[box]} after:{int(since.timestamp())}",
                                  "maxResults": 500}
        if box == "spam":
            params["includeSpamTrash"] = "true"
        ids: list[str] = []
        while True:
            page = self._call("GET", "messages", "Gmail search", params=params)
            ids += [str(m["id"]) for m in page.get("messages") or []]
            if not page.get("nextPageToken"):
                return ids
            params = {**params, "pageToken": page["nextPageToken"]}

    def fetch(self, ref: str) -> Message:
        data = self._call("GET", f"messages/{quote(ref)}", "Gmail read",
                          params={"format": "full"})
        names = {v: k for k, v in self._label_map().items()}
        return parse_message(data, names)

    def modify(self, ref: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None:
        add_ids = [i for i in (self._label_id(n, create=True) for n in add) if i]
        remove_ids = [i for i in (self._label_id(n, create=False) for n in remove) if i]
        if add_ids or remove_ids:
            self._call("POST", f"messages/{quote(ref)}/modify", "Gmail label",
                       body={"addLabelIds": add_ids, "removeLabelIds": remove_ids})

    def trash(self, ref: str) -> None:
        self._call("POST", f"messages/{quote(ref)}/trash", "Gmail trash")

    def untrash(self, ref: str) -> None:
        self._call("POST", f"messages/{quote(ref)}/untrash", "Gmail untrash")

    def send(self, message: Outgoing) -> str:
        raw = base64.urlsafe_b64encode(message.to_bytes()).decode().rstrip("=")
        return str(self._call("POST", "messages/send", "Gmail send", body={"raw": raw})["id"])


# -- parsing Gmail's message resource ---------------------------------------------------------

def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError):
        return value


def _walk(part: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    children = part.get("parts")
    if children:
        for child in children:
            yield from _walk(child)
    else:
        yield part


def _text(part: Mapping[str, Any]) -> str:
    data = (part.get("body") or {}).get("data") or ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset = "utf-8"
    for h in part.get("headers") or []:
        if str(h.get("name", "")).lower() == "content-type":
            found = re.search(r'charset="?([\w.-]+)', str(h.get("value", "")), re.I)
            charset = found.group(1) if found else charset
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def parse_message(data: Mapping[str, Any], label_names: Mapping[str, str]) -> Message:
    payload = data.get("payload") or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers") or []:          # top first: the first occurrence wins
        headers.setdefault(str(h.get("name", "")).lower(), str(h.get("value", "")))
    name, address = email.utils.parseaddr(headers.get("from", ""))
    to = [a.lower() for _, a in email.utils.getaddresses(
        [headers.get("to", ""), headers.get("cc", "")]) if a]
    plain, rich, files = [], [], []
    for part in _walk(payload):
        mime = str(part.get("mimeType", "")).lower()
        if part.get("filename"):
            files.append(Attachment(str(part["filename"]), mime,
                                    int((part.get("body") or {}).get("size") or 0)))
        elif mime == "text/plain":
            plain.append(_text(part))
        elif mime == "text/html":
            rich.append(_text(part))
    body = "\n".join(plain) if plain else html_to_text("\n".join(rich))
    received = datetime.fromtimestamp(int(data.get("internalDate") or 0) / 1000, UTC)
    return Message(
        ref=str(data.get("id", "")), sender=address.lower(), sender_name=_decode(name),
        subject=_decode(headers.get("subject", "")), body=body, received=received,
        labels=frozenset(label_names.get(i, i) for i in data.get("labelIds") or []),
        to=tuple(to), message_id=headers.get("message-id", "").strip(),
        in_reply_to=headers.get("in-reply-to", "").strip(),
        references=tuple(headers.get("references", "").split()), headers=headers,
        attachments=tuple(files))
