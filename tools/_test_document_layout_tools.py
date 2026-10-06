import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DocumentLayoutToolsTest(unittest.TestCase):
    def test_missing_pymupdf4llm_reports_unsupported(self):
        from core.intelligence import document_layout_tools as tools

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tools,
            '_load_pymupdf4llm',
            return_value=(None, 'pymupdf4llm missing'),
        ):
            pdf_path = Path(tmp) / 'report.pdf'
            pdf_path.write_bytes(b'%PDF-1.4')
            result = tools.extract_markdown_with_pymupdf4llm(pdf_path)

        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['markdown'], '')
        self.assertIn('pymupdf4llm missing', result['error'])

    def test_mock_markdown_headings_support_section_comparison(self):
        from core.intelligence import document_layout_tools as tools

        class FakePyMuPDF4LLM:
            @staticmethod
            def to_markdown(path, **kwargs):
                if kwargs.get('page_chunks'):
                    return [{'page': 1, 'text': '# Main business'}]
                return '# Main business\n\n| Item | Value |\n|---|---|\n| Revenue | 100 |\n\n## Risk factors\n'

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tools,
            '_load_pymupdf4llm',
            return_value=(FakePyMuPDF4LLM, ''),
        ):
            pdf_path = Path(tmp) / 'report.pdf'
            pdf_path.write_bytes(b'%PDF-1.4')
            report = tools.compare_section_index_with_markdown(
                pdf_path,
                current_sections=[
                    {'section_id': 'main_business', 'title': 'Main business'},
                    {'section_id': 'risk_factors', 'title': 'Risk factors'},
                ],
            )

        self.assertEqual(report['markdown_status'], 'ok')
        self.assertEqual(report['json_status'], 'ok')
        self.assertEqual(report['heading_count'], 2)
        self.assertEqual(report['table_markdown_count'], 1)
        self.assertEqual(report['section_overlap_count'], 2)
        self.assertNotIn('evidence', report)


if __name__ == '__main__':
    unittest.main()
