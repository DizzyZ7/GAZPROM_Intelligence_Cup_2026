"""Offline checks for links and the public, client-only demo."""
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class _AssetParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.external_references = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ('script', 'img', 'iframe', 'link'):
            url = values.get('src') or values.get('href') or ''
            if url.startswith(('http:', 'https:', '//')):
                self.external_references.append(url)


class DocumentationChecks(unittest.TestCase):
    def test_internal_markdown_files_exist(self):
        missing = []
        for path in list(ROOT.glob('*.md')) + list((ROOT / 'docs').glob('*.md')):
            md = path.read_text(encoding='utf-8')
            for target in re.findall(r'(?<!!)\[[^\]]+\]\(([^)]+)\)', md):
                if target.startswith(('http:', 'https:', '#', 'mailto:')):
                    continue
                file_path = (path.parent / target.split('#', 1)[0]).resolve()
                # Links to the repository root are validated by the GitHub comparison, 
                # and local extracted cases may be tested without the root checkout.
                if not file_path.is_relative_to(ROOT.resolve()):
                    continue
                if not file_path.exists():
                    missing.append(f'{path.relative_to(ROOT)} -> {target}')
        self.assertEqual(missing, [])

    def test_demo_is_standalone_and_contains_verified_scenario_labels(self):
        markup = (ROOT / 'demo/index.html').read_text(encoding='utf-8')
        parser = _AssetParser()
        parser.feed(markup)
        self.assertEqual(parser.external_references, [])
        self.assertNotIn('fetch(', markup)
        self.assertNotIn('XMLHttpRequest(', markup)
        self.assertIn('Устойчивость сегментов к порогам', markup)
        self.assertIn('Экономика трех бонусов', markup)
        self.assertIn('При +1 л, ₽', markup)


if __name__ == '__main__':
    unittest.main()