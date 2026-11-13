from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from plagiarism import proofreading
from plagiarism.storage import Store


class ProofreadingTests(unittest.TestCase):
    def test_vendored_dictionary_matches_pinned_manifest_and_license(self):
        root = proofreading.DICTIONARY.parent
        manifest = json.loads((root / 'manifest.json').read_text())
        self.assertEqual(len(manifest['commit']), 40)
        for item in manifest['files']:
            self.assertEqual(sha256((root / item['name']).read_bytes()).hexdigest(), item['sha256'])
            self.assertIn(manifest['commit'], item['url'])
        self.assertEqual((root / 'README_ru_RU.txt').read_bytes(),
                         (root.parents[2] / 'web/dictionary-license.txt').read_bytes())

    @unittest.skipUnless(proofreading.available(), 'Install Hunspell for native integration')
    def test_native_typos_suggestions_morphology_and_utf16_offsets(self):
        text = '🙂 Привет,мир! Здесь ашибка и карова. Слова словами слово.'
        result = proofreading.check(text)
        self.assertTrue(result['dictionary_available'])
        issues = {issue['original']: issue for issue in result['issues']}
        self.assertIn('ошибка', issues['ашибка']['suggestions'])
        self.assertIn('корова', issues['карова']['suggestions'])
        self.assertIn(', ', issues[',']['suggestions'])
        encoded = text.encode('utf-16-le')
        for issue in result['issues']:
            self.assertEqual(encoded[issue['start']*2:issue['end']*2].decode('utf-16-le'), issue['original'])
        group = next(item for item in result['frequencies'] if item['word'] == 'слово')
        self.assertEqual(group['count'], 3)
        self.assertEqual({item['word'] for item in group['forms']}, {'слова', 'словами', 'слово'})

    @unittest.skipUnless(proofreading.available(), 'Install Hunspell for native integration')
    def test_personal_name_is_uncertain_and_personal_dictionary_isolated(self):
        with TemporaryDirectory() as directory:
            alice = Store(Path(directory) / 'a.sqlite3')
            bob = Store(Path(directory) / 'b.sqlite3')
            alice.update_dictionary('Данияр', 'add')
            self.assertEqual(Store(alice.path).dictionary_words(), ['данияр'])
            self.assertEqual(bob.dictionary_words(), [])
            before = proofreading.check('Данияр приехал.')
            unknown = next(item for item in before['issues'] if item['original'] == 'Данияр')
            self.assertIn('имя', unknown['message'])
            self.assertFalse(proofreading.check('Данияр приехал.', alice.dictionary_words())['issues'])
            alice.update_dictionary('Данияр', 'remove')
            self.assertEqual(alice.dictionary_words(), [])
            for word in ['#save', 'два слова', '../file', 'x\nопасно']:
                with self.assertRaises(ValueError):
                    alice.update_dictionary(word, 'add')

    def test_rules_ignore_addresses_and_preserve_normal_punctuation(self):
        clean = 'Цена 12,5 тенге. Адрес https://пример.рф/тест,путь и почта имя@example.org. А. С. Пушкин. «Текст (пример)». Всё хорошо!'
        with patch.object(proofreading, 'available', return_value=False):
            self.assertEqual(proofreading.check(clean)['issues'], [])
            result = proofreading.check('Это  это слово ,, и конец [')
        self.assertEqual({item['kind'] for item in result['issues']}, {'spacing', 'punctuation', 'repetition'})
        self.assertFalse(result['dictionary_available'])
        self.assertTrue(result['warnings'])

    def test_timeout_never_claims_dictionary_success_or_stores_text(self):
        with patch.object(proofreading, 'available', return_value=True), patch.object(proofreading, '_run', side_effect=subprocess.TimeoutExpired('hunspell', 12)):
            result = proofreading.check('Ашибка  здесь')
        self.assertFalse(result['dictionary_available'])
        self.assertEqual(result['checked_unique_forms'], 0)
        self.assertEqual(result['issues'][0]['kind'], 'spacing')
        self.assertNotIn('text', result)

    def test_context_rules_offer_manual_grammar_corrections_without_changing_input(self):
        text = 'Он хочет учится. Нужно готовится. Более лучше в течении дня.'
        with patch.object(proofreading, 'available', return_value=False):
            result = proofreading.check(text)
            clean = proofreading.check('Он учится. Он хочет учиться. Нужно готовиться. Лучше в течение дня. В течении реки есть водовороты.')
        grammar = [item for item in result['issues'] if item['kind'] == 'grammar']
        self.assertEqual([item['suggestions'][0] for item in grammar], ['учиться', 'готовиться', 'Лучше', 'в течение'])
        self.assertFalse(clean['issues'])
        self.assertEqual(result['text_sha256'], sha256(text.encode()).hexdigest())

    def test_empty_oversized_and_concurrent_inputs_are_bounded(self):
        for text in ['', ' ', 'а'*10001, None]:
            with self.assertRaises(ValueError):
                proofreading.check(text)
        proofreading.LOCK.acquire()
        try:
            with self.assertRaisesRegex(ValueError, 'занят'):
                proofreading.check('Текст')
        finally:
            proofreading.LOCK.release()


if __name__ == '__main__':
    unittest.main()
