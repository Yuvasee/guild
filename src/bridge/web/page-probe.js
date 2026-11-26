/* Runs in a headless render of a page for `bridge page check`: measures how wide its main column is and reads the
   background and text colours of its diagrams, then writes them as JSON into #bridge-probe-out for bridge to judge. */
(function () {
  "use strict";
  var root = document.documentElement, canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  var ctx = canvas.getContext("2d", { willReadFrequently: true });
  var OURS = "#bridge-doc-nav,#ds-theme,#bdv-pop,#bridge-probe-out";
  var TEXTISH = /^(text|tspan|textPath|line|polyline|g|defs|marker|title|desc)$/;
  var MAX_TEXTS = 400, MIN_CONTRAST = window.bridgeProbeMinContrast || 3, MIN_TEXT_PX = window.bridgeProbeMinTextPx || 14;
  // A stroke this wide on screen, and at least this share of the font size, rings each glyph: 2 px at 13 px.
  var HALO_MIN_PX = 2, HALO_FONT_SHARE = 0.15;

  // Any CSS colour as [r, g, b, a 0..1]; null when it is not one (a gradient, a url()).
  function rgba(c) {
    if (!c || c === "none" || /url\(|gradient/.test(c)) return null;
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = "rgba(0, 0, 0, 0)";
    ctx.fillStyle = c;
    ctx.fillRect(0, 0, 1, 1);
    var d = ctx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3] / 255];
  }
  function over(top, under) {
    var a = top[3];
    return [0, 1, 2].map(function (i) { return top[i] * a + under[i] * (1 - a); }).concat([1]);
  }
  function hex(c) {
    return "#" + c.slice(0, 3).map(function (v) { return ("0" + Math.round(v).toString(16)).slice(-2); }).join("");
  }
  function lum(c) {
    var v = c.slice(0, 3).map(function (x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function ratio(a, b) {
    var x = lum(a), y = lum(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }
  // The canvas under everything: the root's background, else the browser's for the page's colour scheme.
  function canvasColour() {
    var dark = root.classList.contains("ds-dark");
    return dark ? [18, 18, 18, 1] : [255, 255, 255, 1];
  }
  // What an element paints over its box: [r, g, b, a], a transparent one when nothing, null when it cannot be read.
  function paint(el) {
    var cs = getComputedStyle(el);
    if (el instanceof SVGElement && el.tagName.toLowerCase() !== "svg") {
      if (TEXTISH.test(el.tagName)) return [0, 0, 0, 0];
      if (cs.fill === "none") return [0, 0, 0, 0];
      var f = rgba(cs.fill);
      if (!f) return null;
      f[3] *= parseFloat(cs.fillOpacity) * parseFloat(cs.opacity);
      return f;
    }
    if (cs.backgroundImage && cs.backgroundImage !== "none") return null;
    return rgba(cs.backgroundColor) || [0, 0, 0, 0];
  }
  // The colour seen at a point under whatever `skip` leaves out; null when a gradient or image is in the way.
  function backgroundAt(x, y, skip) {
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
    var stack = document.elementsFromPoint(x, y).filter(function (el) { return !el.closest(OURS) && !(skip && skip(el)); });
    var c = canvasColour();
    for (var i = stack.length - 1; i >= 0; i--) {
      var p = paint(stack[i]);
      if (!p) return null;
      if (p[3] > 0) c = over(p, c);
    }
    return c;
  }
  function pageColour() {
    return [root, document.body].reduce(function (c, el) {
      var p = paint(el);
      return p && p[3] > 0 ? over(p, c) : c;
    }, canvasColour());
  }
  function heading(el) {
    var all = document.querySelectorAll("h1,h2,h3"), last = "";
    for (var i = 0; i < all.length; i++) {
      if (all[i].closest(OURS)) continue;
      if (all[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) last = all[i].textContent;
      else break;
    }
    return last.replace(/\s+/g, " ").trim().slice(0, 80);
  }
  function label(el) {
    var named = el.getAttribute("aria-label") || (el.querySelector("title,figcaption") || {}).textContent ||
      el.innerText || el.textContent;
    return (named || "").replace(/\s+/g, " ").trim().slice(0, 60);
  }
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
  }

  function column() {
    // The text, not the wide blocks that scroll in their own box.
    var blocks = document.body.querySelectorAll("h1,h2,h3,p,li,blockquote,dl");
    var left = Infinity, right = -Infinity;
    [].forEach.call(blocks, function (el) {
      if (el.closest(OURS + ",table,pre,svg,figure,.ds-scroll") || !visible(el)) return;
      if (/fixed|sticky/.test(getComputedStyle(el).position)) return;
      var r = el.getBoundingClientRect();
      left = Math.min(left, r.left);
      right = Math.max(right, r.right);
    });
    return { left: left === Infinity ? 0 : Math.round(left), right: right === -Infinity ? 0 : Math.round(right),
      window: root.clientWidth, page: root.scrollWidth };
  }

  function areas(page) {
    var found = [];
    var picked = [].filter.call(document.body.querySelectorAll("svg,figure,.mermaid,.ds-figure"), function (el) {
      if (el.closest(OURS) || !visible(el)) return false;
      if (el.tagName.toLowerCase() === "svg") {
        var r = el.getBoundingClientRect();
        return !(el.parentElement && el.parentElement.closest("svg")) && r.width >= 120 && r.height >= 60;
      }
      return !el.querySelector("svg") && !el.closest("svg");
    });
    picked.forEach(function (el) {
      el.scrollIntoView({ block: "center" });
      var r = el.getBoundingClientRect(), off = 0, seen = 0, at = null;
      [0.1, 0.5, 0.9].forEach(function (fx) {
        [0.1, 0.5, 0.9].forEach(function (fy) {
          var c = backgroundAt(r.left + r.width * fx, r.top + r.height * fy, function (n) { return TEXTISH.test(n.tagName); });
          if (!c) return;
          seen++;
          if (ratio(c, page) >= 3) { off++; at = at || c; }
        });
      });
      var item = { label: label(el), heading: heading(el), samples: seen, off: off, bg: at ? hex(at) : "", texts: [], small: [] };
      texts(el, item);
      found.push(item);
    });
    return found;
  }

  // Text drawn over its own wide stroke (paint-order: stroke) is read against that stroke, not what lies under it.
  function halo(el, cs) {
    if (!/^stroke/.test(cs.paintOrder)) return null;
    var c = rgba(cs.stroke);
    if (!c) return null;
    c[3] *= parseFloat(cs.strokeOpacity);
    var m = el.getScreenCTM(), scale = m ? Math.sqrt(Math.abs(m.a * m.d - m.b * m.c)) : 1;
    var width = parseFloat(cs.strokeWidth) * scale, size = parseFloat(cs.fontSize) * scale;
    return c[3] > 0 && width >= Math.max(HALO_MIN_PX, size * HALO_FONT_SHARE) ? c : null;
  }

  var counted = 0;
  function texts(area, item) {
    var svg = area.tagName.toLowerCase() === "svg";
    var list = svg ? area.querySelectorAll("text") : [].filter.call(area.querySelectorAll("*"), function (el) {
      return [].some.call(el.childNodes, function (n) { return n.nodeType === 3 && n.nodeValue.trim(); });
    });
    [].forEach.call(list, function (el) {
      if (counted >= MAX_TEXTS || !visible(el)) return;
      counted++;
      var r = el.getBoundingClientRect();
      if (r.top < 0 || r.bottom > innerHeight) { el.scrollIntoView({ block: "center" }); r = el.getBoundingClientRect(); }
      var cs = getComputedStyle(el);
      if (svg) {
        // The size on screen: the computed font size (class rules, inheritance and units resolved) times the svg's scale.
        var m = el.getScreenCTM(), px = parseFloat(cs.fontSize) * (m ? Math.sqrt(Math.abs(m.a * m.d - m.b * m.c)) : 1);
        if (px < MIN_TEXT_PX - 0.5) {
          item.small.push({ text: el.textContent.replace(/\s+/g, " ").trim().slice(0, 40), px: Math.round(px * 10) / 10 });
        }
      }
      var fg = rgba(svg ? cs.fill : cs.color);
      if (!fg) return;
      if (svg) fg[3] *= parseFloat(cs.fillOpacity);
      var bg = backgroundAt(r.left + r.width / 2, r.top + r.height / 2, svg ? function (n) {
        return TEXTISH.test(n.tagName);
      } : null);
      if (!bg || fg[3] === 0) return;
      var ring = svg ? halo(el, cs) : null;
      if (ring) bg = over(ring, bg);
      var seen = over(fg, bg), contrast = ratio(seen, bg);
      if (contrast < MIN_CONTRAST) {
        item.texts.push({ text: el.textContent.replace(/\s+/g, " ").trim().slice(0, 40), ratio: Math.round(contrast * 100) / 100,
          fg: hex(seen), bg: hex(bg) });
      }
    });
  }

  function run() {
    var page = pageColour(), out = { theme: root.classList.contains("ds-dark") ? "dark" : "light", page: hex(page) };
    try {
      out.column = column();
      out.areas = areas(page);
    } catch (e) {
      out.error = String(e && e.message || e);
    }
    var s = document.createElement("script");
    s.type = "application/json";
    s.id = "bridge-probe-out";
    s.textContent = JSON.stringify(out).replace(/</g, "\\u003c");
    document.body.appendChild(s);
  }
  function later() { setTimeout(run, 400); }
  if (document.readyState === "complete") later(); else window.addEventListener("load", later);
})();
