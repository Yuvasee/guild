import re
from collections.abc import Mapping

MAX_SCAN_CHARS = 20_000
MAX_REFS_PER_TEXT = 20

_PR_URL_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
_GH_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
_REPO_NUM_RE = re.compile(r"(?<![\w/.-])([\w.-]+)/([\w.-]+)#(\d+)\b")
_BARE_NUM_RE = re.compile(r"(?:\bPR\s*#?(\d{1,6})|(?<![\w&/#])#(\d{2,6}))\b", re.IGNORECASE)
_GENERIC_ISSUE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
_NOT_ISSUE_PREFIXES = frozenset(
    {"UTF", "SHA", "ISO", "HTTP", "RFC", "CVE", "GPT", "TLS", "SSL", "AES", "RSA", "P", "X", "COVID", "MD5"}
)


def extract_refs(
    text: str,
    *,
    issue_prefixes: tuple[str, ...],
    default_repo: str | None,
) -> list[str]:
    scanned = text[:MAX_SCAN_CHARS]
    found: dict[str, None] = {}
    for owner, repo, number in _PR_URL_RE.findall(scanned):
        found[f"pr:{owner}/{repo}#{number}"] = None
    for owner, repo, number in _GH_ISSUE_URL_RE.findall(scanned):
        found[f"gh-issue:{owner}/{repo}#{number}"] = None
    for owner, repo, number in _REPO_NUM_RE.findall(scanned):
        found[f"pr:{owner}/{repo}#{number}"] = None
    if default_repo:
        for explicit, bare in _BARE_NUM_RE.findall(scanned):
            found[f"pr:{default_repo}#{explicit or bare}"] = None
    for issue in extract_issue_keys(scanned, issue_prefixes):
        found[f"issue:{issue}"] = None
    return list(found)[:MAX_REFS_PER_TEXT]


def map_text_to_ref(text: str, *, issue_prefixes: tuple[str, ...], default_repo: str | None) -> str | None:
    """One whole argument as a PR or issue ref; None rather than a guess, so a typo is refused and not dropped."""
    value = text.strip()
    if re.fullmatch(r"(pr|gh-issue):[\w.-]+/[\w.-]+#\d+|issue:[A-Za-z][A-Za-z0-9]*-\d+", value):
        return value
    if match := re.fullmatch(r"(?:https?://)?github\.com/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)[/\w?=&#.-]*", value):
        owner, repo, kind, number = match.groups()
        return f"{'pr' if kind == 'pull' else 'gh-issue'}:{owner}/{repo}#{number}"
    if match := re.fullmatch(r"([\w.-]+)/([\w.-]+)#(\d+)", value):
        return f"pr:{match.group(1)}/{match.group(2)}#{match.group(3)}"
    if (match := re.fullmatch(r"#(\d+)", value)) and default_repo:
        return f"pr:{default_repo}#{match.group(1)}"
    # A tracker's issue URL: /issue/KEY (Linear and most), /browse/KEY (Jira), /issues/KEY.
    key = re.search(r"https?://\S+?/(?:issues?|browse)/([A-Za-z][A-Za-z0-9]*-\d+)", value)
    keys = extract_issue_keys(key.group(1) if key else value, issue_prefixes)
    return f"issue:{keys[0]}" if keys and (key or value.upper() == keys[0]) else None


def extract_issue_keys(text: str, issue_prefixes: tuple[str, ...]) -> list[str]:
    if issue_prefixes:
        pattern = re.compile(rf"\b({'|'.join(map(re.escape, issue_prefixes))})-(\d+)\b", re.IGNORECASE)
        return [f"{prefix.upper()}-{number}" for prefix, number in pattern.findall(text)]
    return [
        f"{prefix}-{number}" for prefix, number in _GENERIC_ISSUE_RE.findall(text) if prefix not in _NOT_ISSUE_PREFIXES
    ]


def extract_branch_refs(branch: str, issue_prefixes: tuple[str, ...]) -> list[str]:
    if not issue_prefixes:
        return []
    return [f"issue:{key}" for key in extract_issue_keys(branch, issue_prefixes)]


def map_repo_to_label(repo: str, overrides: Mapping[str, str]) -> str:
    """A four-letter repo tag for tight UI: the repo name without the org's own prefix (Acme-Org/acme-infra → infr)."""
    if repo in overrides:
        return overrides[repo]
    org, _, name = repo.partition("/")
    org_word = org.lower().split("-")[0]
    if name.lower().startswith(org_word + "-") and len(name) > len(org_word) + 1:
        name = name[len(org_word) + 1 :]
    return re.sub(r"[^a-z0-9]", "", name.lower())[:4] or name[:4]


def map_ref_to_label(ref: str, overrides: Mapping[str, str]) -> str:
    """pr:Acme-Org/acme-infra#21 → infr#21; issue keys stay as they are."""
    kind, _, value = ref.partition(":")
    repo, sep, number = value.rpartition("#")
    if kind in ("pr", "gh-issue") and sep and "/" in repo:
        return f"{map_repo_to_label(repo, overrides)}#{number}"
    return value
