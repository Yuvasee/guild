# The review doc

The one description of how a PR's review document looks. The lead role, worker briefs, bridge's doc requests and
SKILL.md point here; where they repeat a rule (provenance marks, the Reviews section) as a reminder, this file is
the full version, so change it here first.

**When:** a code PR of 100 lines or more (docs, images, lockfiles and generated files left out). Register it with
`guild review doc <pr> /path/file.html`, and again after each update.
**Reader:** the owner, on the Guild page, on a laptop or a phone. They read **In short**, skim the headings and the
one-line summaries, and open only what they want to check.

## Before you write, and before you register

1. **Read the reference example first**, if this install keeps one (`house.md` names it): a doc the owner called
   clear. Match its plainness and length, not its subject.
2. **First reader.** Before `guild review doc`, give a fresh subagent the install's reader profile (who the reader
   is and what they already know; `house.md` names the file) and only the doc's **In short** and **What changes and
   why**, and ask it to explain back in its own words what changes, why and how, and to list every term that reader
   would not understand. A wrong or vague explain-back, or any term on that list not explained in the
   doc, means rewrite and ask a new fresh reader; register only once one gets it right with an empty list. Say in
   **Verified and not verified** that it passed.

## Essence visible, details folded

This holds for everything made for the owner (review docs, pages, reports), not only this doc. The owner reads the
essence and opens details only when they want them. The doc can be long; what shows before anything is opened is
short.

- **The visible layer** is headings, one-line summaries, short bullets, tables and diagrams. Together it reads in a
  few minutes.
- **No walls of text.** A visible paragraph runs to three lines at most. Anything longer becomes bullets or goes into
  a `<details>` under the line it supports.
- **Folded blocks.** Each decision, cost or risk, review and update is a folded block (markup below): its summary
  line names it and gives the verdict or handling, and stands alone; the body holds the reasoning, scenarios,
  evidence, code, numbers and findings.
- **No ids in the visible layer.** Commit hashes, run ids, CI job links and long log lines go inside a fold.
- **Folding is for length, never for hiding.** A risk to users, an open decision or a failed check shows in its
  summary line, and the clarity rules below still hold inside the fold: a reader who opens it must not have to guess.

## Clear to a newcomer

The owner reads many PRs from many areas and does not keep their internals in mind. Write for a reader who is
sharp but has never seen this code.

- **The story first.** The doc opens (in **In short**) with plain sentences about the thing being changed: what it
  is, what it does today and what is wrong with it, before any detail of the change.
- **Every internal name is a term.** Each service, function, table, flag, field, agent type, finding code, metric
  or in-house concept is a click term at its first use in each `h2` section (markup below), with plain words and
  where it lives in the code. A doc for a PR of 100 lines or more has at least 8 of them; `guild review doc` warns
  below that.
- **Full sentences.** No telegraphic notes or arrows in place of verbs; a reader must not reconstruct the meaning.
- **One concrete scenario per change.** Who does what, what they see before and after.
- **Details folded**, as above: clarity is not length.
- **A decision you made yourself is marked decided.** An open question the lead settled is moved out of
  `needs-decision` before the doc is registered, so the owner never sees a question that has an answer.
- **Scripts leave diagrams alone.** A script that edits the doc's HTML (wrapping terms, renumbering) skips `svg` and
  `pre` blocks; look at the diagrams once more before registering.
- **An enabling PR says how it will be used.** A PR that only enables something (a flag off by default, an opt-in
  setting, one step of a multi-PR plan) has, near the top of its description and in plain sentences, a section
  headed `How this will be used`: (1) the problem in the product today; (2) where this PR sits in the plan, as
  numbered steps with links to the sibling PRs and issues, each marked built or not built; (3) what switches it on,
  where, and who decides; (4) what happens if it is never switched on; (5) the named risk of using it wrong. The
  doc's opening story tells the same five points, so the plan never has to be explained in chat. Bridge reads the
  title and description for such words (off by default, an opt-in flag or value, behind a flag, step N of M, …) and keeps the PR not
  ready until that heading is there; a PR that only sounds like one says so in a line `Not an enabling PR: <reason>`.

## The order

