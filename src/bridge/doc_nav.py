import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

NAV_ID = "bridge-doc-nav"
VIEW_ID = "bridge-doc-view"
TERMS_FLAG = "data-viewer-terms"
MIN_HEADINGS = 2
NAV_MAX_BYTES = 2_000_000
_HEAD_END_RE = re.compile(r"</head\s*>", re.I)
_INERT_TAGS = ("template", "textarea")
_RAW_TAGS = ("script", "style")

_STYLE = """
#bridge-doc-nav,#bridge-doc-nav *{all:revert;box-sizing:border-box}
#bridge-doc-nav{--bdn-bg:Canvas;--bdn-fg:CanvasText;--bdn-acc:var(--bdv-link,#2a5db0);
--bdn-line:color-mix(in srgb,var(--bdn-fg) 16%,transparent);color-scheme:light dark;display:block;position:sticky;top:0;
z-index:2147483000;margin:0;padding:0;background:var(--bdn-bg);color:var(--bdn-fg);
font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;border-bottom:1px solid var(--bdn-line);
box-shadow:0 2px 10px rgba(0,0,0,.12)}
#bridge-doc-nav summary{display:flex;gap:10px;align-items:center;padding:8px 12px;cursor:pointer;list-style:none;
user-select:none}
#bridge-doc-nav summary::-webkit-details-marker{display:none}
#bridge-doc-nav .bdn-label{flex:none;font-weight:600;color:var(--bdn-acc);border:1px solid currentColor;
border-radius:7px;padding:4px 12px}
#bridge-doc-nav .bdn-label::before{content:"\\2630\\00a0\\00a0"}
#bridge-doc-nav details[open] .bdn-label{background:color-mix(in srgb,var(--bdn-acc) 14%,transparent)}
#bridge-doc-nav .bdn-cur{font-weight:600;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;min-width:0;flex:1}
#bridge-doc-nav ol{list-style:none;margin:0;padding:6px 8px 10px;max-height:min(70vh,560px);overflow:auto}
#bridge-doc-nav:not(.bdn-side) ol{position:absolute;left:0;right:0;top:100%;background:var(--bdn-bg);
border-bottom:1px solid var(--bdn-line);box-shadow:0 10px 18px rgba(0,0,0,.18)}
#bridge-doc-nav li{margin:0;padding:0}
#bridge-doc-nav a{display:block;padding:5px 10px;border-left:3px solid transparent;border-radius:0 6px 6px 0;
color:inherit;text-decoration:none;overflow-wrap:anywhere}
#bridge-doc-nav li.bdn-h2 a{font-weight:600;margin-top:2px}
#bridge-doc-nav li.bdn-h3 a{padding-left:24px;font-size:13px;opacity:.8}
#bridge-doc-nav a:hover{background:color-mix(in srgb,var(--bdn-fg) 8%,transparent)}
#bridge-doc-nav a.bdn-on{border-left-color:var(--bdn-acc);color:var(--bdn-acc);opacity:1;
background:color-mix(in srgb,var(--bdn-acc) 12%,transparent)}
#bridge-doc-nav.bdn-top{position:fixed;top:0;left:0;right:0}
#bridge-doc-nav.bdn-side{position:fixed;top:0;left:0;bottom:0;width:var(--bdn-w,240px);overflow:auto;
padding:14px 8px 24px;border-bottom:0;border-right:1px solid var(--bdn-line);box-shadow:none;
background:color-mix(in srgb,var(--bdn-fg) 4%,var(--bdn-bg))}
#bridge-doc-nav.bdn-side summary{cursor:default;padding:2px 10px 10px}
#bridge-doc-nav.bdn-side details .bdn-label{border:0;padding:0;color:inherit;font-size:12px;letter-spacing:.08em;
text-transform:uppercase;background:none;opacity:.75}
#bridge-doc-nav.bdn-side .bdn-label::before,#bridge-doc-nav.bdn-side .bdn-cur{display:none}
#bridge-doc-nav.bdn-side ol{max-height:none;overflow:visible;padding:0}
h2[id],h3[id]{scroll-margin-top:64px}
h1,h2,h3,h4,:not(pre)>code{overflow-wrap:anywhere}
@media print{#bridge-doc-nav{display:none}}
"""

