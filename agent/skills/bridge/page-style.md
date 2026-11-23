# Page style

Every page bridge serves (`/p/<slug>`) and every review doc gets one shared style at serve time
(`src/bridge/web/doc-style.css`). Write plain HTML and it already reads like a magazine: `h1` is a tinted band, each
`h2` gets a coloured number badge (left off when your own h2s start with a number such as `1.`), the paragraph right
after `h1` is a larger lead, tables are one rounded block with zebra rows and a header that stays in view. Base rules
have zero specificity, so any rule of your own wins; write CSS only for what the classes below do not cover.

No borders or lines: blocks and rows are told apart by background. Every colour is a token with a light and a dark
value; use them in your own CSS and SVG so the page follows the reader's theme.

A few rules are forced over your CSS, so every page reads the same:

- Text is 18px, tables 0.92 of it, inline code 0.88 of its text. A size you set in px on other elements stays.
- Inline code never breaks inside a name: short code stays on one line, code over 32 characters wraps only after
  `/`, `_`, `-` (and `.` when it is not a path). A table wider than its column scrolls sideways in its own box.
- A light / dark / auto switch sits top right. The reader's choice holds for every page on the same address and
  applies before the page paints: it sets `data-theme="light|dark"` on `<html>` and turns your own
  `prefers-color-scheme` rules on or off to match. Key dark colours on either and both work; a page with no
  background of its own gets the chosen theme's canvas.

## Classes

| Class | What it is |
|---|---|
| `ds-band` (or `band`) | Header band: gradient, white text. Holds `ds-kicker`, `h1`, a `p` deck and `ds-chips`. |
| `ds-kicker` (or `kicker`) | Small upper-case line above a title, in the section's colour. |
| `ds-chips` (or `chips`) | Row of pills inside a band: its `span` and `a` children. |
| `ds-lead` (or `lead`) | Larger serif lead paragraph. |
| `ds-sec` (or `sec`) | Section header: `<header class="ds-sec"><span class="ds-num">2</span><div><span class="ds-kicker">…</span><h2>…</h2></div></header>`; its h2 gets no badge. `num` works for `ds-num`. |
| `ds-callout` (or `callout`, `box`) + `ds-info` / `ds-warn` / `ds-risk` / `ds-ok` / `ds-decision` | Tinted box; a `ds-label` (or `label`) inside is its coloured heading. A paragraph opening with bold **Risk:**, **Decision:**, **Note:** becomes one by itself. |
| `ds-pull` (or `pull`) | Pull quote: large serif line on a fading tint. |
| `ds-badge` + `keep` / `change` / `measure` / `dropped` / `risk` | Status pill. |
| `ds-stage-1` … `ds-stage-5` | Sets the colour (`--ds-sc`) of whatever it is on: a `ds-sec` number, a kicker, a `ds-stage` card, a pull quote. |
| `ds-stage` | Card tinted in its stage colour. |
| `ds-cards` | Grid of cards: each child is a tinted rounded card. |
| `ds-figure` + `svg.ds-wide` | Figure on a tint for a diagram; `ds-wide` keeps a wide SVG at 760px and scrolls sideways on a phone. |

Evidence words (FROM CODE, REPRODUCED, OBSERVED) become chips by themselves; `data-ds-skip` on an element leaves it
and its contents alone.

Colour tokens: `--ds-fg`, `--ds-muted`, `--ds-tint`, `--ds-zebra`, `--ds-s1` … `--ds-s5` (stage hues: indigo, teal,
violet, pink, amber), `--ds-red`, `--ds-green`, `--ds-blue`, `--ds-amber`, `--ds-grey`, `--ds-on-accent` (text on a
solid hue). Fonts: `--ds-serif`, `--ds-sans`.

## Example (invented)

```html
<header class="ds-band">
  <span class="ds-kicker">Discovery · week 3</span>
  <h1>Where the late orders come from</h1>
  <p>Three causes explain most late orders; one of them is cheap to fix.</p>
  <div class="ds-chips"><span>12 stores</span><span>4 weeks of data</span></div>
</header>
<p class="ds-lead">Most late orders start before the kitchen sees them.</p>
<header class="ds-sec ds-stage-2"><span class="ds-num">1</span>
  <div><span class="ds-kicker">Causes</span><h2>What we found</h2></div></header>
<div class="ds-cards">
  <div class="ds-stage ds-stage-1"><span class="ds-kicker">Intake</span><p>Orders wait in a queue. OBSERVED</p></div>
  <div class="ds-stage ds-stage-3"><span class="ds-kicker">Kitchen</span><p>Prep time is steady.</p></div>
</div>
<div class="ds-callout ds-warn"><span class="ds-label">Watch</span>Weekend data is thin.</div>
<p class="ds-pull">Fix the queue first; the kitchen is not the bottleneck.</p>
<table><tr><th>Change</th><th>Status</th></tr>
  <tr><td>Queue alert</td><td><span class="ds-badge keep">keep</span></td></tr>
  <tr><td>Second oven</td><td><span class="ds-badge dropped">dropped</span></td></tr></table>
```

## Diagram

SVG presentation attributes do not read CSS variables: put tokens in `style`, not in `fill="…"`.

```html
<figure class="ds-figure">
  <svg class="ds-wide" viewBox="0 0 520 90" role="img" aria-label="Order flow: intake, queue, kitchen">
    <rect x="10" y="20" width="140" height="50" rx="12" style="fill:color-mix(in srgb,var(--ds-s1) 16%,transparent)"/>
    <text x="80" y="50" text-anchor="middle" style="fill:var(--ds-fg);font:600 14px var(--ds-sans)">Intake</text>
    <path d="M150 45H190" style="stroke:var(--ds-muted);stroke-width:2"/>
    <rect x="190" y="20" width="140" height="50" rx="12" style="fill:color-mix(in srgb,var(--ds-red) 16%,transparent)"/>
    <text x="260" y="50" text-anchor="middle" style="fill:var(--ds-red);font:600 14px var(--ds-sans)">Queue</text>
    <path d="M330 45H370" style="stroke:var(--ds-muted);stroke-width:2"/>
    <rect x="370" y="20" width="140" height="50" rx="12" style="fill:color-mix(in srgb,var(--ds-s2) 16%,transparent)"/>
    <text x="440" y="50" text-anchor="middle" style="fill:var(--ds-fg);font:600 14px var(--ds-sans)">Kitchen</text>
  </svg>
  <figcaption>The queue is where orders wait.</figcaption>
</figure>
```
