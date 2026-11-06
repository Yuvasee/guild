import re
import time

from bridge.doc_nav import NAV_ID, TERMS_FLAG, VIEW_ID, convert_doc_to_viewable

DOC = b"""<!doctype html><html><head><title>t</title></head><body>
<h1>Title</h1>
<h2>In short</h2><p>x</p>
<h2 id="names" class="k">Names <code>used</code> below</h2>
<h3>Codex &amp; Claude</h3>
<h3 class="x">In short</h3>
<!-- <h2>commented out</h2> -->
<script>var s = "<h2>in a script</h2>";</script>
<h4>too deep</h4>
</body></html>"""


def nav_links(doc: bytes) -> list[tuple[str, str]]:
    nav = doc[doc.index(f'<nav id="{NAV_ID}"'.encode()) : doc.index(b"</nav>")].decode()
    return re.findall(r'<li class="bdn-(h[23])"><a href="#([^"]+)">', nav)


def test_h2_and_h3_are_listed_in_order_with_their_ids() -> None:
    served = convert_doc_to_viewable(DOC)
    assert nav_links(served) == [("h2", "in-short"), ("h2", "names"), ("h3", "codex-claude"), ("h3", "in-short-2")]
    assert b">Names used below</a>" in served and b">Codex &amp; Claude</a>" in served
    assert b"commented out</a>" not in served and b"in a script</a>" not in served and b"too deep</a>" not in served


def test_ids_are_added_only_where_missing() -> None:
    served = convert_doc_to_viewable(DOC)
    assert b'<h2 id="in-short">In short</h2>' in served
    assert b'<h2 id="names" class="k">' in served and b'id="names"' not in served.replace(b'<h2 id="names"', b"", 1)
    assert b'<h3 id="in-short-2" class="x">In short</h3>' in served
    assert b"<h1>Title</h1>" in served and b"<h4>too deep</h4>" in served
    assert b'<script>var s = "<h2>in a script</h2>";</script>' in served


def test_the_bar_goes_right_after_body_and_only_once() -> None:
    served = convert_doc_to_viewable(DOC)
    assert served.index(f'<nav id="{NAV_ID}"'.encode()) == served.index(b"<body>") + len(b"<body>")
    assert convert_doc_to_viewable(served) == served
    assert served.count(f'id="{NAV_ID}"'.encode()) == 1


def test_a_doc_with_fewer_than_two_headings_gets_the_view_but_no_bar() -> None:
    for doc in (b"<html><body><p>plain</p></body></html>", b"<html><body><h2>Only</h2><p>x</p></body></html>"):
        served = convert_doc_to_viewable(doc)
        assert f'<nav id="{NAV_ID}"'.encode() not in served and f'<style id="{VIEW_ID}">'.encode() in served
        assert b"<h2>Only</h2>" in served or b"<p>plain</p>" in served


def test_the_view_is_injected_once_with_its_term_popover_and_link_colors() -> None:
    served = convert_doc_to_viewable(DOC)
    assert served.count(b"bdv-pop") >= 1 and served.count(b'.closest(".term[data-def]")') == 1
    assert served.count(f'<style id="{VIEW_ID}">'.encode()) == 1
    assert b":where(a:link,a:visited){color:var(--bdv-link)" in served
    for mark in (b"observed", b"reproduced", b"from-code", b"decided", b"fixed-here", b"deferred", b"needs-decision"):
        assert b".mark." + mark in served
    assert convert_doc_to_viewable(served) == served


def test_the_html_tag_is_flagged_so_a_docs_own_term_fallback_stays_off() -> None:
    served = convert_doc_to_viewable(b'<!doctype html><HTML lang="en"><body><span class="term" data-def="d">T</span>')
    assert served.startswith(f'<!doctype html><HTML {TERMS_FLAG} lang="en">'.encode())
    flagged = b"<html data-viewer-terms><body><h2>A</h2><h2>B</h2></body></html>"
    assert convert_doc_to_viewable(flagged).count(TERMS_FLAG.encode()) == 2
    assert f"setAttribute('{TERMS_FLAG}','')".encode() in convert_doc_to_viewable(b"<p>no html tag</p>")


def test_the_bar_is_a_side_column_from_880px_and_a_fixed_top_bar_below() -> None:
    served = convert_doc_to_viewable(DOC)
    assert b"SIDE_MIN=880" in served and b"#bridge-doc-nav.bdn-top{position:fixed;top:0" in served
    assert b'<span class="bdn-label">Contents</span>' in served
    assert b'html.style.boxSizing="border-box"' in served and b'addEventListener("beforeprint",unshift)' in served


