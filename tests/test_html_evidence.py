"""Independent HTML evidence: authored inputs and fake transport only."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from plagiarism import web_fetch as fetch, web_search as web, web_evaluation as evaluation
from plagiarism import html_evidence as h, html_evidence_merge as merge
from plagiarism.html_evidence import build_derivation, valid_derivation, source_view, HTMLSupport, POLICY, digest
from plagiarism.html_evidence_merge import analyze_with_supplements
from plagiarism.pdf_evidence import analyze_with_supplements as pdf_analyze
from plagiarism.matching import _tokens, analyze
from plagiarism.web_evaluation import _report
import test_web_fetch as wire_fixtures


WORDS = 'Исследователь утром измерил влажность воздуха возле старой башни перед рассветом'

LEFT, RIGHT = WORDS.split(' ', 1)

VISIBLE = LEFT + '[1][2] ' + RIGHT

HTML = '<title>Unchanged title</title><p>' + LEFT + ''.join(('<sup class="reference"><a href="#cite_note-authored-' + str(n) + '">[' + str(n) + ']</a></sup>' for n in (1, 2))) + ' ' + RIGHT + '</p><a href="/original">original</a>'

URL = 'https://example.test/atmosphere'

def fetched(html=HTML, enabled=True):
    fixture = wire_fixtures.Fixture()
    fixture.add('example.test', '/atmosphere', wire_fixtures.Response(html.encode()))
    result = fetch.Fetcher(resolver=fixture.resolver, connector=fixture.connector, clock=lambda: fixture.now, semantic_notes=enabled).fetch(URL)
    return (result, fixture)

def records(html=HTML):
    page, _ = fetched(html)
    primary = {'id': 'physical', 'url': URL, 'title': page['title'], 'text': page['text'], 'provenance': {'text_sha256': page['text_sha256']}}
    return (primary, source_view(primary, page), page)

def report(target, primary, views):
    result = analyze_with_supplements(target, [primary], views)
    result.update(text=target, sources=[primary, *views], web={'errors': []}, scope='web_search')
    checked = _report(result, {'id': 'authored', 'text_sha256': digest(target)})
    assert not checked['invalid_highlights'], checked['invalid_highlights']
    return result

def covered(target, matches):
    return {t.start for t in _tokens(target) if any((m['kind'] == 'exact' and m['start'] <= t.start and (t.end <= m['end']) for m in matches))}

class IndependentHTMLTests(unittest.TestCase):

    def test_fetcher_primary_title_links_metadata_unchanged_and_no_extra_wire(self):
        old, fo = fetched(enabled=False)
        new, fn = fetched()
        for key in old:
            if key != 'fetched_at':
                self.assertEqual(old[key], new[key], key)
        self.assertEqual(len(fo.requests), len(fn.requests))
        self.assertEqual(len(fn.requests), 2)
        self.assertEqual(new['extraction'], 'html-parser-v4-edit-controls')
        self.assertEqual(new['html_semantic_view']['text'].split('\n')[0], WORDS)

    def test_no_ID_target_gains_full_ten_span_without_deleting_old_nine(self):
        primary, views, _ = records()
        old = pdf_analyze(WORDS, [primary])
        result = report(WORDS, primary, views)
        self.assertEqual(len(covered(WORDS, old['matches'])), 9)
        self.assertEqual(len(covered(WORDS, result['matches'])), 10)
        self.assertEqual(result['matches'][:len(old['matches'])], old['matches'])
        new = result['matches'][-1]
        self.assertEqual(new['excerpt'], WORDS)
        self.assertEqual(new['source_excerpt'], views[0]['text'][new['source_start']:new['source_end']])
        self.assertIn('html_semantic_notes_evidence', new)
        self.assertEqual(result['stats']['checked_source_count'], 1)

    def test_visible_citation_target_keeps_every_legacy_object_and_twelve_words(self):
        primary, views, _ = records()
        before = deepcopy(primary)
        old = pdf_analyze(VISIBLE, [primary])
        result = report(VISIBLE, primary, views)
        self.assertEqual(result['matches'], old['matches'])
        self.assertEqual(len(covered(VISIBLE, result['matches'])), 12)
        self.assertEqual(primary, before)

    def test_quoted_visible_target_keeps_quoted_flags_ids_and_offsets(self):
        target = '«' + VISIBLE + '»\nИсточник: ' + URL
        primary, views, _ = records()
        old = pdf_analyze(target, [primary])
        result = report(target, primary, views)
        self.assertEqual(result['matches'], old['matches'])
        self.assertTrue(all((m['quoted'] for m in result['matches'])))
        self.assertEqual(len(covered(target, result['matches'])), 12)

    def test_mixed_ID_and_no_ID_targets_same_physical_page_union22(self):
        target = VISIBLE + '\n\n' + WORDS
        primary, views, _ = records()
        old = pdf_analyze(target, [primary])
        result = report(target, primary, views)
        self.assertEqual(len(covered(target, result['matches'])), 22)
        self.assertEqual(result['matches'][:len(old['matches'])], old['matches'])
        self.assertEqual(result['stats']['source_count'], 1)
        self.assertEqual(len({m['id'] for m in result['matches']}), len(result['matches']))

    def test_similar_and_PDF_objects_unchanged_when_HTML_added(self):
        phrase = 'Редактор проверил каждую строку перед публикацией подробного описания лабораторного измерения'
        primary, views, _ = records()
        pdf = {'id': 'pdf', 'url': 'https://example.test/paper.pdf', 'text': phrase.replace('лабораторного', 'лабо раторного')}
        pdfview = {'id': 'pdf-view', 'representation_of': 'pdf', 'url': pdf['url'], 'text': phrase}
        target = WORDS + '\n\n' + phrase
        old = pdf_analyze(target, [primary, pdf], [pdfview])
        result = analyze_with_supplements(target, [primary, pdf], [pdfview, *views])
        self.assertEqual(result['matches'][:len(old['matches'])], old['matches'])
        self.assertTrue(any((m.get('pdf_spacing_evidence') for m in result['matches'])))
        self.assertEqual(len({m['id'] for m in result['matches']}), len(result['matches']))

    def test_compound_forgery_rejected_even_with_same_ordered_characters(self):
        primary = 'amber canyon cedar forest distant island lantern market quiet river silver valley[1]'
        forged = 'ambercanyon cedarforest distantisland lanternmarket quietriver silvervalley'
        with self.assertRaises(ValueError):
            build_derivation(primary, forged, [{'rendered_marker': '[1]'}], 'a' * 64)

    def test_overlong_or_nonstring_marker_transcript_rejected(self):
        for marker in ('[' + ' ' * 65 + '1]', None, 42):
            with self.assertRaises(ValueError):
                build_derivation(WORDS + '[1]', WORDS, [{'rendered_marker': marker}], 'a' * 64)

    def test_changed_letters_order_hashes_and_negative_spans_rejected(self):
        primary, views, page = records()
        view = views[0]
        for mutate in (lambda v: v['provenance'].update(raw_html_sha256='b' * 64), lambda v: v.update(url='https://other.test/'), lambda v: v['provenance']['html_derivation'].update(removed_primary_spans=[[-1, 2]]), lambda v: v.update(text=v['text'].replace('утром', 'вечером'))):
            bad = deepcopy(view)
            mutate(bad)
            self.assertFalse(HTMLSupport(primary, bad).valid)
            self.assertEqual(analyze_with_supplements(WORDS, [primary], [bad])['matches'], pdf_analyze(WORDS, [primary])['matches'])

    def test_report_proof_tampering_and_missing_proof_quarantined(self):
        primary, views, _ = records()
        result = report(WORDS, primary, views)
        for mutate in (lambda m: m.pop('html_semantic_notes_evidence'), lambda m: m['html_semantic_notes_evidence'].update(view_span=[0, 1])):
            bad = deepcopy(result)
            mutate(bad['matches'][-1])
            checked = _report(bad, {'id': 'authored', 'text_sha256': digest(WORDS)})
            self.assertEqual(len(checked['invalid_highlights']), 1)

    def test_duplicate_HTML_views_skip_only_HTML(self):
        primary, views, _ = records()
        other = deepcopy(views[0])
        other['id'] = 'another'
        self.assertEqual(analyze_with_supplements(WORDS, [primary], views + [other])['matches'], pdf_analyze(WORDS, [primary])['matches'])

    def test_invalid_HTML_declaration_cannot_fall_back_to_PDF_spacing(self):
        primary = {'id': 'physical', 'url': URL, 'text': 'amber canyon cedar forest distant island lantern market quiet river silver valley[1]'}
        target = 'ambercanyon cedarforest distantisland lanternmarket quietriver silvervalley'
        view = {'id': 'physical-html-notes-v1', 'representation_of': 'physical', 'url': URL, 'text': target, 'provenance': {'extraction': 'html-parser-v3-semantic-notes', 'html_derivation': {}, 'evidence_policy': 'invalid-html-policy'}}
        self.assertEqual(analyze_with_supplements(target, [primary], [view])['matches'], pdf_analyze(target, [primary])['matches'])
        forged = pdf_analyze(target, [primary], [view])
        forged.update(text=target, sources=[primary, view], web={'errors': []})
        self.assertTrue(_report(forged, {'id': 'authored', 'text_sha256': digest(target)})['invalid_highlights'])
        forged['sources'][1]['provenance'] = None
        self.assertTrue(_report(forged, {'id': 'authored', 'text_sha256': digest(target)})['invalid_highlights'])

    def test_duplicate_physical_URL_and_total_source_caps_skip_or_reject_HTML(self):
        primary, views, _ = records()
        duplicate = {**primary, 'id': 'duplicate'}
        old = pdf_analyze(WORDS, [primary, duplicate])
        self.assertEqual(analyze_with_supplements(WORDS, [primary, duplicate], views)['matches'], old['matches'])
        with self.assertRaises(ValueError):
            analyze_with_supplements(WORDS, [primary], views * 100)

    def test_combined_200k_bound_retains_usable_primary(self):
        large = '<p>' + 'independent words ' * 6000 + HTML + '</p>'
        page, _ = fetched(large)
        self.assertGreater(len(page['text']), 100000)
        self.assertNotIn('html_semantic_view', page)
        self.assertEqual(page['text_sha256'], digest(page['text']))

    def test_code_math_quote_and_unrelated_negatives_do_not_invent_views(self):
        for tag in ('code', 'pre', 'math', 'q', 'blockquote'):
            page, _ = fetched('<' + tag + '>' + HTML + '</' + tag + '>')
            self.assertNotIn('html_semantic_view', page, tag)
        primary, views, _ = records()
        negative = 'Архивариус бережно разложил старинные карты возле широкого деревянного шкафа'
        self.assertFalse(any((m['kind'] == 'exact' for m in report(negative, primary, views)['matches'])))

    def test_normal_run_with_post_phase_counts_HTML_once_and_no_PDF_policy(self):
        primary, views, page = records()

        class Search:
            provider = 'authored'

            def search(self, query, request_id):
                return [{'url': URL}]

        class Pages:

            def fetch(self, url):
                return deepcopy(page)

        class Wiki:
            provider = 'mediawiki'

            def search(self, query, request_id):
                return []
        with patch.object(web, 'selected_provider', return_value='tavily'):
            result = web.run(WORDS, searcher=Search(), wiki_searcher=Wiki(), fetcher=Pages(), use_wiki_followup=True)
        self.assertEqual(result['stats']['matched_words'], 10)
        self.assertEqual(result['web']['html_text_views'], 1)
        self.assertEqual(result['web']['pdf_text_views'], 0)
        self.assertIsNone(result['web']['pdf_evidence_policy'])
        self.assertEqual(result['web']['html_evidence_policy'], POLICY)
        self.assertEqual(result['web']['sources_checked'], 1)
        self.assertEqual(len(result['sources']), 2)
        self.assertFalse(_report(result, {'id': 'authored', 'text_sha256': digest(WORDS)})['invalid_highlights'])

fixture = SimpleNamespace(WORDS=WORDS, VISIBLE=VISIBLE, URL=URL)

def source_and_view(primary_text=None, clean_text=None, labels=('[1]', '[2]')):
    primary_text = fixture.VISIBLE if primary_text is None else primary_text
    clean_text = fixture.WORDS if clean_text is None else clean_text
    descriptor = h.build_derivation(primary_text, clean_text, [{'rendered_marker': label} for label in labels], 'a' * 64)
    primary = {'id': 'physical', 'title': 'Authored page', 'url': fixture.URL, 'text': primary_text, 'provenance': {'origin': 'web_fetch', 'training_consent': False}}
    fetched = {'content_type': 'text/html', 'extraction': h.PRIMARY_EXTRACTION, 'raw_html_sha256': 'a' * 64, 'html_semantic_view': {'id': h.VIEW_ID, 'policy': h.POLICY, 'extraction': h.VIEW_EXTRACTION, 'text': clean_text, 'text_sha256': h.digest(clean_text), 'raw_html_sha256': 'a' * 64, 'derivation': descriptor}}
    views = h.source_view(primary, fetched)
    assert len(views) == 1
    return (primary, views[0], fetched)

def coverage(text, matches):
    tokens = _tokens(text)
    return {t.start for t in tokens if any((m['kind'] == 'exact' and m['start'] <= t.start and (t.end <= m['end']) for m in matches))}

class IndependentEvidenceIdentityAndReportTests(unittest.TestCase):

    def test_valid_view_is_independent_and_does_not_mutate_primary(self):
        primary, view, fetched = source_and_view()
        before = deepcopy(primary)
        again = h.source_view(primary, fetched)
        self.assertEqual(primary, before)
        self.assertEqual(again, [view])
        self.assertEqual(view['representation_of'], primary['id'])
        self.assertEqual(view['url'], primary['url'])

    def test_visible_ID_targets_keep_all_twelve_with_or_without_quote_URL(self):
        primary, view, _ = source_and_view()
        for target in (fixture.VISIBLE, '«' + fixture.VISIBLE + '»\nИсточник: ' + fixture.URL):
            with self.subTest(target=target):
                old = pdf_analyze(target, [primary])
                old_matches = list(old['matches'])
                old_values = deepcopy(old_matches)
                with patch.object(merge, 'legacy_analyze', return_value=old):
                    result = merge.analyze_with_supplements(target, [primary], [view])
                self.assertTrue(all((a is b for a, b in zip(result['matches'], old_matches))))
                self.assertEqual(result['matches'][:len(old_values)], old_values)
                self.assertTrue(coverage(target, old_matches) <= coverage(target, result['matches']))
                copied_end = len(fixture.VISIBLE) + int(target.startswith('«'))
                copied = [t for t in _tokens(target) if t.end <= copied_end]
                self.assertEqual(len({t.start for t in copied} & coverage(target, result['matches'])), 12)

    def test_no_ID_target_gains_first_word_without_removing_legacy_nine(self):
        primary, view, _ = source_and_view()
        old = pdf_analyze(fixture.WORDS, [primary])
        old_matches = list(old['matches'])
        self.assertEqual(len(coverage(fixture.WORDS, old_matches)), 9)
        with patch.object(merge, 'legacy_analyze', return_value=old):
            result = merge.analyze_with_supplements(fixture.WORDS, [primary], [view])
        self.assertEqual(len(coverage(fixture.WORDS, result['matches'])), 10)
        self.assertTrue(all((a is b for a, b in zip(result['matches'], old_matches))))
        self.assertEqual(result['stats']['source_count'], 1)
        self.assertEqual(result['stats']['checked_source_count'], 1)

    def test_mixed_ID_and_no_ID_insertions_keep_one_physical_source(self):
        second = 'Сотрудница вечером проверила давление воды около новой плотины после дождя'
        left, right = second.split(' ', 1)
        primary_text = fixture.VISIBLE + ' Затем наблюдение завершилось.\n' + left + '[3][4] ' + right
        clean_text = fixture.WORDS + ' Затем наблюдение завершилось.\n' + second
        primary, view, _ = source_and_view(primary_text, clean_text, ('[1]', '[2]', '[3]', '[4]'))
        target = fixture.VISIBLE + '\n\nСамостоятельное пояснение исследовательской работы.\n\n' + second
        old = pdf_analyze(target, [primary])
        self.assertEqual(len(coverage(target, old['matches'])), 21)
        result = merge.analyze_with_supplements(target, [primary], [view])
        self.assertEqual(len(coverage(target, result['matches'])), 22)
        self.assertEqual(result['stats']['source_count'], 1)
        self.assertEqual(result['stats']['checked_source_count'], 1)

    def test_genuine_legacy_PDF_spacing_result_is_unchanged(self):
        primary = {'id': 'pdf-primary', 'url': 'https://example.test/authored.pdf', 'title': 'Authored PDF', 'text': 'amber canyon cedar forest distant island lantern market quiet river silver valley'}
        target = 'ambercanyon cedarforest distantisland lanternmarket quietriver silvervalley'
        pdf = {'id': 'pdf-primary-pdfium-v1', 'representation_of': primary['id'], 'url': primary['url'], 'title': 'PDF spacing', 'text': target}
        expected = pdf_analyze(target, [primary], [pdf])
        actual = merge.analyze_with_supplements(target, [primary], [pdf])
        self.assertEqual(actual, expected)
        self.assertTrue(any((m.get('pdf_spacing_evidence') for m in actual['matches'])))

    def test_report_duplicate_physical_URL_primaries_quarantine_HTML(self):
        primary, view, _ = source_and_view()
        second = {**primary, 'id': 'second-primary'}
        second_view = deepcopy(view)
        second_view.update(id='second-primary-html-notes-v1', representation_of=second['id'])
        mapping = {view['id']: (primary, view), second_view['id']: (second, second_view)}
        result = analyze(fixture.WORDS, [view, second_view])
        for match in result['matches']:
            p, v = mapping[match['source_id']]
            match['html_semantic_notes_evidence'] = h.HTMLSupport(p, v).evidence(match)
        result.update(text=fixture.WORDS, sources=[primary, second, view, second_view], web={'errors': []})
        checked = evaluation._report(result, {'id': 'authored', 'text_sha256': h.digest(fixture.WORDS)})
        self.assertEqual(len(checked['invalid_highlights']), len(result['matches']))

    def test_report_malformed_HTML_provenance_is_quarantined_without_crash(self):
        primary, view, _ = source_and_view()
        result = analyze(fixture.WORDS, [view])
        for match in result['matches']:
            match['html_semantic_notes_evidence'] = h.HTMLSupport(primary, view).evidence(match)
        view['provenance'] = None
        result.update(text=fixture.WORDS, sources=[primary, view], web={'errors': []})
        checked = evaluation._report(result, {'id': 'authored', 'text_sha256': h.digest(fixture.WORDS)})
        self.assertEqual(len(checked['invalid_highlights']), len(result['matches']))

class EvidenceBoundaryTests(unittest.TestCase):
    def test_each_HTML_declaration_signal_blocks_PDF_fallback_independently(self):
        primary = {"id": "physical", "url": URL, "title": "Authored page",
            "text": "amber canyon cedar forest distant island lantern market quiet river silver valley"}
        target = "ambercanyon cedarforest distantisland lanternmarket quietriver silvervalley"
        declarations = [
            {"id": "physical-html-notes-v1", "provenance": {}},
            {"id": "other-view", "provenance": {"extraction": h.VIEW_EXTRACTION}},
            {"id": "other-view", "provenance": {"html_derivation": {}}},
            {"id": "other-view", "provenance": {"evidence_policy": "html-invalid-policy"}},
            {"id": "other-view", "provenance": None},
        ]
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                view = {"representation_of": primary["id"], "url": URL, "text": target, **declaration}
                result = merge.analyze_with_supplements(target, [primary], [view])
                self.assertEqual(result["matches"], [])

    def test_exact_100_snapshot_boundary_and_101_rejection(self):
        primary, view, _ = source_and_view()
        others = [{**primary, "id": "other-" + str(n), "url": "https://example.test/other-" + str(n)} for n in range(98)]
        result = merge.analyze_with_supplements(WORDS, [primary, *others], [view])
        self.assertEqual(result["stats"]["checked_source_count"], 99)
        self.assertEqual(len(coverage(WORDS, result["matches"])), 10)
        extra = {**primary, "id": "one-too-many", "url": "https://example.test/extra"}
        with self.assertRaises(ValueError):
            merge.analyze_with_supplements(WORDS, [primary, *others, extra], [view])

    def test_noncharacter_excerpt_cannot_gain_HTML_evidence(self):
        raw = VISIBLE.replace("влажность", "влаж\ufdd0ность")
        clean = WORDS.replace("влажность", "влаж\ufdd0ность")
        primary, view, _ = source_and_view(raw, clean)
        support = h.HTMLSupport(primary, view)
        self.assertTrue(support.valid)
        candidate = analyze(clean, [view])["matches"][0]
        self.assertIn("\ufdd0", candidate["source_excerpt"])
        self.assertIsNone(support.evidence(candidate))

    def test_Fetcher_view_kind_hash_and_extraction_bindings_are_required(self):
        primary, _, fetched_page = source_and_view()
        original = deepcopy(primary)
        mutations = [lambda value: value.update(content_type="application/pdf"),
            lambda value: value.update(extraction="pdfium-text-v1"),
            lambda value: value.update(raw_html_sha256="b" * 64),
            lambda value: value["html_semantic_view"].update(text_sha256="b" * 64),
            lambda value: value["html_semantic_view"].update(id="unsupported-view")]
        for mutate in mutations:
            value = deepcopy(fetched_page)
            mutate(value)
            with self.subTest(value=value):
                self.assertEqual(h.source_view(primary, value), [])
                self.assertEqual(primary, original)


if __name__ == "__main__":
    unittest.main()
