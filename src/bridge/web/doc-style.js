/* Shared doc style: highlights code blocks and gives links, inline code and evidence words their roles, in any doc,
   without the author adding classes. Safe to run twice. */
(function () {
  "use strict";
  var HLJS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js";
  var HLJS_SRI = "sha384-RH2xi4eIQ/gjtbs9fUXM68sLSi99C7ZWBRX1vDrVv6GQXRibxXLbwO2NGZB74MbU";
  var AUTO = ["python", "typescript", "javascript", "yaml", "json", "bash", "diff", "xml", "css"];
  var LABEL = { python: "py", typescript: "ts", javascript: "js", bash: "shell", xml: "html", ini: "toml" };
  // A script that edits the doc skips diagrams, the viewer's own nav and popovers, and code.
  var SKIP = "svg,script,style,textarea,pre,code,kbd,samp,#bridge-doc-nav,#bdv-pop,.ds-chip,.mark,.tag,[data-ds-skip]";
  var root = document.documentElement;
  // What <html> carried before this script and the contents bar changed it, so a copy saved from the browser can be
  // given back as it was written.
  if (!root.hasAttribute("data-bridge-root")) {
    root.setAttribute("data-bridge-root", JSON.stringify({ c: root.getAttribute("class"), s: root.getAttribute("style"),
      t: root.getAttribute("data-theme") }));
  }

  // The reader's theme choice, kept for every page on this origin. It runs before the body is parsed, so the page
  // paints in the chosen theme: data-theme for docs that key on it, and the page's own prefers-color-scheme rules
  // rewritten to match, for docs that only follow the system.
  var THEME_KEY = "bridge-theme", OWN_THEME = root.getAttribute("data-theme");
  var SCHEME = /\(\s*prefers-color-scheme\s*:\s*(dark|light)\s*\)/gi;
  var originals = new Map();
  function storedTheme() {
    // Bridge keeps the choice and says it here: a review doc is sandboxed and has no storage of its own.
    var v = window.bridgeTheme;
    if (!v) try { v = localStorage.getItem(THEME_KEY); } catch (e) { /* private window: light */ }
    return knownTheme(v);
  }
  // With no choice made, light: dark text on a light page reads best; Auto, picked, follows the system.
  function knownTheme(v) { return v === "dark" || v === "auto" ? v : "light"; }
  function forceMedia(media, forced) {
    if (!media || !media.mediaText) return;
    var text = originals.has(media) ? originals.get(media) : media.mediaText;
    SCHEME.lastIndex = 0;
    if (!SCHEME.test(text)) return;
    originals.set(media, text);
    media.mediaText = forced ? text.replace(SCHEME, function (_, kind) {
      return kind.toLowerCase() === forced ? "(min-width:0px)" : "(max-width:0.001px)";
    }) : text;
  }
  function forceRules(rules, forced) {
    for (var i = 0; i < rules.length; i++) {
      var rule = rules[i], inner = rule.cssRules;
      forceMedia(rule.media, forced);
      if (!inner && rule.styleSheet) try { inner = rule.styleSheet.cssRules; } catch (e) { /* another origin */ }
      if (inner) forceRules(inner, forced);
    }
  }
  function applyTheme(choice) {
    var forced = choice === "auto" ? null : choice;
    if (forced) root.setAttribute("data-theme", forced);
    else if (OWN_THEME === null) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", OWN_THEME);
    [].forEach.call(document.styleSheets || [], function (sheet) {
      forceMedia(sheet.media, forced);
      var rules;
      try { rules = sheet.cssRules; } catch (e) { return; }
      if (rules) forceRules(rules, forced);
    });
    // Before the body exists only the root can be read; start() reads again with the body.
    root.classList.remove("ds-cs");
    var own = [root].concat(document.body ? [document.body] : []).some(function (el) {
      return !clear(getComputedStyle(el).backgroundColor);
    });
    if (own) return;
    root.classList.add("ds-cs");
    // Text the page coloured itself must stay readable on the new canvas, or the page keeps the default one.
    if (!document.body) return;
    var dark = forced ? forced === "dark" : !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
    if (luma(getComputedStyle(document.body).color) < 128 === dark) root.classList.remove("ds-cs");
  }
  var chosen = storedTheme();
  applyTheme(chosen);

  var THEMES = [["light", "\u2600\ufe0e", "Light"], ["dark", "\u263e", "Dark"], ["auto", "Auto", "Follow the system"]];
  function themeSwitch() {
    if (document.getElementById("ds-theme")) return;
    var bar = document.createElement("div");
    bar.id = "ds-theme";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Theme");
    bar.setAttribute("data-ds-skip", "");
    bar.setAttribute("data-bridge-added", "switch");
    THEMES.forEach(function (t) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = t[1];
      b.title = t[2];
      b.setAttribute("aria-label", t[2]);
      b.setAttribute("data-theme-choice", t[0]);
      b.addEventListener("click", function () {
        try { localStorage.setItem(THEME_KEY, t[0]); } catch (e) { /* sandboxed: bridge keeps it */ }
        if (window.fetch && location.protocol.indexOf("http") === 0) {
          fetch("/theme", { method: "POST", mode: "no-cors", credentials: "omit", body: t[0] }).catch(function () {});
        }
        switchTheme(t[0]);
      });
      bar.appendChild(b);
    });
    document.body.appendChild(bar);
    pressed(chosen);
  }
  function pressed(choice) {
    [].forEach.call(document.querySelectorAll("#ds-theme button"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-choice") === choice));
    });
  }
  function switchTheme(choice) {
    chosen = choice;
    // The palette classes the last reading left would hold the old palette while the page is read again.
    root.classList.remove("ds-light", "ds-dark", "bdv-light", "bdv-dark");
    applyTheme(choice);
    pressed(choice);
    if (!document.body) return;
    theme();
    // The contents bar and the review viewer read the page's colours again.
    document.dispatchEvent(new Event("ds-theme"));
  }

  function clear(c) { return !c || c === "transparent" || /rgba\(.*,\s*0\)$/.test(c); }
  function theme() {
    var bg = getComputedStyle(document.body).backgroundColor;
    if (clear(bg)) bg = getComputedStyle(root).backgroundColor;
    var dark;
    if (clear(bg)) {
      // No background of its own: the canvas is white unless the page opted into a dark colour scheme.
      var scheme = getComputedStyle(root).colorScheme || "";
      dark = /dark/.test(scheme) && (!/light/.test(scheme) || !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches));
    } else {
      var m = bg.match(/[\d.]+/g);
      dark = 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2] < 128;
    }
    root.classList.toggle("ds-dark", dark);
    root.classList.toggle("ds-light", !dark);
  }

  function links() {
    [].forEach.call(document.querySelectorAll("a[href]"), function (a) {
      if (a.closest("#bridge-doc-nav,svg") || a.classList.contains("ds-ext")) return;
      var url;
      try { url = new URL(a.getAttribute("href"), location.href); } catch (e) { return; }
      if (!/^https?:$/.test(url.protocol)) return;
      if (url.host === location.host || /^(127\.0\.0\.1|localhost)(:|$)/.test(url.host)) return;
      a.classList.add("ds-ext");
      if (!a.title) a.title = url.host;
    });
  }

  // Long inline code may wrap at a space, after / and after a dot, and nowhere else: never inside a name such as
  // potential_issues or doc-style. A short one stays on one line.
  var LONG_CODE = 32;
  function codeBreaks() {
    [].forEach.call(document.querySelectorAll("code"), function (c) {
      if (c.closest("pre,svg,[data-ds-skip]") || c.children.length || c.classList.contains("ds-long")) return;
      var t = c.textContent;
      // A path breaks after / only, so a file keeps its extension; a dotted name after each dot. Each part is a span
      // that never wraps: wrapping text may otherwise break after any hyphen.
      var at = c.classList.contains("ds-path") || /\//.test(t) ? /(?<=\/|\s)(?=\S)/ : /(?<=\.|\s)(?=\S)/;
      var parts = t.split(at);
      if (t.length <= LONG_CODE || parts.length < 2) return;
      c.textContent = "";
      parts.forEach(function (part, i) {
        var piece = document.createElement("span");
        // The first part keeps a path's icon on its line.
        piece.className = i ? "ds-seg" : "ds-seg ds-head";
        piece.textContent = part;
        if (i) c.appendChild(document.createElement("wbr"));
        c.appendChild(piece);
      });
      c.classList.add("ds-long");
    });
  }

  // A table or diagram wider than its column scrolls sideways in its own box; one that fits keeps a table's sticky
  // header.
  function wideTables() {
    [].forEach.call(document.querySelectorAll("table,svg"), function (t) {
      var box = t.parentElement;
      if (!box || t.closest("[data-ds-skip],#bridge-doc-nav") || (t.tagName !== "TABLE" && box.closest("svg"))) return;
      var cs = getComputedStyle(box);
      if (!box.classList.contains("ds-scroll") && !/auto|scroll/.test(cs.overflowX)) {
        if (outside(t, box) <= 1) return;
        var wrap = document.createElement("div");
        wrap.className = "ds-scroll";
        wrap.setAttribute("data-bridge-wrap", "");
        box.insertBefore(wrap, t);
        wrap.appendChild(t);
        box = wrap;
      }
      widen(box, t);
    });
  }
  // How far the block reaches past its box's content edge (its padding is not room for it).
  function outside(t, box) {
    var cs = getComputedStyle(box);
    return t.getBoundingClientRect().width - (box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight));
  }
  // A table or diagram wider than its column grows past the container to the window's edges, to the right first, then
  // to the left, never under the side contents bar; only one wider than the window scrolls, inside its own box. A doc's
  // narrow column otherwise left empty space beside a scrolling table.
  function widen(box, t) {
    var table = t.tagName === "TABLE";
    [box, t].forEach(unwiden);
    var over = outside(t, box);
    if (over <= 1 || getComputedStyle(box).direction === "rtl") return;
    var r = box.getBoundingClientRect(), lo = GUTTER, hi = root.clientWidth - GUTTER;
    var nav = document.getElementById("bridge-doc-nav");
    if (nav && nav.classList.contains("bdn-side")) lo = Math.max(lo, nav.getBoundingClientRect().right + GUTTER);
    // Never past an ancestor that clips or scrolls: the table would be cut off or scroll twice.
    for (var a = box.parentElement; a && a !== document.body && a !== root; a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== "visible") {
        var ar = a.getBoundingClientRect();
        lo = Math.max(lo, ar.left);
        hi = Math.min(hi, ar.left + a.clientWidth);
      }
    }
    var width = Math.min(Math.ceil(r.width + over), Math.floor(hi - Math.min(lo, r.left)));
    if (width <= r.width + 1) return;
    var shift = Math.max(0, Math.min(r.left - lo, r.left + width - hi));
    [box, t].forEach(function (el) { el.setAttribute("data-ds-widened", el.getAttribute("style") || ""); });
    box.style.maxWidth = "none";
    box.style.boxSizing = "border-box";
    box.style.width = width + "px";
    if (shift > 0) box.style.marginLeft = (parseFloat(getComputedStyle(box).marginLeft) || 0) - Math.ceil(shift) + "px";
    if (table) t.style.width = "100%";
  }
  // The element's own inline style back, as it was before it was widened.
  function unwiden(el) {
    if (!el.hasAttribute("data-ds-widened")) return;
    var own = el.getAttribute("data-ds-widened");
    if (own) el.setAttribute("style", own); else el.removeAttribute("style");
    el.removeAttribute("data-ds-widened");
  }
  var GUTTER = 24;

  var PATH = /^(?:[\w.@~-]+\/)+(?:[\w@-][\w.@-]*\.\w{1,5}(?::\d+(?:[-–]\d+)?)?)?$|^[\w.-]+\.(?:py|ts|tsx|js|jsx|mjs|json|ya?ml|toml|md|html|css|sh|sql|txt|cfg|ini|lock|tf|go|rs)(?::\d+(?:[-–]\d+)?)?$|^:\d+(?:[-–]\d+)?$/;
  var IDENT = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\(\.{0,3}\))?$/;
  function inlineCode() {
    [].forEach.call(document.querySelectorAll("code"), function (c) {
      if (c.closest("pre,svg,.ds-band,.band") || c.children.length || c.classList.contains("ds-path") || c.classList.contains("ds-ident")) return;
      var t = c.textContent.trim();
      if ((PATH.test(t) && !/^\//.test(t)) || /^(~|\/(home|Users|tmp|var|etc|opt|private))\/\S+$/.test(t) || (/^\/\S+\.\w{1,5}$/.test(t) && PATH.test(t.slice(1))))
        c.classList.add("ds-path");
      else if (IDENT.test(t) && (/[_.(]|[a-z][A-Z]/.test(t) || /^[A-Z][A-Z0-9_]{2,}$/.test(t))) c.classList.add("ds-ident");
    });
  }

  var KINDS = { "from code": "from-code", "reproduced": "reproduced", "observed": "observed", "not verified": "not-verified",
    "unverified": "not-verified", "decided": "decided", "deferred": "deferred", "accepted": "accepted",
    "needs decision": "needs-decision", "needs-decision": "needs-decision", "fixed here": "fixed-here", "fixed-here": "fixed-here" };
  var WORDS = /\b(FROM CODE|REPRODUCED|OBSERVED|NOT VERIFIED|UNVERIFIED)\b/g;
  function chip(el, kind) { el.classList.add("ds-chip", "ds-" + kind); }
  function chips() {
    // Elements authors already made into a label: .mark (review-doc spec), .tag, or a small element holding only the word.
    [].forEach.call(document.querySelectorAll(".mark,.tag,span,small,em,i,b,strong"), function (el) {
      if (el.classList.contains("ds-chip") || el.children.length || el.closest("svg,pre,#bridge-doc-nav,h1,h2,h3")) return;
      var kind = KINDS[el.textContent.trim().toLowerCase()];
      if (!kind && el.classList.contains("mark")) kind = Object.keys(KINDS).map(function (k) { return KINDS[k]; })
        .filter(function (k) { return el.classList.contains(k); })[0];
      var labelled = el.classList.contains("mark") || el.classList.contains("tag") || /^[A-Z ]+$/.test(el.textContent.trim());
      if (kind && labelled) chip(el, kind);
    });
    // The bare words in running text, upper case only, so prose ("we observed") stays prose.
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!WORDS.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
        WORDS.lastIndex = 0;
        return n.parentElement.closest(SKIP + ",h1,h2,h3,a,button") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
    });
    var found = [];
    while (walker.nextNode()) found.push(walker.currentNode);
    found.forEach(function (n) {
      var frag = document.createDocumentFragment(), text = n.nodeValue, at = 0, m;
      WORDS.lastIndex = 0;
      while ((m = WORDS.exec(text))) {
        frag.appendChild(document.createTextNode(text.slice(at, m.index)));
        var s = document.createElement("span");
        s.textContent = m[1];
        chip(s, KINDS[m[1].toLowerCase()]);
        frag.appendChild(s);
        at = m.index + m[1].length;
      }
      frag.appendChild(document.createTextNode(text.slice(at)));
      n.parentNode.replaceChild(frag, n);
    });
  }

  var CALLOUT = /^(risk|warning|caution|danger|decision|decided|recommendation|my pick|note|tip|important|difference from main|fixed|done)\b/i;
  var CALLOUT_KIND = { risk: "risk", warning: "risk", caution: "risk", danger: "risk", decision: "decision",
    decided: "decision", recommendation: "decision", "my pick": "decision", fixed: "ok", done: "ok" };
  function callouts() {
    [].forEach.call(document.querySelectorAll(".callout,.box"), function (el) {
      el.classList.add("ds-callout");
      ["risk", "decision", "ok", "warn", "info"].forEach(function (k) { if (el.classList.contains(k)) el.classList.add("ds-" + k); });
    });
    [].forEach.call(document.querySelectorAll("p,blockquote>p:first-child"), function (p) {
      if (p.closest("li,td,th,summary,svg,.ds-callout,#bridge-doc-nav")) return;
      var first = p.firstElementChild, lead = p.firstChild;
      while (lead && lead.nodeType === 3 && !lead.nodeValue.trim()) lead = lead.nextSibling;
      if (!first || first !== lead || !/^(B|STRONG)$/.test(first.tagName)) return;
      var m = first.textContent.trim().match(CALLOUT);
      if (!m) return;
      var box = p.parentElement.tagName === "BLOCKQUOTE" ? p.parentElement : p;
      box.classList.add("ds-callout");
      var kind = CALLOUT_KIND[m[1].toLowerCase()];
      if (kind) box.classList.add("ds-" + kind);
    });
  }

  function guess(text) {
    var lines = text.split("\n"), first = lines[0];
    var marked = lines.filter(function (l) { return /^[+-](?![+-]{2} )/.test(l); }).length;
    if (/^(diff --git|@@ |--- |\+\+\+ )/m.test(text) || (marked >= 2 && lines.every(function (l) { return /^[+\- ]|^$/.test(l); })))
      return "diff";
    if (/^\s*(#|\/\/)\s*\S+\.py\b/.test(first)) return "python";
    if (/^\s*\/\/\s*\S+\.tsx?\b/.test(first)) return "typescript";
    if (/^\s*(async def |def |class \w+[:(]|from [\w.]+ import |import [\w.]+$|@[\w.]+(\(|$))/m.test(text) ||
      /^\w+ = [\w.]+\($/m.test(text)) return "python";
    if (/^\s*[\[{]/.test(text)) { try { JSON.parse(text); return "json"; } catch (e) { /* not JSON */ } }
    if (/^\s*\$ |^\s*(git|gh|npm|npx|bridge|kubectl|helm|curl|cd|python3?|uv|aws|docker|pytest) /m.test(first)) return "bash";
    return null;
  }

  function blocks() {
    var list = [].filter.call(document.querySelectorAll("pre"), function (pre) {
      return !pre.closest("svg,[data-ds-skip]") && !pre.classList.contains("mermaid") && !pre.hasAttribute("data-ds-lang");
    });
    if (!list.length) return;
    load(function (hljs) {
      list.forEach(function (pre) {
        var code = pre.querySelector(":scope>code") || pre;
        if (code.children.length || /\bno-?highlight\b/.test(code.className + " " + pre.className)) return;
        var named = (code.className + " " + pre.className).match(/\b(?:language|lang)-([\w+-]+)/);
        var text = code.textContent, lang = named && hljs.getLanguage(named[1]) ? named[1] : guess(text), result;
        try {
          result = lang ? hljs.highlight(text, { language: lang, ignoreIllegals: true }) : hljs.highlightAuto(text, AUTO);
        } catch (e) { return; }
        if (!result.language || (!lang && result.relevance < 3)) return;
        code.innerHTML = result.value;
        var name = result.language;
        pre.setAttribute("data-ds-lang", LABEL[name] || name);
        if (name === "diff") pre.classList.add("ds-diff");
      });
    });
  }

  function load(then) {
    if (window.hljs) return then(window.hljs);
    var s = document.createElement("script");
    s.src = HLJS;
    s.integrity = HLJS_SRI;
    s.crossOrigin = "anonymous";
    s.setAttribute("data-bridge-added", "highlighter");
    // Offline or blocked: code stays readable, just not coloured.
    s.onload = function () { if (window.hljs) then(window.hljs); };
    document.head.appendChild(s);
  }

  // Boxes drawn by a border get a background instead; a border-bottom underline under inline text becomes a dotted
  // text underline. The CSS removes every border; this reads them with that rule lifted, in one style pass.
  var BOXLESS = /^(TABLE|THEAD|TBODY|TFOOT|TR|TD|TH|PRE|IMG|IFRAME|VIDEO|CANVAS|INPUT|SELECT|TEXTAREA|HR|BUTTON)$/;
  function opaque(c) { return !clear(c); }
  function luma(c) { var m = c.match(/[\d.]+/g) || [255, 255, 255]; return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]; }
  function behind(el) {
    for (var p = el.parentElement; p; p = p.parentElement) {
      var bg = getComputedStyle(p).backgroundColor;
      if (opaque(bg)) return bg;
    }
    return getComputedStyle(root).backgroundColor;
  }
  function borders() {
    root.classList.add("ds-measure");
    var all = document.body.getElementsByTagName("*"), tint = [], under = [], keep = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!(el instanceof HTMLElement) || BOXLESS.test(el.tagName) ||
        el.closest("svg,table,#bridge-doc-nav,#bdv-pop,[data-ds-skip]")) continue;
      var cs = getComputedStyle(el), on = ["Top", "Right", "Bottom", "Left"].map(function (s) {
        return cs["border" + s + "Style"] !== "none" && parseFloat(cs["border" + s + "Width"]) > 0;
      });
      if (!(on[0] || on[1] || on[2] || on[3])) continue;
      // An empty element drawn by its borders (a CSS triangle, a connector line) is a picture, not a frame.
      if (!el.firstElementChild && !el.textContent.trim()) keep.push(el);
      else if (on[0] && on[1] && on[2] && on[3]) {
        if (!opaque(cs.backgroundColor) || Math.abs(luma(cs.backgroundColor) - luma(behind(el))) < 10) tint.push(el);
      } else if (on[2] && !on[0] && !on[1] && !on[3] && cs.display === "inline") under.push(el);
    }
    root.classList.remove("ds-measure");
    tint.forEach(function (el) { el.classList.add("ds-tint"); });
    under.forEach(function (el) { el.classList.add("ds-under"); });
    keep.forEach(function (el) { el.classList.add("ds-keep"); });
  }

  // A doc that numbers its own h2s keeps its numbers, without a badge counting beside them.
  function ownNumbers() {
    var own = [].some.call(document.querySelectorAll("h2"), function (h) {
      return /^\s*(\d{1,3}|[IVX]{1,5})[.):]\s/.test(h.textContent) || !!h.querySelector(".ds-num,.num");
    });
    root.classList.toggle("ds-own-num", own);
  }

  function stickyTop() {
    var nav = document.getElementById("bridge-doc-nav");
    // The side bar is fixed too, but full height: only a bar across the top covers table headers.
    var top = nav && /sticky|fixed/.test(getComputedStyle(nav).position) && nav.offsetWidth > root.clientWidth / 2;
    root.style.setProperty("--ds-sticky-top", top ? nav.offsetHeight + "px" : "0px");
  }

  // A body whose own margins are both zero (a reset) would sit at the window's left edge once the container is
  // narrower than the window: it is centred. Margins a page set to make room for something are left alone.
  function centre() {
    var b = document.body, cs = getComputedStyle(b);
    if (parseFloat(cs.marginLeft) === 0 && parseFloat(cs.marginRight) === 0 && b.offsetWidth < root.clientWidth - 1)
      b.classList.add("ds-center");
  }

  function start() {
    if (root.hasAttribute("data-ds-done")) return;
    root.setAttribute("data-ds-done", "");
    applyTheme(chosen);
    centre(); theme(); borders(); ownNumbers(); links(); inlineCode(); codeBreaks(); chips(); callouts(); blocks(); stickyTop();
    wideTables(); themeSwitch();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  // Stylesheets that arrived after the first pass get the chosen theme too.
  window.addEventListener("load", function () { applyTheme(chosen); theme(); stickyTop(); });
  window.addEventListener("resize", function () { stickyTop(); wideTables(); });
  // A table in a closed fold has no width until the fold opens.
  window.addEventListener("toggle", function () { wideTables(); }, true);
  window.addEventListener("storage", function (e) {
    if (e.key === THEME_KEY) switchTheme(knownTheme(e.newValue));
  });
  var dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
  if (dark && dark.addEventListener) dark.addEventListener("change", function () { setTimeout(theme, 0); });
})();