_SCRIPT = """
(function(){
var nav=document.getElementById("bridge-doc-nav");if(!nav)return;
var html=document.documentElement,det=nav.querySelector("details"),cur=nav.querySelector(".bdn-cur");
var links=[].slice.call(nav.querySelectorAll("a[href^='#']"));
var heads=[],SIDE_MIN=880,GAP=24,base=null,own=null;
function clear(c){return !c||c==="transparent"||/rgba\\(.*,\\s*0\\)$/.test(c)}
function paint(){var b=getComputedStyle(document.body),root=getComputedStyle(html);
var bg=b.backgroundColor;nav.style.colorScheme=root.colorScheme||"normal";if(clear(bg))bg=root.backgroundColor;
if(!clear(bg))nav.style.setProperty("--bdn-bg",bg);nav.style.setProperty("--bdn-fg",b.color)}
function contentLeft(){var left=Infinity,els=document.body.querySelectorAll("h1,h2,h3,p,table,pre,ul,ol,figure,svg");
for(var i=0;i<els.length;i++){if(nav.contains(els[i]))continue;var r=els[i].getBoundingClientRect();
if(r.width>0&&r.height>0&&r.left<left)left=r.left}return left===Infinity?0:left}
function unshift(){var s=html.style;if(own){s.paddingLeft=own[0];s.paddingTop=own[1];s.boxSizing=own[2]}}
function layout(){if(base===null){var cs=getComputedStyle(html),s=html.style;
own=[s.paddingLeft,s.paddingTop,s.boxSizing];base=[parseFloat(cs.paddingLeft)||0,parseFloat(cs.paddingTop)||0]}
unshift();
var side=window.innerWidth>=SIDE_MIN;
nav.classList.toggle("bdn-side",side);nav.classList.toggle("bdn-top",!side);det.open=side;
if(side){var free=contentLeft()-GAP,w=Math.max(220,Math.min(280,free));nav.style.setProperty("--bdn-w",w+"px");
if(free<w){html.style.paddingLeft=base[0]+w+GAP+"px";html.style.boxSizing="border-box"}}
else html.style.paddingTop=base[1]+nav.offsetHeight+"px"}
function track(){var on=-1;for(var i=0;i<heads.length;i++){if(heads[i]&&heads[i].getBoundingClientRect().top<=72)on=i}
links.forEach(function(a,i){a.classList.toggle("bdn-on",i===on)});
var h2=on;while(h2>0&&links[h2].parentNode.className!=="bdn-h2")h2--;
cur.textContent=on<0?"":links[h2].textContent}
var queued=false;function soon(){if(!queued){queued=true;setTimeout(function(){queued=false;track()},60)}}
function side(){return nav.classList.contains("bdn-side")}
det.querySelector("summary").addEventListener("click",function(e){if(side())e.preventDefault()});
links.forEach(function(a){a.addEventListener("click",function(){if(!side())det.open=false})});
document.addEventListener("click",function(e){if(!side()&&det.open&&!nav.contains(e.target))det.open=false});
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&!side()&&det.open){det.open=false;
nav.querySelector("summary").focus()}});
function start(){heads=links.map(function(a){return document.getElementById(a.getAttribute("href").slice(1))});
paint();layout();track()}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",start);else start();
window.addEventListener("scroll",soon,{passive:true});
window.addEventListener("resize",function(){layout();soon()});
window.addEventListener("load",function(){paint();layout();track()});
window.addEventListener("beforeprint",unshift);window.addEventListener("afterprint",layout);
var dark=window.matchMedia&&matchMedia("(prefers-color-scheme: dark)");
if(dark&&dark.addEventListener)dark.addEventListener("change",function(){setTimeout(paint,0)});
})();
"""

