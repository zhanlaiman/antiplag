import unittest

from plagiarism.web_fetch import FetchError
from plagiarism.web_fetch import _TextExtractor as OptionalClosingExtractor
from plagiarism.web_fetch import _normalize_whitespace
from scripts.html_extractor_v1_baseline import LegacyTextExtractor


class HtmlOptionalEndingsTests(unittest.TestCase):
    """Authored HTML only; never opens CSS, RFC, or any corpus data."""

    def parse(self, html):
        parser = OptionalClosingExtractor()
        parser.feed(html)
        parser.close()
        return parser

    def text(self, parser):
        return " ".join("".join(parser.pieces).split())

    def test_omitted_head_end_does_not_hide_body(self):
        p = self.parse("<html><head><title>Authored title</title><body><p>Visible authored body")
        self.assertEqual(self.text(p), "Visible authored body")
        self.assertEqual("".join(p.title), "Authored title")

    def test_many_optional_table_cell_and_row_ends_do_not_accumulate_depth(self):
        html = "<table><tbody>" + "".join("<tr><th>Heading<td>One<td>Two" for _ in range(200)) + "</table>"
        p = self.parse(html)
        self.assertEqual(p.nodes, 802)
        self.assertIn("Heading One Two", self.text(p))

    def test_real_nested_divs_keep_original_depth_guard(self):
        with self.assertRaises(FetchError) as failure:
            self.parse("<div>" * 129)
        self.assertEqual(failure.exception.code, "html_too_complex")

    def test_flat_optional_lists_keep_original_node_guard(self):
        with self.assertRaises(FetchError) as failure:
            self.parse("<ul>" + "<li>x" * 50_000)
        self.assertEqual(failure.exception.code, "html_too_complex")

    def test_nested_list_scope_preserves_hidden_parent(self):
        p = self.parse("<ul><li hidden>secret<ul><li>inner secret<li>still secret</ul><li>Visible</ul>")
        self.assertEqual(self.text(p), "Visible")

    def test_nested_table_scope_preserves_hidden_outer_cell(self):
        p = self.parse("<table><tr><td hidden><table><tr><td>secret<td>still secret</table><td>Visible</table>")
        self.assertEqual(self.text(p), "Visible")

    def test_optional_paragraphs_do_not_pop_template_container(self):
        p = self.parse("<p hidden><template><p>secret</p></template><p>Visible")
        self.assertEqual(self.text(p), "Visible")

    def test_explicit_closures_and_anchor_boundaries_remain_correct(self):
        p = self.parse('<div><p><a href="https://example.test/">First</a></p><p>Second</p></div>')
        self.assertEqual(self.text(p), "First Second")
        self.assertEqual(p.links, [{"url": "https://example.test/", "text": "First"}])

    def test_optional_paragraph_end_finishes_its_anchor(self):
        p = self.parse('<p><a href="https://example.test/">First<p>Second')
        self.assertEqual(self.text(p), "First Second")
        self.assertEqual(p.links, [{"url": "https://example.test/", "text": "First"}])

    def test_implicit_paragraph_boundary_does_not_join_neighboring_words(self):
        p = self.parse("<p>First<main>Second</main>")
        self.assertEqual(self.text(p), "First Second")

    def test_navigation_and_aside_content_cannot_leak(self):
        for container in ("nav", "aside"):
            with self.subTest(container=container):
                p = self.parse(f"<ul><li hidden><{container}><li>secret<li>still secret</{container}><li>Visible</ul>")
                self.assertEqual(self.text(p), "Visible")

    def test_body_inside_template_cannot_exit_head_or_expose_text(self):
        p = self.parse("<html><head><template><body>secret</template><body>Visible")
        self.assertEqual(self.text(p), "Visible")

    def test_hidden_generic_container_blocks_implicit_closing_above_it(self):
        for attributes in ('hidden', 'aria-hidden="true"', 'style="display:none"', 'role="navigation"'):
            for tag in ("span", "div"):
                with self.subTest(attributes=attributes, tag=tag):
                    html = f"<ul><li hidden><{tag} {attributes}><li>secret<li>still secret</{tag}><li>Visible</ul>"
                    self.assertEqual(self.text(self.parse(html)), "Visible")

    def test_body_inside_hidden_generic_wrapper_cannot_exit_head(self):
        for tag in ("span", "div"):
            with self.subTest(tag=tag):
                html = f"<html><head><{tag} hidden><body>secret</{tag}><body>Visible"
                self.assertEqual(self.text(self.parse(html)), "Visible")

    def test_well_formed_authored_documents_preserve_text_links_and_title(self):
        samples = [
            '<html><head><title>Authored &amp; title</title><style>skip</style></head><body><nav>skip</nav><article><p>First <em>authored</em> paragraph.</p><p><a href="https://example.test/a">Linked text</a> follows.</p><div hidden>skip</div></article></body></html>',
            '<html><head><title>Nested list</title></head><body><ul><li>First<ul><li>Inner</li></ul></li><li>Second</li></ul><aside>skip</aside></body></html>',
            '<html><head><title>Table</title></head><body><table>\n<tr>\n<td>First</td>\n<td>Second</td>\n</tr>\n</table></body></html>',
        ]
        for html in samples:
            with self.subTest(html=html):
                old = LegacyTextExtractor()
                old.feed(html); old.close()
                new = self.parse(html)
                self.assertEqual(_normalize_whitespace("".join(new.pieces)), _normalize_whitespace("".join(old.pieces)))
                self.assertEqual(new.links, old.links)
                self.assertEqual(new.title, old.title)

    def test_genuinely_nested_lists_still_hit_depth_guard(self):
        with self.assertRaises(FetchError) as failure:
            self.parse("<ul><li>" * 65)
        self.assertEqual(failure.exception.code, "html_too_complex")

    def test_genuinely_nested_tables_still_hit_depth_guard(self):
        with self.assertRaises(FetchError) as failure:
            self.parse("<table><tr><td>" * 44)
        self.assertEqual(failure.exception.code, "html_too_complex")


if __name__ == "__main__":
    unittest.main()
