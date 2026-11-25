# The review doc

The one description of how a PR's review document looks. The lead role, worker briefs, bridge's doc requests and
SKILL.md point here; where they repeat a rule (provenance marks, the Reviews section) as a reminder, this file is
the full version, so change it here first.

**When:** a code PR of 100 lines or more (docs, images, lockfiles and generated files left out). Register it with
`guild review doc <pr> /path/file.html`, and again after each update.
**Reader:** the owner, on the Guild page, on a laptop or a phone. They read the verdict strip, **In short** and the
**User story** when there is one, skim the headings and the one-line summaries, and open only what they want to check.

## Before you write, and before you register

1. **Read the reference example first**, if this install keeps one (`house.md` names it): a doc the owner called
   clear. Match its plainness and length, not its subject.
2. **First reader.** Before `guild review doc`, give a fresh subagent the install's reader profile (who the reader is
   and what they already know; `house.md` names the file), the doc's **In short**, **User story** (when it has one),
   **What changes and why** and **Fields** (when it has one), the top of the PR's description (its three opening parts,
   see **The PR description** below), the verdict strip and **What else could break** (when it has them), and the
   PR's diff, and ask it to retell the user story, if there is one, in its own words, step by step, today and after;
   to explain back from the doc and the diff what changes, why and how, and what each field in **Fields** is for and
   whether its example value looks real; to say whether the verdict strip agrees with the doc and the diff, and to
   name any caller, reader or version the diff touches that **What else could break** leaves out; to list every term
   that reader would not understand; and to list the sentences that could be deleted without losing anything the
   reader needs to decide or review. A wrong retelling of the story, a wrong or vague explain-back, a strip that
   disagrees with the doc, a missed caller or reader, or a flagged term the doc neither explains nor drops, means
   rewrite and ask a new fresh reader. The deletion list is advice: cut what you agree with; readers differ there, so it never blocks a pass. It
   passes once a reader's explain-back is right and every term it flagged is explained or gone. At most three readers:
   if the third still gets it wrong, the doc's structure is the problem, so rework the top rather than polish sentences.
   Say under **Checks** (its Verified part) that the first reader passed, in which round, and any flagged point you
   kept with why (`guild review doc` refuses the doc without that line).

## The PR description

The PR's own description opens with a condensed version of the doc's opening, for a colleague with no context and little
attention, in three parts (the second only when it applies):

1. **Context**: what this is and its type (the doc's verdict strip names it: a model change, a bug fix, a refactor,
   …), where it came from (the issue, incident or request), why it is needed now. Two or three sentences.
2. **User story**, only when the PR changes what a real person (a user, an admin, an operator) does or sees; internal
   plumbing (stored records, clean-up jobs, refactors, schemas) gets none, never a stretched one, so skip this part.
   Numbered steps with concrete actors: who does what, what the system does, what goes wrong today or doesn't happen;
   then the same steps after this PR. No abstract nouns where an actor and an action fit.
3. **How it is solved, in principle**: two to four sentences, no file or function names.

A PR that adds or changes a model, a schema, an API or event contract, or a stored shape then lists each added or
changed field, one line each: what it is, why it is needed (who writes it, who reads it) and a realistic example
value.

A PR that only enables something (a flag off by default, an opt-in value, one step of a plan) keeps its **How this
will be used** section right after these three parts, not folded: the merge check looks for it near the top; its long
parts may fold inside it. Everything else (files, tests, measurements, reviews, rollout) comes after, folded in
`<details>`. A key term specific
to this PR or its area is defined at its first use, a few at most, never a glossary at the top: write
`<ins title="one-line definition">term</ins>` (GitHub keeps this tag: the term shows underlined and the definition
appears on hover; it strips `<abbr>` and `<dfn>`); a definition longer than a line goes in parentheses after the term.
No headers like What / Why / Measured filled with jargon: the three parts are plain sentences.

## Essence visible, details folded

This holds for everything made for the owner (review docs, pages, reports), not only this doc. The owner reads the
essence and opens details only when they want them. The doc can be long; what shows before anything is opened is
short.

