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

  function clear(c) { return !c || c === "transparent" || /rgba\(.*,\s*0\)$/.test(c); }
  function theme() {
    var bg = getComputedStyle(document.body).backgroundColor;
    if (clear(bg)) bg = getComputedStyle(root).backgroundColor;
    if (clear(bg)) { root.classList.remove("ds-dark", "ds-light"); return; }
    var m = bg.match(/[\d.]+/g), dark = 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2] < 128;
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

  var PATH = /^(?:[\w.@~-]+\/)+(?:[\w@-][\w.@-]*\.\w{1,5}(?::\d+(?:[-–]\d+)?)?)?$|^[\w.-]+\.(?:py|ts|tsx|js|jsx|mjs|json|ya?ml|toml|md|html|css|sh|sql|txt|cfg|ini|lock|tf|go|rs)(?::\d+(?:[-–]\d+)?)?$|^:\d+(?:[-–]\d+)?$/;
  var IDENT = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\(\.{0,3}\))?$/;
  function inlineCode() {
    [].forEach.call(document.querySelectorAll("code"), function (c) {
      if (c.closest("pre,svg") || c.children.length || c.classList.contains("ds-path") || c.classList.contains("ds-ident")) return;
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
    [].forEach.call(document.querySelectorAll(".callout"), function (el) {
      el.classList.add("ds-callout");
      ["risk", "decision", "ok"].forEach(function (k) { if (el.classList.contains(k)) el.classList.add("ds-" + k); });
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

  function stickyTop() {
    var nav = document.getElementById("bridge-doc-nav");
    // The side bar is fixed too, but full height: only a bar across the top covers table headers.
    var top = nav && /sticky|fixed/.test(getComputedStyle(nav).position) && nav.offsetWidth > root.clientWidth / 2;
    root.style.setProperty("--ds-sticky-top", top ? nav.offsetHeight + "px" : "0px");
  }

  function start() {
    if (root.hasAttribute("data-ds-done")) return;
    root.setAttribute("data-ds-done", "");
    theme(); borders(); links(); inlineCode(); chips(); callouts(); blocks(); stickyTop();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  window.addEventListener("load", function () { theme(); stickyTop(); });
  window.addEventListener("resize", stickyTop);
  var dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
  if (dark && dark.addEventListener) dark.addEventListener("change", function () { setTimeout(theme, 0); });
})();
