import unittest
from plagiarism.matching import analyze,literal_equivalent,MIN_WORDS


class SpacedLineWrapTests(unittest.TestCase):
    def test_pdf_space_before_line_wrap_keeps_both_original_excerpts_and_offsets(self):
        clean='Научное сообщество внимательно изучает изменения окружающей среды'
        for gap in (' -\n','\t- \r\n','-\n'):
            wrapped=clean.replace('сообщество','со'+gap+'общество')
            for target,source in ((clean,wrapped),(wrapped,clean)):
                result=analyze(target,[{'id':'source','url':'https://example.org/paper.pdf','title':'PDF','text':source}])
                full=[m for m in result['matches']if m['kind']=='exact'and m['start']==0 and m['end']==len(target)]
                self.assertEqual(len(full),1)
                match=full[0]
                self.assertEqual(match['excerpt'],target)
                self.assertEqual(match['source_excerpt'],source)
                self.assertTrue(literal_equivalent(match['excerpt'],match['source_excerpt']))

    def test_no_arbitrary_whitespace_paragraph_dash_digit_or_letter_changes(self):
        clean='Научное сообщество внимательно изучает изменения окружающей среды'
        for altered in ('со общество','со - общество','со \n-\nобщество','со -\n\nобщество',
                        'со —\nобщество','со --\nобщество','со -\fобщество','со -\nобшество'):
            self.assertFalse(literal_equivalent(clean,clean.replace('сообщество',altered)))
        self.assertFalse(literal_equivalent('100лет','100 -\nлет'))

    def test_five_words_do_not_become_a_six_word_exact(self):
        self.assertEqual(MIN_WORDS,6)
        clean='Сообщество бережно изучает лесные экосистемы'
        source=clean.replace('Сообщество','Со -\nобщество')
        result=analyze(clean,[{'id':'s','title':'PDF','url':'https://example.org/paper.pdf','text':source}])
        self.assertFalse(any(m['kind']=='exact'for m in result['matches']))