- **The visible layer** is the verdict strip, headings, one-line summaries, short bullets, tables and diagrams. Outside
  **Fields** it holds at most 900 words (the install's `doc_visible_max_words`), about four minutes of reading: fold
  summary lines count, fold bodies, diagrams and code blocks do not. **Fields** is reference the owner reads whole,
  so it is not counted.
- **No walls of text.** A visible paragraph runs to three lines at most. Anything longer becomes bullets or goes into
  a `<details>` under the line it supports.
- **Short sentences.** No sentence in the visible part of **In short**, **User story** (if any) and **What changes and
  why** runs over 30 words, and each sentence says one thing about the change as it is now: not how the PR got there (an
  earlier version, a dropped approach), not the review itself (who reviewed, which checks ran; that goes in
  **Checks**), and not reassurance with no fact the reader acts on.
- **Folded blocks.** Each decision, cost or risk, review and update is a folded block (markup below): its summary
  line starts with the point (readers scanning a line see its first two words), gives the verdict or handling, and
  stands alone; an id such as `D3`, if kept, goes after the point. The body holds the reasoning, scenarios,
  evidence, code, numbers and findings.
- **No ids in the visible layer.** Commit hashes, run ids, CI job links and long log lines go inside a fold.
- **Folding is for length, never for hiding.** A risk to users, an open decision or a failed check shows in its
  summary line, and the clarity rules below still hold inside the fold: a reader who opens it must not have to guess.

## Clear to a newcomer

The owner reads many PRs from many areas and does not keep their internals in mind. Write for a reader who is
sharp but has never seen this code.

- **The story first.** The doc opens (in **In short**) with plain sentences about the thing being changed: what it
  is, what it does today and what is wrong with it, before any detail of the change.
- **Key terms in the text, the rest as terms.** **In short** explains the two or three terms the reader needs to
  decide in its own sentences; nothing needed to decide sits only in a popover. Every other service, function,
  table, flag, field, agent type, finding code, metric or in-house concept is a click term at its first use in each
  `h2` section (markup below), with plain words and where it lives in the code. There is no quota.
- **Full sentences.** No telegraphic notes or arrows in place of verbs; a reader must not reconstruct the meaning.
- **One concrete scenario per change.** Who does what, what they see before and after. The main one is the
  **User story** when the doc has one; without it, and for a change the story does not reach, the scenario goes in
  **What changes and why**.
- **Every Before names its baseline:** main today, a PR it builds on (`Before (#12 today)`), a named branch
  (`Before (branch dev/x today)`), or hypothetical; a design alternative is never shown as Before.
- **A term list is a list.** Three or more terms with their meanings go in a `<ul>`, a `<dl>` or a two-column table,
  never in a paragraph of `Term: meaning.` sentences.
- **A changed model shows its fields.** A PR that adds or changes a model, a schema, an API or event contract, or a
  stored shape has a visible **Fields** section: every changed model and contract, each with all its added or
  changed fields, what each is, why it is needed (who writes it, who reads it), its limits and a realistic example
  value. A field nobody reads is flagged there, not left for the reader to find.
- **What else could break.** Whether the change breaks code elsewhere is the reviewer's hardest question, so the doc
  answers it: the other callers, readers and versions it touches (an older pod reading a new value mid-rollout, a
  client of the API, an export), each with what happens and its handling. A PR that adds a model, helper or concept
  also says which existing one looks most like it, and whether it was reused or why not.
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
  doc's **How this will be used** section gives the same five points, so the plan never has to be explained in chat.
  Bridge reads the title and description for such words (off by default, an opt-in flag or value, behind a flag, step
  N of M, …) and keeps the PR not ready until that heading is there; a PR that only sounds like one says so in a line
  `Not an enabling PR: <reason>`.
- **A PR that changes the same files as another open PR says how they relate.** When bridge reports two open PRs
  changing the same files, one is stacked on the other, or each description gets a line
  `Related: #N — stacked | independent because …` naming the other, one PR per line; until then neither is ready
  for the owner. A line saying `independent because <reason>` does not make the PR wait for the other's merge.

## The order

The doc opens with a verdict strip, then uses these `h2` headings, in this order, worded as here. Which ones it has
follows the PR's type (below); a section that does not apply is left out, never kept to say "nothing here". Inside a
section, use `h3` only to group (a mechanism in **How it works**, the parts of **Checks**); single items are folded
blocks, not `h3`s.

**The verdict strip** comes before **In short** and nothing else does: five labelled lines, at most 25 words each,
so the owner knows in seconds whether to read on (markup below).

| Line | What it says |
|---|---|
| **Type** | The PR's type first, one or two of: model / contract, refactor, bug fix, new feature, infra / chart, docs only, enabling, backport (joined by `+`); then, after a comma, its size in a few words. Only the part before the first comma, colon or number is read as the type. |
| **You decide** | What the owner must decide, or "nothing". |
| **Could break** | The short answer of **What else could break**. |
| **Main risk** | The main risk and its handling, or "none known". |
| **Checks** | The outcome of tests, reviews and checks: what passes, open findings, what could not be verified. |

| # | `h2` heading | What it holds |
|---|---|---|
| 1 | **In short** | First, two or three plain sentences that tell the story: what the thing being changed is and does today, what goes wrong or is missing, and what this PR makes different, with its two or three key terms explained in the text. Then at most five bullets of at most 25 words each, readable in 30 seconds: what the PR does, for whom, why now, and what is not done yet. |
| 2 | **User story** (only when it applies) | Only when the PR changes what a real person (a user, an admin, an operator) does or sees; internal plumbing (stored records, clean-up jobs, refactors, schemas) gets none, never a stretched one: leave the section out and **What changes and why** follows **In short**. When it applies: one concrete person, named by role (a support agent, a tenant admin; never a real customer or colleague), and what happens to them, as a numbered list (`<ol>`) of at least three steps, outside closed folds. Each step says in plain words what they do and see today and after this PR ("Today: … After: …"); a code name appears only as a term that explains it. One short line before the list may set the scene. |
| 3 | **What changes and why** | The essence. Before and after, with a diagram, and the reasons as bullets, each with its provenance mark; when there is a **User story**, a bullet names the step it changes ("step 3"). Small changes as bullets. What it deliberately does not change. |
| 4 | **Fields** (model / contract; others when a stored or exchanged shape changes) | Visible, not folded. Every changed model and contract, each with a table of all its added or changed fields, one row per field, with columns: the field; what it is, in plain words; why it is needed: who writes it and who reads it; its limits (type, allowed values, empty or not, size); an example value: realistic and in the field's real shape (a plausible id, date, amount or allowed value; a short JSON for an object or list), invented, never real customer data. A field with no reader is flagged as such in its row. Writer and reader file paths may go in a plain fold under the table; the fields themselves never fold. An annotated example record (a code block with a short comment per value) may come first, as well as the tables, never instead. Example (invented): why `OrderOut` gains `refund_reason` (`"arrived damaged"`), and what `include` and `exclude` on `OrderFilter` mean (`{"include": ["shipped"], "exclude": ["test"]}`). |
| 5 | **What else could break** (every type but docs only and backport) | The other code, readers and versions the change touches, one bullet each: what happens to them and the handling, with its mark. For a model / contract or new feature PR, a line starting `Reuse:` names the existing model, helper or concept closest to what the PR adds, and whether it was reused or why not. |
| 6 | Sections of the PR's type | **Root cause** (bug fix; infra when it fixes an incident): the symptom, the cause, and the before and after reproduction. **No behaviour change** (refactor): what shows that behaviour did not change (tests unchanged, outputs compared). **How this will be used** (enabling; a new feature or infra change that ships switched off): the five points below. **Rollout** (infra, enabling; a model or feature when a deploy order, migration or version skew matters): order, rollback, what to watch. **Differences from the original** (backport): link to the original, what differs and how conflicts were resolved, why this release, tested on the release branch; what the original's review covered is left out. |
| 7 | **How it works** (new feature; other types only when the mechanism is not clear from What changes) | The mechanism, with a diagram: flow, sequence, data, where it runs. One `h3` per mechanism. Tables for structured facts (what each counter or field means). |
| 8 | **Decisions** (only when a real choice was made) | One folded block per decision. Open ones first, marked `needs-decision`: the summary line is the question and the recommendation; the body has the options, what each costs, and why. Then the ones taken, marked `decided`: the summary line is what was chosen; the body has the alternatives and why. |
| 9 | **Costs and risks** (a refactor or backport only when it has one) | One folded block per cost or risk: the summary line is what it is and its handling; the body makes it clear without guessing (below). |
| 10 | **Where to look** | Files in reading order, one row per file with what to check there (its functions too); the local worktree path in a paragraph above them. Kept visible: the owner uses it every time. Past the main files, the rest may go in one plain fold. |
| 11 | **Checks** | Its body is one fold, whose summary line is the verdict: what passes, how many reviews and open findings, what is not verified; an open finding or a failed check shows there, never only inside. In the fold, three `h3`s. **Tests**: one line per test or group, or a small table (test, what it proves); what no test covers, one line. **Reviews**: each review that ran (Codex, the review bot, others), with its counts; each finding that changed code or is still open one line with its outcome: fixed in `<commit>` / deferred: reason + issue; the rejected ones in a plain fold under it, one line each with the reason. Bridge reads this part: until it names every review the install requires (`required_reviews`) the PR is not ready for the owner. **Verified and not verified**: what ran live for the PR (a preview, staging) and what it showed, marked `observed`, and what could not run there and why; what else was checked, one plain line each with its mark; what was not verified, why, and who would notice and when. |
| 12 | **Changes since the last review** | Only after the owner's own review of this PR (their GitHub review or comments on it, or a review sent from the Guild page), covering what changed since their latest one. Never for other reviewers' rounds or for updates before the owner has looked. Always last; newest first, one folded block per update: what changed in the code and in this doc; commits inside. |

**Which sections, by type.** In short, Where to look and Checks are always there, and What changes and why for every
type but docs only and backport. Then, by the strip's Type (a PR of two types takes both sets):

| Type | Signals in the PR | Needs |
|---|---|---|
| model / contract | models, schemas, OpenAPI, events, migrations or stored shapes change | Fields, What else could break with a Reuse line, Costs and risks |
| refactor | the title says refactor; tests unchanged or only moved | No behaviour change, What else could break |
| bug fix | the title says fix, or an issue with an observed symptom | Root cause, What else could break, Costs and risks |
| new feature | a new route, screen, service or job | What else could break with a Reuse line, How it works, Costs and risks |
| infra / chart | Helm values, charts, Terraform, CI, Dockerfiles | Rollout, What else could break, Costs and risks |
| docs only | only Markdown, images or a docs repo | nothing more |
| enabling | off by default, an opt-in value, step N of M | How this will be used, Rollout, What else could break, Costs and risks |
| backport | base is a release branch; cherry-picked commits | Differences from the original |

The rest (User story, Fields for other types, How it works, Decisions) comes in when its condition in the table
above holds.

Nothing comes before In short but the verdict strip: no 'changed since' or update box at the top, even right after
an update. What changed goes only in **Changes since the last review**, which exists only once the owner has
reviewed an earlier head, and stays last and folded.

In **Where to look**, each file is named once, by its full path from the repo root (`src/app/run.py`, not `run.py`,
`.../app/run.py` or a home or worktree path). Each row is a list item or table row whose first `<code>` is that path;
a line number after it (`run.py:40-52`) is fine.

`guild review doc` refuses a doc:
- with no verdict strip before **In short** (outside closed folds), a strip missing one of its five lines or with a line over 25 words
  (`doc_bullet_max_words`), or whose Type names none of the types above;
- whose first `h2` is not **In short**; that lacks a section its type needs (Fields, What else could break, Root
  cause, No behaviour change, Rollout, How this will be used, Differences from the original, as in the table above);
  or whose **What else could break**, for a model / contract or new feature PR, has no Reuse line;
- that has **Tests**, **Reviews** or **Verified and not verified** as an `h2`, no **Checks** `h2`, or no **Where to
  look** `h2`;
- that has a **User story** anywhere but as the second `h2`, or whose story has fewer than three visible top-level
  items of a numbered list (`<ol>`; bullets, a table, a closed fold's body or a nested list do not count);
- that has **Changes since the last review** before the owner's review or anywhere but last;
- whose **In short** has more than five bullets or a bullet over 25 words, that has a sentence over 30 words in the
  visible part of **In short**, **User story** (if any) and **What changes and why** (fold bodies, diagrams and code
  blocks are not counted; the limits are the install's `doc_in_short_max_bullets`, `doc_bullet_max_words` and
  `doc_sentence_max_words`), or that shows more than `doc_visible_max_words` words outside **Fields** before anything
  is opened;
- whose **Fields** has a table of fields inside a closed fold, or one with no column for what it is, why (who writes,
  who reads), limits or an example;
- with a fold whose summary starts with an id (`D3. …`) rather than its point;
- that shows a Before (a heading, a table's header row or column, a bold or labelled line, a diagram label or image
  alt text) without '<base> today' / 'on <base>', 'branch <name>' or 'hypothetical' in the same table, figure or
  paragraph, or the one just before or after it;
- with a paragraph holding three or more `Term: meaning.` (or `Term — meaning.`, `Term = meaning.`) pairs, or a
  section titled like a glossary (Words used, Terms, Glossary) whose paragraphs hold three or more together;
- whose **Where to look** names a file twice, gives a path not from the repo root, or, when the PR has a review
  worktree, a path the commit in `pr-head` does not have.

The first-reader line is read from the Verified part of **Checks** only. The checker does not check the order of the
sections past these, that **Checks** is folded or has its three parts, what a Reuse line or a strip line says, or that
In short explains its key terms: the first reader and the word budget do. Once registered, where the install has a classifier key, each visible
sentence of In short, User story and What changes and why is screened for telling the PR's history, being about the
review process, or stacking subordinate clauses; each flagged sentence is printed back quoted, as a warning. The
screen misses things and can be wrong: whether the doc reads plainly stays the writer's job.

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
  `<span class="term" data-def="One plain sentence: what it is, and where it lives in the code.">Billing Service</span>`. `data-def` is plain text (no
  markup, `&quot;` for a quote), one sentence a newcomer understands. A code name goes inside:
  `<span class="term" data-def="…"><code>billing_service</code></span>`.
- **Mark:** provenance and handling labels, small and quiet, after the claim or at the end of a summary line, never in
  a heading, and no legend anywhere. `<span class="mark observed">observed</span>`. Classes: `observed`, `reproduced`,
  `from-code` (provenance), and `decided` (a decision taken and kept), `accepted`, `fixed-here`, `deferred`,
  `rejected`, `needs-decision` (handling). The text is the class in lowercase, with a space for the hyphen.
- **Verdict strip:** before the first `h2`, `<dl class="verdict"><dt>Type</dt><dd>Model / contract, 800 lines, mostly
  models and tests</dd><dt>You decide</dt><dd>…</dd><dt>Could break</dt><dd>…</dd><dt>Main risk</dt><dd>…</dd>
  <dt>Checks</dt><dd>…</dd></dl>`. The viewer lays it out.
- **Folded block:** one per decision, cost or risk, review, update and long test detail:
  `<details class="item"><summary><b>Metrics keep 12 snapshots</b> - all-time counts come from the evidence rows
  <span class="mark decided">decided</span></summary> body: bullets, tables, evidence </details>`. Nothing but the
  summary line shows until it is opened, so that line must stand alone and start with its point.
- **Checks:** `<h2>Checks</h2><details><summary>All pass · 3 reviews, 0 open · not verified: index builds on the
  production database</summary><h3>Tests</h3> … <h3>Reviews</h3> … <h3>Verified and not verified</h3> …
  </details>`.
- **Plain folds:** a `<details>` without the class, for evidence under a bullet or a paragraph.
- **Links:** plain `<a href>`; the viewer colors them.

## Diagrams

One in **What changes and why** (before and after); **How it works**, when the doc has it, gets its own only when
the mechanism needs one. Inline SVG or Mermaid.
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