def test_an_open_popover_follows_scrolling_inside_a_box_and_fits_the_window() -> None:
    served = convert_doc_to_viewable(DOC)
    assert b'document.addEventListener("scroll",place,{passive:true,capture:true})' in served
    assert b"#bdv-pop{box-sizing:border-box;" in served and b"max-height:calc(100vh - 16px);overflow:auto" in served
    assert (
        b"html.style.paddingLeft=base[0]+w+GAP" in served
        and b"html.style.paddingTop=base[1]+nav.offsetHeight" in served
    )


def test_an_existing_data_id_is_not_taken_for_an_id() -> None:
    served = convert_doc_to_viewable(b'<body><h2 data-id="z">A</h2><h2>B</h2></body>')
    assert nav_links(served) == [("h2", "a"), ("h2", "b")]
    assert b'<h2 id="a" data-id="z">' in served


def test_a_generated_id_does_not_clash_with_one_the_doc_has() -> None:
    served = convert_doc_to_viewable(b'<body><div id="risks"></div><h2>Risks</h2><h2>Risks</h2></body>')
    assert nav_links(served) == [("h2", "risks-2"), ("h2", "risks-3")]


def test_heading_text_is_escaped_in_the_bar() -> None:
    served = convert_doc_to_viewable(b"<body><h2>a &lt;b&gt;</h2><h2>&quot;q&quot;</h2></body>")
    assert b">a &lt;b&gt;</a>" in served and b">&quot;q&quot;</a>" in served


def test_without_a_body_tag_the_bar_follows_the_doctype() -> None:
    served = convert_doc_to_viewable(b"<!DOCTYPE html><h2>A</h2><h2>B</h2>")
    assert served.startswith(f'<!DOCTYPE html><nav id="{NAV_ID}"'.encode())


def test_heading_markup_inside_an_attribute_is_not_a_heading_and_stays_inert() -> None:
    doc = b"""<body><div title="<h2 onmouseover='x=1'>A</h2>">d</div><h2>B</h2><h2>C</h2></body>"""
    served = convert_doc_to_viewable(doc)
    assert nav_links(served) == [("h2", "b"), ("h2", "c")]
    assert b"""<div title="<h2 onmouseover='x=1'>A</h2>">""" in served


def test_an_opening_heading_in_a_script_string_does_not_swallow_a_real_one() -> None:
    served = convert_doc_to_viewable(b'<body><script>const s="<h2>";</script><h2>A</h2><h2>B</h2></body>')
    assert nav_links(served) == [("h2", "a"), ("h2", "b")]


def test_a_body_tag_in_a_head_script_or_a_gt_in_an_attribute_does_not_move_the_bar() -> None:
    doc = b'<html><head><script>const t="<body>";</script></head><body data-note="a > b"><h2>A</h2><h2>B</h2>'
    served = convert_doc_to_viewable(doc)
    assert b'const t="<body>";</script>' in served
    assert served.index(b'<body data-note="a > b"><nav id=') > 0


def test_an_id_is_read_from_the_attribute_not_from_another_attribute_value() -> None:
    served = convert_doc_to_viewable(b'<body><h2 title="example id=ghost" id="real">A</h2><h2 title="a > b">B</h2>')
    assert nav_links(served) == [("h2", "real"), ("h2", "b")]
    assert b'<h2 title="example id=ghost" id="real">' in served and b'<h2 id="b" title="a > b">' in served


def test_headings_in_a_template_are_not_listed() -> None:
    served = convert_doc_to_viewable(b"<body><template><h2>T</h2></template><h2>A</h2><h2>B</h2></body>")
    assert nav_links(served) == [("h2", "a"), ("h2", "b")]


def test_mentioning_the_bar_in_text_does_not_suppress_it() -> None:
    served = convert_doc_to_viewable(b"<body><p>see bridge-doc-nav</p><h2>A</h2><h2>B</h2></body>")
    assert f'<nav id="{NAV_ID}"'.encode() in served


def test_many_heading_literals_in_a_script_scan_quickly() -> None:
    doc = b"<body><h2>A</h2><h2>B</h2><script>var s='" + b"<h2>" * 50_000 + b"';</script></body>"
    started = time.monotonic()
    assert nav_links(convert_doc_to_viewable(doc)) == [("h2", "a"), ("h2", "b")]
    assert time.monotonic() - started < 1


def test_non_utf8_bytes_survive_the_rewrite() -> None:
    served = convert_doc_to_viewable(b"<body><p>\xff\xfe</p><h2>A</h2><h2>B</h2></body>")
    assert b"<p>\xff\xfe</p>" in served
