import html
import itertools
import math
import re
import sqlite3
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from bridge.errors import UsageError
from bridge.pages import (
    ARTIFACT_SOURCE,
    find_unready_citations,
    is_page_path,
    map_unready_citations_to_error,
)

PIN_KINDS = ("cmd", "link", "note")
TITLE_MAX_CHARS = 200
BODY_MAX_CHARS = {"cmd": 4000, "link": 2000, "note": 1000}
SOURCE_MAX_CHARS = 80
POINTER_HINT = (
    "Pin a pointer instead: where it lives (--note \"1Password item 'Prod DB admin'\") "
    "or a command that reads it (--cmd 'op read op://Vault/Item/password')."
)

# Prefixes of issued credentials; each needs enough random tail after it to be a real one.
_KNOWN_SECRETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("an API key (sk-…)", re.compile(r"(?<![A-Za-z0-9-])sk-(?:[a-z]+-)?(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("a Slack app token", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("a PEM key or certificate", re.compile(r"-----BEGIN ")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("an npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}")),
    ("a Linear API key", re.compile(r"\blin_(?:api|oauth)_[A-Za-z0-9]{20,}")),
    ("a Stripe key", re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.")),
    ("a bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}")),
    ("a Basic authorization header", re.compile(r"(?i)\bauthorization:\s*basic\s+[A-Za-z0-9+/]{8,}")),
    (
        "a webhook URL",
        re.compile(
            r"(?i)\b(?:hooks\.slack\.com/(?:services|workflows|triggers)|discord(?:app)?\.com/api/(?:v\d+/)?webhooks)/\S"
        ),
    ),
)
_QUOTED_OR_WORD = r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"'|;&]+)"
_AT_COMMAND = r"(?:^|[;&|(]\s*|\bsudo\s+|\bexec\s+)"
# Short flags that take a password only in these programs; elsewhere -u is a user and -p a port.
_PASSWORD_FLAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a user:password for curl",
        re.compile(rf"(?m){_AT_COMMAND}curl\b[^|;&\n]*?\s(?:-u\s*|--user[=\s]\s*){_QUOTED_OR_WORD}"),
    ),
    (
        "a password after mysql -p",
        re.compile(rf"(?m){_AT_COMMAND}(?:mysql|mariadb)\w*\b[^|;&\n]*?\s-p{_QUOTED_OR_WORD}"),
    ),
    ("a password for sshpass", re.compile(rf"(?m){_AT_COMMAND}sshpass\s+-p\s*{_QUOTED_OR_WORD}")),
    (
        "a password for docker login",
        re.compile(rf"(?m)\b(?:docker|podman|nerdctl|helm\s+registry)\s+login\b[^|;&\n]*?\s-p\s+{_QUOTED_OR_WORD}"),
    ),
)
_SECRET_WORD = (
    r"(?:password|passwd|pwd|passphrase|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)"
)
# Named for the secret it holds (DB_PASSWORD, --client-secret), not for a pointer to one (--secret-id, TOKEN_FILE).
_SECRET_NAME = rf"[A-Za-z0-9_.-]*?{_SECRET_WORD}"
# Names that end like a secret but hold a reference or an id: Helm's existingSecret, AWS's --client-token.
_NOT_A_SECRET_NAME = re.compile(r"(?i).*existing\w*|.*(?:client|page|next|starting|continuation|idempotency)[_-]?token")
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s&;,})\]]+)"
_NOT_IN_A_NAME = r"(?<![A-Za-z0-9_.-])"
_ASSIGNED = re.compile(rf"(?i){_NOT_IN_A_NAME}({_SECRET_NAME})\s*=\s*{_VALUE}")
_QUOTED_KEY = re.compile(rf"(?i)[\"']({_SECRET_NAME})[\"']\s*:\s*{_VALUE}")
_FLAG_VALUE = re.compile(rf"(?i)(?<![A-Za-z0-9-])--({_SECRET_NAME})(?:\s+|=)(\"[^\"]*\"|'[^']*'|[^\s-]\S*)")
_COLON_KEY = re.compile(rf"(?i){_NOT_IN_A_NAME}({_SECRET_NAME}):([ \t]*){_VALUE}")
_POINTER_WORDS = re.compile(
    r"(?i)\b(?:1password|op://|vault|keychain|bitwarden|lastpass|keeper|secrets? ?manager|parameter store|ssm|doppler"
    r"|item|ask|lives|stored|see)\b"
)
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_URL_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]*:([^/\s@]+)@")
# Signed URLs carry their grant in these; a document id in ?id= grants nothing by itself.
_SIGNED_QUERY_KEYS = frozenset(
    ("sig", "signature", "x-amz-signature", "x-amz-security-token", "x-goog-signature", "x-goog-credential")
)
_ID_QUERY_KEYS = frozenset(("v", "list", "q", "ref", "sha", "commit"))
# Values of --*-id, *_name and the like are public ids, however random they look.
_ID_VALUE = re.compile(r"(?i)(?<![A-Za-z0-9])-{0,2}[A-Za-z0-9_.-]*(?:id|ids|arn|name|ref|sha|digest|hash)(?:=|\s+)\S+")
_RANDOM_RUN = re.compile(r"[A-Za-z0-9+=]{24,}")
_RANDOM_RUN_WITH_SEPARATORS = re.compile(r"[A-Za-z0-9+=_/-]{32,}")
_HEX = re.compile(r"[0-9a-f]+|[0-9A-F]+")
RANDOM_MIN_ENTROPY_BITS = 4.0
# Random base62 changes character class (lower, upper, digit) other than upper to lower at ~44% of steps, random
# lowercase base36 at ~40%, CamelCase words at ~17%.
RANDOM_MIN_CLASS_SWITCHES = 0.25