_LIGHT = (
    "--bdv-link:#2a5db0;--bdv-pop-bg:#ffffff;--bdv-pop-fg:#1d1d1b;--bdv-line:rgba(0,0,0,.14);--bdv-red:#b3261e;"
    "--bdv-green:#1e7a46;--bdv-blue:#2f5d8a;--bdv-violet:#5b4a9e;--bdv-grey:#5f6368;--bdv-amber:#9a5b00"
)
_DARK = (
    "--bdv-link:#8ab4f8;--bdv-pop-bg:#26282c;--bdv-pop-fg:#ece9e4;--bdv-line:rgba(255,255,255,.16);--bdv-red:#f2a49e;"
    "--bdv-green:#7dd3a0;--bdv-blue:#a8c4ef;--bdv-violet:#c5b5f5;--bdv-grey:#b0b4b9;--bdv-amber:#f0c27a"
)
# Zero-specificity :where() rules lose to any rule of the doc's own, so older docs that style these keep their look.
_VIEW_STYLE = (
    f":root{{{_LIGHT};--bdv-ind:18px}}"
    f"@media (prefers-color-scheme:dark){{:root:not(.bdv-light){{{_DARK}}}}}:root.bdv-dark{{{_DARK}}}"
    """
@media (max-width:600px){:root{--bdv-ind:10px}}
:where(a:link,a:visited){color:var(--bdv-link);text-decoration-thickness:1px;text-underline-offset:2px}
:where(a:hover){text-decoration-thickness:2px}
:where(h2){font-size:1.5em;line-height:1.25;margin:2.2em 0 .6em;padding-bottom:.3em;
border-bottom:1px solid var(--bdv-line)}
:where(h3){font-size:1.17em;line-height:1.3;margin:1.6em 0 .5em}
.bdv-in2{margin-left:var(--bdv-ind)}
.bdv-in3{margin-left:calc(2*var(--bdv-ind))}
:where(details:not(#bridge-doc-nav details)>summary){cursor:pointer;list-style:none;font-weight:600}
:where(details:not(#bridge-doc-nav details)>summary)::-webkit-details-marker{display:none}
:where(details:not(#bridge-doc-nav details)>summary)::before{content:"";display:inline-block;width:.42em;height:.42em;
border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(-45deg);
margin:0 .65em .12em .1em;transition:transform .15s;opacity:.65}
:where(details[open]:not(#bridge-doc-nav details)>summary)::before{transform:rotate(45deg)}
:where(details.item){border-top:1px solid var(--bdv-line);padding:.55em 0}
:where(details.item>:not(summary)){margin-left:1.3em}
:where(details.item[open]>summary){margin-bottom:.4em}
:where(details.item>summary){font-weight:400}
:where(details.item>summary b,details.item>summary strong){font-weight:700}
.mark{--c:var(--bdv-grey);display:inline-block;font:600 .72em/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:0 .55em;border-radius:999px;color:var(--c);background:color-mix(in srgb,var(--c) 13%,transparent);
white-space:nowrap;vertical-align:.1em;letter-spacing:.01em;text-transform:lowercase}
.mark.observed{--c:var(--bdv-red)}.mark.reproduced,.mark.fixed-here{--c:var(--bdv-green)}
.mark.from-code{--c:var(--bdv-blue)}.mark.decided{--c:var(--bdv-violet)}.mark.deferred{--c:var(--bdv-amber)}
.mark.needs-decision{--c:var(--bdv-amber);background:color-mix(in srgb,var(--c) 22%,transparent)}
.term[data-def]{text-decoration:underline dashed;text-decoration-thickness:1px;text-underline-offset:.22em;
text-decoration-color:color-mix(in srgb,currentColor 60%,transparent);cursor:pointer;border-radius:3px}
.term[data-def]:hover,.term[data-def][aria-expanded=true]{text-decoration-color:currentColor;
background:color-mix(in srgb,var(--bdv-link) 12%,transparent)}
.term[data-def]:focus-visible{outline:2px solid var(--bdv-link);outline-offset:1px}
#bdv-pop{box-sizing:border-box;position:fixed;z-index:2147483600;
max-width:340px;max-height:calc(100vh - 16px);overflow:auto;
padding:8px 12px;border-radius:8px;
background:var(--bdv-pop-bg);color:var(--bdv-pop-fg);border:1px solid var(--bdv-line);
box-shadow:0 6px 20px rgba(0,0,0,.2);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;text-align:left;
white-space:normal;overflow-wrap:anywhere}
#bdv-pop[hidden]{display:none}
@media print{#bdv-pop{display:none}}
"""
)

