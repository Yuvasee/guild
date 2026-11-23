import re
from collections.abc import Iterator

from bridge.review_marks import MD_HIDDEN_RE

_PR_RE = re.compile(
    r"https?://github\.com/(?P<url_owner>[\w.-]+)/(?P<url_repo>[\w.-]+)/pull/(?P<url_number>\d+)"
    r"|(?<![\w/.&-])(?:(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+))?#(?P<number>\d+)\b"
)
# Within a sentence: a dot inside a URL or a file name does not end it.
_IN_SENTENCE = r"(?:[^.!?\n]|[.!?](?=\S))"
_TRIGGER_RE = re.compile(
    r"\bonly\s+after\b"
    r"|(?P<verb>\b(?:merge|merges|merging|ship|ships|shipping|land|lands|landing|release|releasing|deploy|deploys|"
    r"deploying)\b"
    rf"{_IN_SENTENCE}{{0,80}}?\b(?:after|until)\b)"
    r"|\bmerge\s+\S+\s+first\b"
    rf"|\bafter\b(?={_IN_SENTENCE}{{0,120}}(?:\b(?:is|are|gets?|has\s+been|have\s+been)\s+"
    r"(?:merged|released|shipped|deployed|landed)|\b(?:merges|lands|ships|is\s+in\s+main)\b))"
    r"|(?P<state>\bdepend(?:s|ing|ent)?\s+on\b|\bblocked\s+(?:by|on)\b)"
    rf"|\b(?:needs|requires)\b(?={_IN_SENTENCE}{{0,120}}\b(?:merged|released|deployed|landed)\b)",
    re.I,
)
# Before 'deploy … after', only this PR or nothing: 'the next deploy after #12 merges' times someone else's deploy.
# \W, not [\W_]: an underscore both ways backtracks quadratically, and emphasis is gone before it is matched.
_DEPLOY_SUBJECT_RE = re.compile(
    r"^\W*(?:\d+[.)]\W*)?(?:\w+:\W*)?(?:(?:please|only|then|this|it|PR|change|do|not|don['\u2019]t|never|will|should|"
    r"must|can)\b\W*)*$",
    re.I,
)
_DEPLOY_SUBJECT_MAX = 80
_NEGATED_RE = re.compile(r"(?:\bno\s+longer|\bnot|n't|\bnever)\s+(?:\w+\s+)?$", re.I)
_CLAUSE_END_RE = re.compile(r";|\b(?:related|see|cf|also|follow(?:s|-up)|part\s+of|refs?)\b", re.I)
_MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\((https?://github\.com/[^)\s]+)\)")
_BACK_REF_RE = re.compile(r"\b(?:that|the\s+code|the\s+above)\s+PR\b|\bthe\s+PR\s+above\b", re.I)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_HEADING_RE = re.compile(r"^\s*#+\s")
_PARAGRAPH_END_RE = re.compile(r"\n\s*\n")
_RELATED_LINE_RE = re.compile(r"^[\s>*+-]*(?:\*\*|__)?related(?:\*\*|__)?\s*:(?P<rest>.*)$", re.I | re.M)
# A reason must follow, and a line that also says stacked or not independent is undecided: it keeps the wait.
_INDEPENDENT_RE = re.compile(r"\bindependent\s+because\s+[^\s.…]", re.I)
_UNDECIDED_RE = re.compile(r"\bstacked\b|\bnot\s+independent\b", re.I)


def map_markdown_to_dependencies(body: str, repo: str, number: int) -> tuple[tuple[str, str], ...]:
    """Each PR its body says it merges only after, as owner/repo#N, with the first sentence that says so, cut to show.
    A sentence that says so must name that PR in the same clause after the words that say so, in the list its colon
    opens, or point back to the only PR its paragraph names ('that PR'). A bare mention, a PR named before the words
    ('#12 depends on this'), a negation ('no longer blocked by') or a deploy of something else ('the next deploy after
    #12 merges') give none."""
    found: dict[str, str] = {}
    text = _MD_LINK_RE.sub(r"\1", MD_HIDDEN_RE.sub("", body))
    full = _map_text_to_full_refs(text, repo)
    for paragraph in _PARAGRAPH_END_RE.split(text):
        in_paragraph = _find_prs(paragraph, repo, number, full)
        sentences = _map_paragraph_to_sentences(paragraph)
        for index, sentence in enumerate(sentences):
            shown, negated_until = "", 0
            for trigger in _find_triggers(sentence):
                if trigger.start() < negated_until:
                    continue
                # "Merge Org/lib#4 after this PR" names the PR that waits, not the one waited for.
                start = trigger.end() if trigger["verb"] else trigger.start()
                clause = sentence[start:]
                if m := _CLAUSE_END_RE.search(clause, trigger.end() - start):
                    clause = clause[: m.start()]
                # 'No longer depends on #12, which lands after #13': the negation reaches the next ';'.
                if trigger["state"] and _NEGATED_RE.search(sentence[: trigger.start()]):
                    negated_until = (
                        semicolon if (semicolon := sentence.find(";", trigger.end())) >= 0 else len(sentence)
                    )
                    continue
                named = _find_prs(clause, repo, number, full)
                if not named and clause.rstrip().endswith(":"):
                    named = _find_prs("\n".join(sentences[index + 1 :]), repo, number, full)
                if not named and _BACK_REF_RE.search(clause) and len(in_paragraph) == 1:
                    named = in_paragraph
                if named:
                    shown = shown or _cut_to_shown(" ".join(_EMPHASIS_RE.sub("", sentence).split()))
                    found.update({pr: shown for pr in named if pr not in found})
    return tuple(sorted(found.items()))


def _map_paragraph_to_sentences(paragraph: str) -> list[str]:
    """Its sentences. A line wrapped mid-sentence goes on with the next one; a line break after a heading, a line that
    ends in . ! ? or :, or before a list item, quote or heading, ends one, as does each table row."""
    lines: list[str] = []
    previous = ""
    for line in paragraph.split("\n"):
        ends = _HEADING_RE.match(previous) or _EMPHASIS_RE.sub("", previous).rstrip().endswith((".", "!", "?", ":"))
        table = previous.lstrip().startswith("|") or line.lstrip().startswith("|")
        if lines and not ends and not table and not _BLOCK_START_RE.match(line):
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
        previous = line
    return [sentence for line in lines for sentence in _SENTENCE_END_RE.split(line)]


def _find_triggers(sentence: str) -> Iterator[re.Match[str]]:
    """Each run of words that says it merges after a PR. Someone else's deploy owns its 'after': only a nearer verb
    ('To deploy, merge this after #12') takes it back, not the bare 'after #12 merges' it spans."""
    pos, owned_until = 0, 0
    while trigger := _TRIGGER_RE.search(sentence, pos):
        pos = trigger.start() + 1
        if trigger.start() < owned_until and not trigger["verb"]:
            continue
        verb, before = trigger["verb"] or "", sentence[: trigger.start()]
        # Past this many characters the words before it name something else: no need to read them all again.
        if verb.lower().startswith("deploy") and (
            len(before) > _DEPLOY_SUBJECT_MAX or not _DEPLOY_SUBJECT_RE.match(_EMPHASIS_RE.sub("", before))
        ):
            owned_until = max(owned_until, trigger.end())
            continue
        yield trigger
        pos = trigger.end()


# Anchored at the sentence's start, after marks, a label ('Note:') or 'please': an instruction, not prose such as
# 'records merge when their keys match'.
_PRECONDITION_RE = re.compile(
    r"^[\W_]*(?:\w+(?:\s+\w+)?:\s*)?(?:please\s+)?(?:"
    r"(?:only\s+)?merge(?:\s+(?:this|it|this\s+PR))?\s+(?:after|once|when|until|only\s+(?:after|when|once|if))\b"
    r"|(?:don['\u2019]?t|never)\s+merge(?:\s+(?:this|it|this\s+PR))?\s+(?:until|before)\b"
    r"|do\s+not\s+merge\b|hold\s+off\s+(?:on\s+)?merging\b"
    r"|(?:don['\u2019]?t|never)\s+merge(?:\s+(?:this|it|this\s+PR))?\s+yet\b"
    r"|keep(?:\s+(?:this|it|this\s+PR))?\s+unmerged\b"
    r"|hold\W*$)",
    re.I,
)
_DEPLOY_WORDS_RE = re.compile(
    r"\b(?:runs?|running|ran|deploy(?:s|ed|ing)?|appl(?:y|ies|ied)|roll(?:s|ed)?\s+out|releas(?:e|es|ed)|"
    r"upgrad(?:e|es|ed)|install(?:s|ed)?|is\s+live|goes\s+live)\b",
    re.I,
)
_EMPHASIS_RE = re.compile(r"\*+|(?<!\w)__?|__?(?!\w)")
_BLOCK_START_RE = re.compile(r"^\s*(?:[-*+>]|#+|\d+[.)])\s+")
_PRECONDITION_SHOWN_MAX = 120
PRECONDITION_PREFIX = 'its body says: "'


def map_markdown_to_preconditions(body: str, repo: str, number: int) -> tuple[str, ...]:
    """Why its body says it may not merge yet: each sentence such as 'Merge after the API is deployed' or 'Do not
    merge', as a blocker that stays until the sentence is edited out. One that names a PR is that PR's wait instead,
    which its merge clears."""
    text = _MD_LINK_RE.sub(r"\1", MD_HIDDEN_RE.sub("", body))
    full = _map_text_to_full_refs(text, repo)
    found: list[str] = []
    for block in _map_text_to_blocks(text):
        for sentence in re.split(r"(?<=[.!?])\s+", _EMPHASIS_RE.sub("", block)):
            if not _PRECONDITION_RE.search(sentence):
                continue
            # A sentence naming a PR is that PR's wait, unless what it waits for is a run or a deploy, which no merge
            # of that PR can tell: 'merge only after the team runs the chart from #12'.
            if _find_prs(sentence, repo, number, full) and not _DEPLOY_WORDS_RE.search(sentence):
                continue
            reason = f'{PRECONDITION_PREFIX}{_cut_to_shown(sentence)}" (edit the body once that is done)'
            if reason not in found:
                found.append(reason)
    return tuple(found)


def _cut_to_shown(sentence: str) -> str:
    if len(sentence) <= _PRECONDITION_SHOWN_MAX:
        return sentence
    return sentence[: _PRECONDITION_SHOWN_MAX - 1].rstrip() + "\u2026"


def map_dependencies_to_quotes(dependencies: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {ref.casefold(): sentence for ref, sentence in dependencies}


def map_sentence_to_quote(sentence: str | None) -> str:
    """What a wait on a PR adds so its owner sees which sentence of the body to edit."""
    return f' (its body says: "{sentence}")' if sentence else ""


def _map_text_to_blocks(text: str) -> list[str]:
    """Its paragraphs, list items, quotes and headings, each on one line without its mark: a wrapped line joins the
    one before it."""
    blocks: list[str] = []
    joined = False
    for line in text.splitlines():
        if not line.strip():
            joined = False
            continue
        starts = _BLOCK_START_RE.match(line)
        content = " ".join(line[starts.end() if starts else 0 :].split())
        if joined and not starts:
            blocks[-1] += " " + content
        else:
            blocks.append(content)
        joined = True
    return blocks


def map_markdown_to_named_prs(body: str, repo: str, number: int) -> tuple[str, ...]:
    """Every PR its body names, as owner/repo#N, whatever the words around it; never itself. A bare #N is the PR of
    that number the body names in full elsewhere, else this repo's #N. Some may be issues: only GitHub can tell.
    A Related line that calls them independent names PRs it need not wait for."""
    text = _MD_LINK_RE.sub(r"\1", MD_HIDDEN_RE.sub("", body))
    full = _map_text_to_full_refs(text, repo)
    independent: set[str] = set()
    undecided: set[str] = set()
    for m in _RELATED_LINE_RE.finditer(text):
        words = _EMPHASIS_RE.sub("", m["rest"])
        settled = _INDEPENDENT_RE.search(words) and not _UNDECIDED_RE.search(words)
        (independent if settled else undecided).update(pr.casefold() for pr in _find_prs(words, repo, number, full))
    freed = independent - undecided
    return tuple(sorted(pr for pr in _find_prs(text, repo, number, full) if pr.casefold() not in freed))


def map_markdown_to_related_prs(body: str, repo: str, number: int) -> tuple[str, ...]:
    """The PRs its body's 'Related:' lines name, as owner/repo#N: the ones it says how it relates to."""
    text = _MD_LINK_RE.sub(r"\1", MD_HIDDEN_RE.sub("", body))
    full = _map_text_to_full_refs(text, repo)
    lines = "\n".join(m["rest"] for m in _RELATED_LINE_RE.finditer(text))
    return tuple(sorted(_find_prs(lines, repo, number, full)))


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
    must_merge: tuple[str, ...],
    skipped: set[str],
    states: dict[str, str | None],
    said: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Why the PR may not merge yet, one reason per PR it must merge after: it is open, closed without merging or could
    not be read. A PR its body only names is no wait: a body cites PRs as proof, as examples or as history. An issue,
    a number that is nothing, and a PR in `skipped` (lowercase: those stacked on this one) block nothing. A wait
    quotes the sentence `said` has for its PR (lowercase)."""
    by_key = {ref.casefold(): state for ref, state in states.items()}
    blockers: list[str] = []
    refs = {ref.casefold(): ref for ref in must_merge}
    for key, ref in sorted(refs.items()):
        if key in skipped:
            continue
        state = by_key.get(key)
        if state is None:
            blockers.append(f"could not read {ref}")
        elif state == "OPEN":
            blockers.append(f"waits for {ref} to merge{map_sentence_to_quote((said or {}).get(key))}")
        elif state == "CLOSED":
            blockers.append(f"{ref} was closed without merging")
    return tuple(blockers)