@dataclass(frozen=True)
class Pin:
    id: int
    title: str
    kind: str
    body: str
    source: str | None
    pinned_by: str
    created_at: int
    archived_at: int | None
    archived_by: str | None
    updated_at: int | None = None

    @property
    def shown_at(self) -> int:
        return max(self.created_at, self.updated_at or 0)


class PinError(UsageError):
    pass


def find_secret(text: str) -> str | None:
    """What in the text looks like a secret, or None; pins hold pointers to secrets, never the secrets."""
    found = _find_secret_as_written(text)
    decoded = urllib.parse.unquote(text)
    return found if found is not None or decoded == text else _find_secret_as_written(decoded)


def _find_secret_as_written(text: str) -> str | None:
    for what, pattern in _KNOWN_SECRETS:
        if pattern.search(text):
            return what
    for what, pattern in _PASSWORD_FLAGS:
        for match in pattern.finditer(text):
            value = match.group("value")
            password = value.split(":", 1)[1] if what.endswith("curl") and ":" in value else value
            if (":" in value or not what.endswith("curl")) and not _is_reference(password):
                return what
    for match in _URL_USERINFO.finditer(text):
        if not _is_reference(match.group(1)):
            return "a URL with user:password@"
    for pattern in (_ASSIGNED, _QUOTED_KEY, _FLAG_VALUE):
        for match in pattern.finditer(text):
            name, value = match.group(1), match.group(2)
            opening = text[match.start(1) - 1 : match.start(1)]
            if opening in ("'", '"') and value.startswith(opening) and pattern is _ASSIGNED:
                continue  # 'api_key=' inside a quoted pattern: the "value" is the closing quote.
            if not _NOT_A_SECRET_NAME.fullmatch(name) and not _is_reference(value):
                return f"a value given for {name}"
    what = _find_secret_after_colon(text)
    if what is not None:
        return what
    for url in _URL.finditer(text):
        try:
            query = urllib.parse.urlsplit(url.group(0)).query
        except ValueError:
            continue
        for key, value in urllib.parse.parse_qsl(query):
            k = key.lower()
            if k in _SIGNED_QUERY_KEYS or (
                k not in _ID_QUERY_KEYS and not k.endswith(("id", "ids")) and _looks_random(value)
            ):
                return f"a credential in a URL's {key}="
    # A link's path holds document ids (Google Docs, artifacts) that look random but grant nothing by themselves.
    outside_urls = " ".join(_URL.split(_ID_VALUE.sub(" ", text)))
    return "a long random-looking string" if _looks_random(outside_urls) else None