Use these `h2` headings, in this order, worded as here. Inside a section, use `h3` only to group (a mechanism in
**How it works**, Tested live, Verified and Not verified); single items are folded blocks, not `h3`s.

| # | `h2` heading | What it holds |
|---|---|---|
| 1 | **In short** | First, two or three plain sentences that tell the story: what the thing being changed is and does today, what goes wrong or is missing, and what this PR makes different. Then five or six bullets, readable in 30 seconds: what the PR does, for whom, why now, what the owner must decide (or "nothing to decide"), and what is not done yet. |
| 2 | **What changes and why** | The essence. Before and after, with a diagram, and the reasons as bullets, each with its provenance mark. Small changes as bullets. What it deliberately does not change. |
| 3 | **How it works** | The mechanism, with diagrams: flow, sequence, data, where it runs. One `h3` per mechanism. Tables for structured facts (what each counter or field means). Extra essence sections, if any (for example, infrastructure the PR needs), go here as `h3`s or as `h2`s right after this one. |
| 4 | **Decisions** | One folded block per decision. Open ones first, marked `needs-decision`: the summary line is the question and the recommendation; the body has the options, what each costs, and why. Then the ones taken, marked `decided`: the summary line is what was chosen; the body has the alternatives and why. |
| 5 | **Costs and risks** | One folded block per cost or risk: the summary line is what it is and its handling; the body makes it clear without guessing (below). |
| 6 | **Where to look** | Files in reading order, one row per file with what to check there (its functions too); the local worktree path in a paragraph above them. Kept visible: the owner uses it every time. |
| 7 | **Tests** | Short: one line per test or group, or a small table (test, what it proves). What no test covers, one line. Detail, if any, folded. |
| 8 | **Reviews** | One folded block per review that ran (Codex, the review bot, others). Summary line: who, how many findings, the outcome in a few words. Body: each finding one line with its outcome: fixed in `<commit>` / deferred: reason + issue / rejected: reason. Bridge reads this section: until it names every review the install requires (`required_reviews`) the PR is not ready for the owner. |
| 9 | **Verified and not verified** | `h3` Tested live (required): what ran in a live environment for the PR (a preview, staging) and what it showed, marked `observed`; what could not run there and why. `h3` Verified: what else was run or checked and what it showed, one plain line each with its mark; hashes, run ids and logs folded. `h3` Not verified: what was not, why, and who would notice and when. |
| 10 | **Changes since the last review** | Only after the owner's own review of this PR (their GitHub review or comments on it, or a review sent from the Guild page), covering what changed since their latest one. Never for other reviewers' rounds or for updates before the owner has looked. Always last; newest first, one folded block per update: what changed in the code and in this doc; commits inside. |

The doc always opens with **In short**, written for a reader with no context: what, why, how. Nothing comes
before it: no 'changed since' or update box at the top, even right after an update. What changed goes only in
**Changes since the last review**, which exists only once the owner has reviewed an earlier head, and stays last
and folded.

In **Where to look**, each file is named once, by its full path from the repo root (`src/app/run.py`, not `run.py`,
`.../app/run.py` or a home or worktree path). Each row is a list item or table row whose first `<code>` is that path;
a line number after it (`run.py:40-52`) is fine.

`guild review doc` refuses a doc whose first `h2` is not **In short**, that has **Changes since the last review**
before the owner's review or anywhere but last, or whose **Where to look** names a file twice, gives a path not from
the repo root, or, when the PR has a review worktree, a path the commit in `pr-head` does not have. Whether the doc
reads plainly and holds no leftovers of an earlier design no check can tell: that stays the writer's job.

A section with nothing in it says so in one line rather than disappearing, except **Decisions** and **Changes since
the last review**, which are left out when empty.

## The markup

The Guild viewer styles and wires exactly this markup, so the doc sets no styles for it: no CSS for links, `h2`, `h3`,
`.term`, `.mark` or `details.item`, and no scripts for them. The viewer sets `data-viewer-terms` on `<html>`; a
fallback for reading the file outside the viewer applies only without it.

