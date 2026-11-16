import re

from bridge.review_marks import MD_HIDDEN_RE

_PR_RE = re.compile(
    r"https?://github\.com/(?P<url_owner>[\w.-]+)/(?P<url_repo>[\w.-]+)/pull/(?P<url_number>\d+)"
    r"|(?<![\w/.&-])(?:(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+))?#(?P<number>\d+)\b"
)
# Within a sentence: a dot inside a URL or a file name does not end it.
_IN_SENTENCE = r"(?:[^.!?\n]|[.!?](?=\S))"
_TRIGGER_RE = re.compile(
    r"\bonly\s+after\b"
    r"|(?P<verb>\b(?:merge|merges|merging|ship|ships|shipping|land|lands|landing|release|releasing|deploy|deploying)\b"
    rf"{_IN_SENTENCE}{{0,80}}?\b(?:after|until)\b)"
    r"|\bmerge\s+\S+\s+first\b"
    rf"|\bafter\b(?={_IN_SENTENCE}{{0,120}}(?:\b(?:is|are|gets?|has\s+been|have\s+been)\s+"
    r"(?:merged|released|shipped|deployed|landed)|\b(?:merges|lands|ships|is\s+in\s+main)\b))"
    r"|(?P<state>\bdepend(?:s|ing|ent)?\s+on\b|\bblocked\s+(?:by|on)\b)"
    rf"|\b(?:needs|requires)\b(?={_IN_SENTENCE}{{0,120}}\b(?:merged|released|deployed|landed)\b)",
    re.I,
)
_NEGATED_RE = re.compile(r"(?:\bno\s+longer|\bnot|n't|\bnever)\s+(?:\w+\s+)?$", re.I)
_CLAUSE_END_RE = re.compile(r";|\b(?:related|see|cf|also|follow(?:s|-up)|part\s+of|refs?)\b", re.I)
_MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\((https?://github\.com/[^)\s]+)\)")
_BACK_REF_RE = re.compile(r"\b(?:that|the\s+code|the\s+above)\s+PR\b|\bthe\s+PR\s+above\b", re.I)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+|\n")
_PARAGRAPH_END_RE = re.compile(r"\n\s*\n")


def map_markdown_to_dependency(body: str, repo: str, number: int) -> str | None:
    """The one PR its body says it merges only after, as owner/repo#N. A sentence that says so must name that PR in
    the same clause after the words that say so, in the list its colon opens, or point back to the only PR its
    paragraph names ('that PR'). A bare mention, a PR named before the words ('#12 depends on this'), a negation
    ('no longer blocked by') or more than one PR give none."""
    found: set[str] = set()
    text = _MD_LINK_RE.sub(r"\1", MD_HIDDEN_RE.sub("", body))
    full = _map_text_to_full_refs(text, repo)
    for paragraph in _PARAGRAPH_END_RE.split(text):
        in_paragraph = _find_prs(paragraph, repo, number, full)
        sentences = _SENTENCE_END_RE.split(paragraph)
        for index, sentence in enumerate(sentences):
            trigger = _TRIGGER_RE.search(sentence)
            if not trigger or (trigger["state"] and _NEGATED_RE.search(sentence[: trigger.start()])):
                continue
            # "Merge Org/lib#4 after this PR" names the PR that waits, not the one waited for.
            start = trigger.end() if trigger["verb"] else trigger.start()
            clause = sentence[start:]
            if m := _CLAUSE_END_RE.search(clause, trigger.end() - start):
                clause = clause[: m.start()]
            named = _find_prs(clause, repo, number, full)
            if not named and clause.rstrip().endswith(":"):
                named = _find_prs("\n".join(sentences[index + 1 :]), repo, number, full)
            if not named and _BACK_REF_RE.search(clause):
                named = in_paragraph
            found |= named
    return found.pop() if len(found) == 1 else None


def map_markdown_to_named_prs(body: str, repo: str, number: int) -> tuple[str, ...]:
    """Every PR its body names, as owner/repo#N, whatever the words around it; never itself. A bare #N is the PR of
    that number the body names in full elsewhere, else this repo's #N. Some may be issues: only GitHub can tell."""
    text = _MD_LINK_RE.sub(r"\1", MD_HIDDEN_RE.sub("", body))
    return tuple(sorted(_find_prs(text, repo, number, _map_text_to_full_refs(text, repo))))


def _map_text_to_full_refs(text: str, repo: str) -> dict[str, set[str]]:
    """Number to the owner/repo#N refs that name it in full: "goes with Org/lib#12 ... #12" means Org/lib's #12."""
    full: dict[str, set[str]] = {}
    for match in _PR_RE.finditer(text):
        pr = _map_match_to_full_ref(match, repo)
        if pr is not None:
            full.setdefault(pr.rpartition("#")[2], set()).add(pr)
    return full


def _map_match_to_full_ref(match: re.Match[str], repo: str) -> str | None:
    if match["url_number"]:
        return f"{match['url_owner']}/{match['url_repo']}#{match['url_number']}"
    if match["repo"]:
        return f"{match['owner'] or repo.partition('/')[0]}/{match['repo']}#{match['number']}"
    return None


def _find_prs(text: str, repo: str, number: int, full: dict[str, set[str]]) -> set[str]:
    prs: set[str] = set()
    for match in _PR_RE.finditer(text):
        named = _map_match_to_full_ref(match, repo)
        prs.update({named} if named else full.get(match["number"]) or {f"{repo}#{match['number']}"})
    return {pr for pr in prs if pr.lower() != f"{repo}#{number}".lower()}


def map_states_to_blockers(
    named: tuple[str, ...],
    must_merge: tuple[str, ...],
    skipped: set[str],
    states: dict[str, str | None],
) -> tuple[str, ...]:
    """Why the PR may not merge yet, one reason per PR: a named PR is open or could not be read, or one it must merge
    after is not merged. An issue, a number that is nothing, and a PR in `skipped` (lowercase: those stacked on this
    one) block nothing."""
    by_key = {ref.casefold(): state for ref, state in states.items()}
    must = {ref.casefold() for ref in must_merge}
    blockers: list[str] = []
    refs = {ref.casefold(): ref for ref in (*named, *must_merge)}
    for key, ref in sorted(refs.items()):
        if key in skipped:
            continue
        state = by_key.get(key)
        if state is None:
            blockers.append(f"could not read {ref}")
        elif state == "OPEN":
            blockers.append(f"waits for {ref} to merge")
        elif state == "CLOSED" and key in must:
            blockers.append(f"{ref} was closed without merging")
    return tuple(blockers)
