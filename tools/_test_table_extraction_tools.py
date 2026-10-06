import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TableExtractionToolsTest(unittest.TestCase):
    def test_missing_camelot_reports_unsupported(self):
        from core.intelligence import table_extraction_tools as tools

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tools,
            '_load_camelot',
            return_value=(None, 'camelot missing'),
        ):
            pdf_path = Path(tmp) / 'report.pdf'
            pdf_path.write_bytes(b'%PDF-1.4')
            result = tools.extract_tables_with_camelot(pdf_path)

        self.assertEqual(result.status, 'unsupported')
        self.assertEqual(result.tables, [])
        self.assertIn('camelot missing', result.error)

    def test_mock_camelot_tablelist_converts_to_table_blocks(self):
        from core.intelligence import table_extraction_tools as tools

        class FakeValues:
            def tolist(self):
                return [
                    ['\u5ba2\u6237\u540d\u79f0', '\u9500\u552e\u989d'],
                    ['Top customer', '12'],
                ]

        class FakeDf:
            values = FakeValues()

        class FakeTable:
            df = FakeDf()
            page = 7
            parsing_report = {'accuracy': 98.0, 'whitespace': 4.0}

        class FakeCamelot:
            @staticmethod
            def read_pdf(path, pages='all'):
                return [FakeTable()]

        with tempfile.TemporaryDirectory() as tmp, patch.object(tools, '_load_camelot', return_value=(FakeCamelot, '')):
            pdf_path = Path(tmp) / 'report.pdf'
            pdf_path.write_bytes(b'%PDF-1.4')
            result = tools.extract_tables_with_camelot(pdf_path, pages='7')

        self.assertEqual(result.status, 'ok')
        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table['page'], 7)
        self.assertEqual(table['header'], ['\u5ba2\u6237\u540d\u79f0', '\u9500\u552e\u989d'])
        self.assertEqual(table['rows'], [['Top customer', '12']])
        self.assertEqual(table['extractor'], 'camelot')
        self.assertGreater(table['confidence'], 0.8)

    def test_compare_table_extractors_reports_candidate_counts(self):
        from core.intelligence.periodic_report_documents import TableExtractionResult
        from core.intelligence import table_extraction_tools as tools

        pdf_tables = [
            {
                'page': 1,
                'header': ['\u5b58\u8d27\u9879\u76ee', '\u8d26\u9762\u4ef7\u503c'],
                'rows': [['\u539f\u6750\u6599', '100']],
                'section_guess': 'inventory',
                'confidence': 0.9,
                'extractor': 'pdfplumber',
            },
        ]
        camelot_tables = [
            {
                'page': 2,
                'header': ['\u6210\u672c\u9879\u76ee', '\u91d1\u989d'],
                'rows': [['\u539f\u6750\u6599', '80']],
                'section_guess': 'cost',
                'confidence': 0.9,
                'extractor': 'camelot',
            },
        ]

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    tools,
                    'extract_tables_with_pdfplumber',
                    return_value=TableExtractionResult('ok', Path(tmp) / 'p.tables.jsonl', pdf_tables, ''),
                ), \
                patch.object(
                    tools,
                    'extract_tables_with_camelot',
                    return_value=TableExtractionResult('ok', Path(tmp) / 'c.tables.jsonl', camelot_tables, ''),
                ):
            report = tools.compare_table_extractors(Path(tmp) / 'report.pdf')

        self.assertEqual(report['pdfplumber_status'], 'ok')
        self.assertEqual(report['camelot_status'], 'ok')
        self.assertEqual(report['pdfplumber_table_count'], 1)
        self.assertEqual(report['camelot_table_count'], 1)
        self.assertGreaterEqual(report['inventory_candidate_count'], 1)
        self.assertGreaterEqual(report['cost_candidate_count'], 1)


if __name__ == '__main__':
    unittest.main()
