import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PeriodicReportDocumentTest(unittest.TestCase):
    def test_missing_pdfplumber_reports_unsupported(self):
        from core.intelligence import periodic_report_documents as docs

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}, clear=False), \
                patch.object(
                    docs,
                    '_load_pdfplumber',
                    return_value=(None, 'pdfplumber missing; table extraction disabled'),
                ):
            result = docs.extract_periodic_report_tables('doc_missing', Path(tmp) / 'report.pdf')

        self.assertEqual(result.status, 'unsupported')
        self.assertEqual(result.tables, [])
        self.assertIn('pdfplumber missing', result.error)

    def test_mocked_pdfplumber_writes_table_jsonl_with_section_guess(self):
        from core.intelligence import periodic_report_documents as docs

        class FakeTable:
            bbox = [1, 2, 3, 4]

            def extract(self):
                return [
                    ['客户名称', '销售额', '占年度销售总额比例'],
                    ['第一名客户', '12亿元', '21%'],
                ]

        class FakePage:
            def find_tables(self):
                return [FakeTable()]

        class FakePdf:
            pages = [FakePage()]

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakePdfplumber:
            @staticmethod
            def open(path):
                return FakePdf()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}, clear=False), \
                patch.object(docs, '_load_pdfplumber', return_value=(FakePdfplumber, '')):
            pdf_path = Path(tmp) / 'report.pdf'
            pdf_path.write_bytes(b'%PDF-1.4')
            result = docs.extract_periodic_report_tables('doc_ok', pdf_path)

            self.assertEqual(result.status, 'ok')
            self.assertEqual(len(result.tables), 1)
            self.assertTrue(result.path.exists())
            rows = [
                json.loads(line)
                for line in result.path.read_text(encoding='utf-8').splitlines()
                if line.strip()
            ]

        self.assertEqual(rows[0]['page'], 1)
        self.assertEqual(rows[0]['section_guess'], 'customer_supplier')
        self.assertEqual(rows[0]['header'], ['客户名称', '销售额', '占年度销售总额比例'])
        self.assertEqual(rows[0]['rows'], [['第一名客户', '12亿元', '21%']])
        self.assertEqual(rows[0]['extractor'], 'pdfplumber')


if __name__ == '__main__':
    unittest.main()