def _find_secret_after_colon(text: str) -> str | None:
    """YAML-style 'name: value'. Prose reads the same ("DB password: 1Password item X"), so a value that names
    where the secret lives passes, and so do several unquoted words after a plain-word name."""
    for match in _COLON_KEY.finditer(text):
        name, gap, value = match.group(1), match.group(2), match.group(3)
        compound = any(c in name for c in "_.-")
        if not gap and (compound or value.startswith((".", "/", "{", "[", ":"))):
            continue  # A refspec (fix-token:main) or a JSONPath (TOKEN:.data.token).
        rest = text[match.end() :].split("\n", 1)[0]
        if _NOT_A_SECRET_NAME.fullmatch(name) or _is_reference(value) or _POINTER_WORDS.search(value + rest):
            continue
        quoted = value.startswith(("'", '"'))
        lone = re.fullmatch(r"[ \t]*(?:#.*)?|[ \t]*[,}\]].*", rest) is not None
        if quoted or lone or compound:
            return f"a value given for {name}"
    return None


def _is_reference(value: str) -> bool:
    if value.startswith("'"):
        # Single quotes keep $ literal, so '$x' is the password itself.
        bare = value.strip("'")
        variable = False
    else:
        bare = value.strip('"')
        variable = bare.startswith("$") and not bare.startswith("$'")
    return (
        variable
        or not bare
        or bare.startswith(("<", "{{", "op://"))
        or re.fullmatch(r"\.{1,2}/\S+|~/\S+|/[^/\s]+/\S+", bare) is not None
        or set(bare) <= set("*x.-_X")
        or bare.lower() in ("none", "null", "nil", "true", "false", "redacted", "changeme", "...", "…")
    )


def _looks_random(text: str) -> bool:
    for run in _RANDOM_RUN.findall(text):
        if _HEX.fullmatch(run) or run.isdigit() or not _switches_class_often(run):
            continue
        if _measure_entropy_bits(run) >= RANDOM_MIN_ENTROPY_BITS:
            return True
    for run in _RANDOM_RUN_WITH_SEPARATORS.findall(text):
        bare = re.sub(r"[_/+=-]", "", run)
        mixed = any(c.islower() for c in bare) and any(c.isupper() for c in bare) and any(c.isdigit() for c in bare)
        if mixed and _switches_class_often(bare) and _measure_entropy_bits(bare) >= RANDOM_MIN_ENTROPY_BITS:
            return True
    return False


def _switches_class_often(text: str) -> bool:
    classes = [0 if c.islower() else 1 if c.isupper() else 2 for c in text]
    # Upper to lower is how every CamelCase word starts, so it does not count.
    switches = sum(a != b and (a, b) != (1, 0) for a, b in itertools.pairwise(classes))
    return switches >= RANDOM_MIN_CLASS_SWITCHES * (len(text) - 1)


