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
    r"|\b(?:merge|merges|merging|ship|ships|shipping|land|lands|landing|release|releasing|deploy|deploying)\b"
    rf"{_IN_SENTENCE}{{0,80}}?\b(?:after|until)\b"
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
    for paragraph in _PARAGRAPH_END_RE.split(text):
        in_paragraph = _find_prs(paragraph, repo, number)
        sentences = _SENTENCE_END_RE.split(paragraph)
        for index, sentence in enumerate(sentences):
            trigger = _TRIGGER_RE.search(sentence)
            if not trigger or (trigger["state"] and _NEGATED_RE.search(sentence[: trigger.start()])):
                continue
            clause = sentence[trigger.start() :]
            clause = (
                clause[: m.start()] if (m := _CLAUSE_END_RE.search(clause, trigger.end() - trigger.start())) else clause
            )
            named = _find_prs(clause, repo, number)
            if not named and clause.rstrip().endswith(":"):
                named = _find_prs("\n".join(sentences[index + 1 :]), repo, number)
            if not named and _BACK_REF_RE.search(clause):
                named = in_paragraph
            found |= named
    return found.pop() if len(found) == 1 else None


def _find_prs(text: str, repo: str, number: int) -> set[str]:
    owner = repo.partition("/")[0]
    prs: set[str] = set()
    for match in _PR_RE.finditer(text):
        if match["url_number"]:
            pr = f"{match['url_owner']}/{match['url_repo']}#{match['url_number']}"
        elif match["repo"]:
            pr = f"{match['owner'] or owner}/{match['repo']}#{match['number']}"
        else:
            pr = f"{repo}#{match['number']}"
        if pr.lower() != f"{repo}#{number}".lower():
            prs.add(pr)
    return prs