_VIEW_SCRIPT = """
(function(){
var root=document.documentElement,pop=null,owner=null,SKIP=/^(SCRIPT|STYLE|TEMPLATE|NAV|LINK|META)$/;
function clear(c){return !c||c==="transparent"||/rgba\\(.*,\\s*0\\)$/.test(c)}
function theme(){var bg=getComputedStyle(document.body).backgroundColor;
if(clear(bg))bg=getComputedStyle(root).backgroundColor;
var m=clear(bg)?null:bg.match(/[\\d.]+/g),dark=!!m&&(.2126*m[0]+.7152*m[1]+.0722*m[2])<128;
root.classList.toggle("bdv-dark",dark);root.classList.toggle("bdv-light",!dark)}
function indent(){var parents=[];[].forEach.call(document.body.querySelectorAll("h2,h3"),function(h){var p=h.parentNode;
if(parents.indexOf(p)<0&&!h.closest("#bridge-doc-nav,details,table,li"))parents.push(p)});
var picked=[];parents.forEach(function(p){var lvl=0,under=false;[].forEach.call(p.children,function(el){
var t=el.tagName;
if(t==="H1"){lvl=0;under=false}else if(t==="H2"){lvl=2;under=true}else if(t==="H3"){
lvl=3;if(under)picked.push([el,"bdv-in2",p])}
else if(lvl&&!SKIP.test(t)&&el.id!=="bdv-pop")picked.push([el,lvl===3&&under?"bdv-in3":"bdv-in2",p])})});
picked=picked.filter(function(x){return getComputedStyle(x[0]).marginLeft==="0px"});
picked.forEach(function(x){x[0].classList.add(x[1])});
picked.forEach(function(x){var r=x[2].getBoundingClientRect(),cs=getComputedStyle(x[2]);
x.push(r.right-(parseFloat(cs.paddingRight)||0)-(parseFloat(cs.borderRightWidth)||0)+1,x[0].getBoundingClientRect().right)});
picked.forEach(function(x){var el=x[0];
x.push(getComputedStyle(el).overflowX==="visible"&&el.scrollWidth>el.clientWidth+1)});
picked.forEach(function(x){if(x[4]>x[3]||x[5])x[0].classList.remove(x[1])})}
function terms(){[].forEach.call(document.querySelectorAll(".term[data-def]"),function(t){
if(!t.hasAttribute("tabindex"))t.tabIndex=0;t.setAttribute("role","button");t.setAttribute("aria-expanded","false")})}
function place(){if(!owner)return;var rs=owner.getClientRects(),r=rs.length?rs[0]:owner.getBoundingClientRect();
var vw=root.clientWidth,vh=window.innerHeight;pop.style.maxWidth=Math.min(340,vw-24)+"px";
var w=pop.offsetWidth,h=pop.offsetHeight,x=Math.max(12,Math.min(r.left,vw-w-12)),y=r.bottom+6;
if(y+h>vh-8)y=r.top-h-6>=8?r.top-h-6:Math.max(8,vh-h-8);pop.style.left=x+"px";pop.style.top=y+"px"}
function close(){if(!owner)return;owner.setAttribute("aria-expanded","false");owner.removeAttribute("aria-describedby");
owner=null;pop.hidden=true}
function open(t){if(!pop){pop=document.createElement("div");pop.id="bdv-pop";pop.setAttribute("role","tooltip");
document.body.appendChild(pop)}close();owner=t;pop.textContent=t.getAttribute("data-def");pop.hidden=false;
t.setAttribute("aria-expanded","true");t.setAttribute("aria-describedby","bdv-pop");place()}
function termOf(e){var n=e.target;return n&&n.closest?n.closest(".term[data-def]"):null}
document.addEventListener("click",function(e){
var t=termOf(e);if(t){e.preventDefault();if(t===owner)close();else open(t)}
else if(owner&&!pop.contains(e.target))close()});
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&owner){var o=owner;close();o.focus();return}
var t=termOf(e);if(t&&(e.key==="Enter"||e.key===" ")){e.preventDefault();if(t===owner)close();else open(t)}});
document.addEventListener("scroll",place,{passive:true,capture:true});window.addEventListener("resize",place);
function start(){theme();terms();indent()}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",start);else start();
window.addEventListener("load",theme);
var dark=window.matchMedia&&matchMedia("(prefers-color-scheme: dark)");
if(dark&&dark.addEventListener)dark.addEventListener("change",function(){setTimeout(theme,0)});
})();
"""


@dataclass
class _Heading:
    level: int
    at: int
    anchor: str | None
    words: list[str]


