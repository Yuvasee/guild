# The review doc

How a large PR's review document looks: the document a lead writes so the owner can review the PR on the page. The
lead role, worker briefs and bridge's doc requests point here.

**When:** a code PR of 100 lines or more (docs, images and the excluded files, lockfiles by default, left out). Register it with
`guild review doc <pr> /path/file.html`, and again after each update.
**Reader:** the owner, on the Guild page, on a laptop or a phone. They read **In short**, skim the headings and the
one-line summaries, and open only what they want to check.

## Essence visible, details folded

- The visible layer is headings, one-line summaries, short bullets, tables and diagrams; it reads in a few minutes.
- A visible paragraph runs to three lines at most; anything longer becomes bullets or goes into a `<details>`.
- Each decision, cost or risk, review and update is a folded block whose summary line stands alone.
- Folding is for length, never for hiding: an open decision, a risk or a failed check shows in its summary line.

## The order

Use these `h2` headings, in this order. A section with nothing in it says so in one line, except **Decisions** and
**Changes since the last review**, which are left out when empty.

| # | `h2` heading | What it holds |
|---|---|---|
| 1 | **In short** | Five or six bullets: what the PR does, why now, what the owner must decide (or "nothing to decide"), what is not done yet. |
| 2 | **What changes and why** | Before and after, with a diagram; the reasons as bullets, each with its provenance mark. |
| 3 | **How it works** | The mechanism, with diagrams; one `h3` per mechanism. |
| 4 | **Decisions** | One folded block per decision: open ones first (`needs-decision`), then the ones taken (`decided`). |
| 5 | **Costs and risks** | One folded block each: the concrete scenario, whether it can happen today or is theoretical, and its handling. |
| 6 | **Where to look** | Files and functions in reading order, one line each with what to check there. |
| 7 | **Tests** | One line per test or group: what it proves. What no test covers, one line. |
| 8 | **Reviews** | One folded block per review that ran (Codex, Claude, ReviewBot, others): each finding one line with its outcome (fixed in `<commit>` / deferred: reason + issue / rejected: reason). Bridge reads this section: until it names both Codex and Claude the PR is not ready for the owner. |
| 9 | **Verified and not verified** | What was run or checked and what it showed, each with its mark; what was not, and why. |
| 10 | **Changes since the last review** | Only after the owner reviewed this PR; newest first, one folded block per update. Always last. |

## The markup

The Guild viewer styles and wires exactly this markup, so the doc sets no styles or scripts for it.

- **Headings:** `h2` per section and `h3` inside, plain text, never skipping a level. The viewer builds the contents
  bar from them.
- **Term:** an internal name, marked at its first use in each section; a click shows its meaning.
  `<span class="term" data-def="One plain sentence: what it is.">LLM Service</span>`
- **Mark:** `<span class="mark observed">observed</span>`. Classes: `observed`, `reproduced`, `from-code`
  (provenance), and `decided`, `accepted`, `fixed-here`, `deferred`, `rejected`, `needs-decision` (handling).
- **Folded block:** `<details class="item"><summary><b>D1. Title</b> - one-line verdict <span class="mark
  accepted">accepted</span></summary> body </details>`
- **Plain folds:** a `<details>` without the class. **Links:** plain `<a href>`.

## Provenance

Every reason a change exists, and every claim that something fails or works, carries a mark:
- `observed`: it happened to a real user. Cite the log, data or report.
- `reproduced`: we made it happen. Say on which commit and how.
- `from-code`: read from source or docs, not seen running.

## The file

- One self-contained HTML file: CSS and scripts inline. The viewer serves it sandboxed.
- `<meta name="pr-head" content="<full sha of the PR head it describes>">` in `<head>`. Bridge shows the doc as
  current only while that equals the PR's head, and asks for an update after each push until the owner has reviewed.
- Light and dark, an explicit `body` background, readable on a phone with no sideways page scroll.