def _measure_entropy_bits(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def check_pin_fields(title: str, kind: str, body: str, source: str | None) -> tuple[str, str, str | None]:
    """The fields cleaned; PinError names what is wrong or what looks like a secret."""
    title = " ".join(title.split())
    body = body.strip()
    source = " ".join(source.split()) if source else None
    if not title:
        raise PinError("a pin needs a title")
    if len(title) > TITLE_MAX_CHARS:
        raise PinError(f"title is {len(title)} chars; keep it under {TITLE_MAX_CHARS}")
    if kind not in PIN_KINDS:
        raise PinError(f"kind is one of {', '.join(PIN_KINDS)}")
    if not body:
        raise PinError(f"--{kind} is empty")
    if len(body) > BODY_MAX_CHARS[kind]:
        raise PinError(f"--{kind} is {len(body)} chars; keep it under {BODY_MAX_CHARS[kind]}")
    if source is not None and len(source) > SOURCE_MAX_CHARS:
        raise PinError(f"source is {len(source)} chars; keep it under {SOURCE_MAX_CHARS}")
    # Before any message that quotes the input, so a refusal never repeats the secret.
    # A /p/<slug> path is only lowercase letters, digits and dashes, which name a page and grant nothing.
    scanned_body = "" if kind == "link" and is_page_path(body) else body
    for field, value in (("title", title), (f"--{kind}", scanned_body), ("source", source or "")):
        what = find_secret(value)
        if what is not None:
            raise PinError(f"refused: {field} holds what looks like {what}. {POINTER_HINT}")
    if kind == "link" and not (is_web_url(body) or is_page_path(body)):
        raise PinError(f"a link is an http(s) URL with no spaces, or a bridge page path /p/<slug>: {body[:80]!r}")
    return title, body, source


def is_web_url(text: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc) and not any(c.isspace() for c in text)


def add_pin(
    conn: sqlite3.Connection, title: str, kind: str, body: str, source: str | None, pinned_by: str, now: int
) -> Pin:
    title, body, source = check_pin_fields(title, kind, body, source)
    if unready := find_unready_citations(conn, f"{title}\n{body}"):
        raise PinError(map_unready_citations_to_error(unready))
    cursor = conn.execute(
        "INSERT INTO pins (title, kind, body, source, pinned_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (title, kind, body, source, pinned_by, now),
    )
    return get_pin(conn, int(cursor.lastrowid or 0))


def pin_link_once(conn: sqlite3.Connection, title: str, url: str, source: str, pinned_by: str, now: int) -> Pin | None:
    """A link already pinned, or held by a registered page, keeps that one pin: None then."""
    if conn.execute("SELECT 1 FROM pages WHERE artifact_url = ? AND archived_at IS NULL", (url,)).fetchone():
        return None
    if conn.execute("SELECT 1 FROM pins WHERE kind = 'link' AND body = ? AND archived_at IS NULL", (url,)).fetchone():
        return None
    return add_pin(conn, title[:TITLE_MAX_CHARS], "link", url, source, pinned_by, now)


def record_artifact_publish(conn: sqlite3.Connection, url: str, title: str, by: str, at: int) -> Pin | None:
    """A republish moves the existing pin of that URL, never adds a second; None when a registered page holds the URL,
    since /pinned shows it as that page, moved to the publish time. PinError when the title looks like a secret."""
    if conn.execute("SELECT 1 FROM pages WHERE artifact_url = ?", (url,)).fetchone() is not None:
        conn.execute(
            "UPDATE pages SET updated_at = max(updated_at, ?) WHERE artifact_url = ? AND archived_at IS NULL", (at, url)
        )
        return None
    title, url, _ = check_pin_fields(" ".join(title.split())[:TITLE_MAX_CHARS] or url, "link", url, ARTIFACT_SOURCE)
    # A person's pin of the same link keeps its own title and pin time; backfill can arrive older than what is stored.
    conn.execute(
        """
        UPDATE pins SET
          title = CASE WHEN source IS :source AND :at >= coalesce(updated_at, created_at) THEN :title ELSE title END,
          created_at = CASE WHEN source IS :source THEN min(created_at, :at) ELSE created_at END,
          updated_at = max(coalesce(updated_at, created_at), :at)
        WHERE kind = 'link' AND body = :url
        """,
        {"source": ARTIFACT_SOURCE, "at": at, "title": title, "url": url},
    )
    row = conn.execute(
        "SELECT * FROM pins WHERE kind = 'link' AND body = ? ORDER BY archived_at IS NULL DESC, id DESC", (url,)
    ).fetchone()
    if row is not None:
        return _map_row_to_pin(row)
    cursor = conn.execute(
        """
        INSERT INTO pins (title, kind, body, source, pinned_by, created_at, updated_at)
        VALUES (?, 'link', ?, ?, ?, ?, ?)
        """,
        (title, url, ARTIFACT_SOURCE, by, at, at),
    )
    return get_pin(conn, int(cursor.lastrowid or 0))


def get_pin(conn: sqlite3.Connection, pin_id: int) -> Pin:
    row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
    if row is None:
        raise PinError(f"no pin #{pin_id}")
    return _map_row_to_pin(row)


def archive_pin(conn: sqlite3.Connection, pin_id: int, by: str, now: int) -> Pin:
    pin = get_pin(conn, pin_id)
    if pin.archived_at is not None:
        raise PinError(f"pin #{pin_id} is already unpinned (by {pin.archived_by})")
    conn.execute("UPDATE pins SET archived_at = ?, archived_by = ? WHERE id = ?", (now, by, pin_id))
    return get_pin(conn, pin_id)


def list_pins(conn: sqlite3.Connection, include_archived: bool = False) -> list[Pin]:
    """Newest first."""
    where = "" if include_archived else "WHERE archived_at IS NULL"
    rows = conn.execute(
        f"SELECT * FROM pins {where} ORDER BY max(created_at, coalesce(updated_at, 0)) DESC, id DESC"
    ).fetchall()
    return [_map_row_to_pin(row) for row in rows]


def _map_row_to_pin(row: sqlite3.Row) -> Pin:
    return Pin(
        id=int(row["id"]),
        title=str(row["title"]),
        kind=str(row["kind"]),
        body=str(row["body"]),
        source=row["source"],
        pinned_by=str(row["pinned_by"]),
        created_at=int(row["created_at"]),
        archived_at=row["archived_at"],
        archived_by=row["archived_by"],
        updated_at=row["updated_at"],
    )


def map_pin_to_html(pin: Pin) -> str:
    e = html.escape
    if pin.kind == "cmd":
        body = (
            f'<div class="cmd"><pre><code>{e(pin.body)}</code></pre>'
            '<button type="button" class="copy" data-copy>Copy</button></div>'
        )
    elif pin.kind == "link" and is_page_path(pin.body):
        body = f'<a class="link" href="{e(pin.body)}">{e(pin.body)}</a>'
    elif pin.kind == "link" and is_web_url(pin.body):
        shown = re.sub(r"^https?://", "", pin.body)
        body = f'<a class="link" href="{e(pin.body)}" target="_blank" rel="noopener noreferrer">{e(shown)}</a>'
    else:
        body = f'<p class="note">{e(pin.body)}</p>'
    published = pin.source == ARTIFACT_SOURCE
    meta = [] if published or not pin.source else [e(pin.source)]
    meta.append(f"{'published' if published else 'pinned'} by {e(pin.pinned_by)}")
    moved = pin.shown_at > pin.created_at
    meta.append(f"{'published ' if moved and not published else ''}{map_ms_to_clock_html(pin.shown_at)}")
    if moved:
        same_day = map_ms_to_day(pin.created_at) == map_ms_to_day(pin.shown_at)
        first = map_ms_to_clock_html(pin.created_at) if same_day else map_ms_to_time_html(pin.created_at)
        meta.append(f"{'first published' if published else 'pinned'} {first}")
    return (
        f'<article class="pin" id="pin-{pin.id}" data-day="{e(map_ms_to_day(pin.shown_at))}">'
        f"<h2>{e(pin.title)}</h2>{body}"
        f'<div class="meta"><span class="id">#{pin.id}</span> · {" · ".join(meta)}</div></article>'
    )


def map_ms_to_time_html(ms: int) -> str:
    at = datetime.fromtimestamp(ms / 1000)
    return f'<time datetime="{at.isoformat(timespec="seconds")}">{html.escape(at.strftime("%b %-d, %H:%M"))}</time>'


def map_ms_to_clock_html(ms: int) -> str:
    at = datetime.fromtimestamp(ms / 1000)
    return f'<time datetime="{at.isoformat(timespec="seconds")}">{at.strftime("%H:%M")}</time>'


def map_ms_to_day(ms: int) -> str:
    """The heading /pinned groups items under, in this machine's time zone."""
    at = datetime.fromtimestamp(ms / 1000)
    day = at.strftime("%A, %b %-d")
    return day if at.year == datetime.now().year else f"{day}, {at.year}"
