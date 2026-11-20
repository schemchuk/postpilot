import imaplib
import re
import ssl
from collections.abc import Callable, Iterator
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from html import unescape

from postpilot.config import ImapSettings


class ImapError(RuntimeError):
    pass


def message_text(message: EmailMessage) -> str:
    part = message.get_body(("html", "plain"))
    if part is None:
        return ""
    try:
        body: str = part.get_content()
    except LookupError:
        payload = part.get_payload(decode=True)
        body = (
            payload.decode("utf-8", errors="replace")
            if isinstance(payload, bytes)
            else ""
        )
    if part.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(style|script)\b[^>]*>.*?</\1>", " ", body)
        body = re.sub(
            r"""(?i)<a\b[^>]*?\s+href\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)[^>]*>""",
            lambda match: " [link: " + match[1].strip("\"'") + "] ",
            body,
        )
        body = unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"[ \t]+", " ", body).strip()


def read_inbox(
    settings: ImapSettings,
    cursor: tuple[int, int] | None,
    factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
) -> Iterator[tuple[int, int, EmailMessage | None]]:
    try:
        with factory(
            settings.host, ssl_context=ssl.create_default_context(), timeout=20
        ) as imap:
            imap.login(settings.user, settings.password)
            if imap.select("INBOX", readonly=True)[0] != "OK":
                raise ImapError("Could not select IMAP inbox")
            validity = int(imap.response("UIDVALIDITY")[1][0])
            status, data = imap.uid("SEARCH", "FROM", '"linkedin.com"')
            if status != "OK":
                raise ImapError("IMAP search failed")
            uids = sorted({int(uid) for uid in (data[0] or b"").split()})
            if cursor is None or cursor[0] != validity:
                yield validity, max(uids, default=0), None
                return
            for uid in [uid for uid in uids if uid > cursor[1]][:20]:
                status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next(
                    (entry[1] for entry in data if isinstance(entry, tuple)), None
                )
                if status != "OK" or raw is None:
                    raise ImapError("IMAP fetch failed")
                message = BytesParser(policy=policy.default).parsebytes(raw)
                headers = message.get_all("From", [])
                addresses: tuple[Address, ...] = (
                    headers[0].addresses if len(headers) == 1 else ()
                )
                trusted_domain = (
                    len(addresses) == 1
                    and addresses[0].domain.lower() == "linkedin.com"
                )
                yield validity, uid, message if trusted_domain else None
    except (imaplib.IMAP4.error, OSError, ValueError, TypeError, IndexError):
        raise ImapError("IMAP read failed") from None