class _DocScanner(HTMLParser):
    """Real h2/h3 elements, ids and insertion points, as a browser would parse them: never inside scripts, styles,
    comments, attribute values or templates."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.text = text
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.headings: list[_Heading] = []
        self.ids: set[str] = set()
        self.after_body: int | None = None
        self.after_head: int | None = None
        self.after_doctype: int | None = None
        self.html_attrs_at: int | None = None
        self.inert = 0
        self.raw = False
        self.open: _Heading | None = None

    def position(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        at = self.position()
        anchor = next((value for name, value in attrs if name == "id" and value), None)
        if anchor:
            self.ids.add(anchor)
        if tag == "html" and self.html_attrs_at is None:
            self.html_attrs_at = -1 if any(name == TERMS_FLAG for name, _ in attrs) else at + len("<html")
        elif tag == "body" and self.after_body is None:
            self.after_body = at + len(self.get_starttag_text() or "")
        elif tag in _INERT_TAGS:
            self.inert += 1
        elif tag in _RAW_TAGS:
            self.raw = True
        elif tag in ("h2", "h3") and not self.inert and self.open is None:
            self.open = _Heading(int(tag[1]), at, anchor, [])

    def handle_endtag(self, tag: str) -> None:
        if tag in _INERT_TAGS and self.inert:
            self.inert -= 1
        elif tag in _RAW_TAGS:
            self.raw = False
        elif self.open is not None and tag == f"h{self.open.level}":
            self.headings.append(self.open)
            self.open = None
        elif tag == "head" and self.after_head is None:
            end = _HEAD_END_RE.match(self.text, self.position())
            self.after_head = end.end() if end else None

    def handle_data(self, data: str) -> None:
        if self.open is not None and not self.raw:
            self.open.words.append(data)

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype") and self.after_doctype is None:
            self.after_doctype = self.text.index(">", self.position()) + 1


def convert_doc_to_viewable(doc: bytes) -> bytes:
    """The doc with the viewer's styles and term popovers, its <html> flagged so the doc's own fallback stays off, and,
    with two or more h2/h3 headings, ids on them and a contents bar built from them; as is when it already carries
    them or is too large to scan on each request."""
    if len(doc) > NAV_MAX_BYTES:
        return doc
    text = doc.decode("utf-8", "surrogateescape")
    scanner = _DocScanner(text)
    try:
        scanner.feed(text)
        scanner.close()
    except (AssertionError, ValueError):
        return doc
    if VIEW_ID in scanner.ids or NAV_ID in scanner.ids:
        return doc
    edits: list[tuple[int, str]] = []
    if scanner.html_attrs_at is not None and scanner.html_attrs_at >= 0:
        edits.append((scanner.html_attrs_at, f" {TERMS_FLAG}"))
    nav = ""
    if len(scanner.headings) >= MIN_HEADINGS:
        taken = set(scanner.ids)
        entries: list[tuple[int, str, str]] = []
        for heading in scanner.headings:
            label = " ".join("".join(heading.words).split())
            anchor = heading.anchor
            if anchor is None:
                anchor = _pick_anchor(label, taken)
                taken.add(anchor)
                edits.append((heading.at + len("<h2"), f' id="{html.escape(anchor)}"'))
            entries.append((heading.level, anchor, label or "(untitled)"))
        nav = _build_nav(entries)
    insert_at = next(
        (at for at in (scanner.after_body, scanner.after_head, scanner.after_doctype) if at is not None), 0
    )
    view = (
        f'<style id="{VIEW_ID}">{_VIEW_STYLE}</style>'
        f"<script>document.documentElement.setAttribute('{TERMS_FLAG}','');{_VIEW_SCRIPT}</script>"
    )
    edits.append((insert_at, nav + view))
    for at, piece in sorted(edits, key=lambda edit: edit[0], reverse=True):
        text = text[:at] + piece + text[at:]
    return text.encode("utf-8", "surrogateescape")


def _pick_anchor(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^\w]+", "-", label.lower()).strip("-")[:60].strip("-") or "section"
    anchor, n = base, 2
    while anchor in taken:
        anchor, n = f"{base}-{n}", n + 1
    return anchor


def _build_nav(entries: list[tuple[int, str, str]]) -> str:
    items = "".join(
        f'<li class="bdn-h{level}"><a href="#{html.escape(anchor)}">{html.escape(label)}</a></li>'
        for level, anchor, label in entries
    )
    return (
        f'<nav id="{NAV_ID}" aria-label="Contents"><style>{_STYLE}</style>'
        f'<details><summary><span class="bdn-label">Contents</span><span class="bdn-cur"></span></summary>'
        f"<ol>{items}</ol></details>"
        f"<script>{_SCRIPT}</script></nav>"
    )