- **Headings:** `h2` per section and `h3` inside, plain text (no marks or badges inside a heading), never skipping a
  level or putting a heading's text in a styled `div`. The viewer builds a sticky contents bar from them, sizes and
  spaces them, and indents the body under each, so write no table of contents and no heading styles.
- **Term:** every internal name (service, function, table, flag, agent type, finding code, metric) is a term, marked
  at its first use in each `h2` section, not every use. Click opens a popover with the meaning; there is no "Names
  used below" section and no glossary.
  `<span class="term" data-def="One plain sentence: what it is, and where it lives in the code.">LLM Service</span>`. `data-def` is plain text (no
  markup, `&quot;` for a quote), one sentence a newcomer understands. A code name goes inside:
  `<span class="term" data-def="…"><code>llm_service</code></span>`.
- **Mark:** provenance and handling labels, small and quiet, after the claim or at the end of a summary line, never in
  a heading, and no legend anywhere. `<span class="mark observed">observed</span>`. Classes: `observed`, `reproduced`,
  `from-code` (provenance), and `decided` (a decision taken and kept), `accepted`, `fixed-here`, `deferred`,
  `rejected`, `needs-decision` (handling). The text is the class in lowercase, with a space for the hyphen.
- **Folded block:** one per decision, cost or risk, review, update and long test detail:
  `<details class="item"><summary><b>D1. Title</b> - one-line verdict or handling <span class="mark
  accepted">accepted</span></summary> body: bullets, tables, evidence </details>`. Nothing but the summary line shows
  until it is opened, so that line must stand alone.
- **Plain folds:** a `<details>` without the class, for evidence under a bullet or a paragraph.
- **Links:** plain `<a href>`; the viewer colors them.

## Diagrams

At least one in **What changes and why** (before and after) and one in **How it works**. Inline SVG or Mermaid.
Each gets a one-line caption that says what to notice. Readable in light and dark, and on a phone (scrolls
sideways inside its own box, never the page).

## Costs and risks: clear without guessing

Every Cost and every Risk gives, in its folded body:
1. the concrete scenario: who does what and sees what, and where (which screen or API);
2. whether it hits current users (as named in your project notes) or is theoretical;
3. the handling: accepted, fixed here, an existing issue (name it), or needs the owner, with what each option costs.

The summary line gives the risk and its handling in one line, with its mark.

## Provenance

Every reason a change exists, and every claim that something fails or works, says where it comes from, with a mark:
- `observed`: it happened to a real user. Cite the log, data or report.
- `reproduced`: we made it happen. Say on which commit and how (command, test, environment). For a defect,
  reproduce the before on unpatched code, not only the after.
- `from-code`: read from source or docs, not seen running.

Never write "users hit" or "onboarding X found" unless it was observed. If nothing was observed, say who would
hit it and when.

## Tone and names

- It informs; it does not sell. No "robust", "seamless", "significantly improves". State what changes and what it
  costs, and let the reader judge.
- Mark each internal name as a term at its first use in a section, or state its effect instead.
- Plain words; units and numbers where they exist ("adds about 40 s to CI", not "slightly slower").

## The file

- One self-contained HTML file: CSS and scripts inline (a pinned Mermaid from a CDN is fine). The viewer serves it
  sandboxed: scripts run, but cannot call bridge.
- `<meta name="pr-head" content="<full sha of the PR head it describes>">` in `<head>`. Bridge shows the doc as
  current while that equals the PR's head, or while every change since is a comment line, a blank line or a docs
  file (Markdown, images), read from GitHub's compare of the two; a docstring counts as code, and so does any file it
  cannot read line by line. Otherwise the queue shows "doc older than code: N commits, M lines" (M leaves out comments
  and docs) and bridge asks the session that pushed last, or its lead once it has ended, else the PR's owner, to
  update it, once per new head until the owner has reviewed. Still behind an hour after the first ask, the assistant
  is told once; registering the doc again starts this over.
- Light and dark (`prefers-color-scheme`), an explicit `body` background, and a readable width on a phone with no
  sideways page scroll: long names in headings and code wrap (`overflow-wrap: anywhere`), wide tables and diagrams
  scroll inside their own box.
- Re-check docs already in the owner's queue when this spec changes.
