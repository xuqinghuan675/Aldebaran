import os
import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DisclosurePdfTest(unittest.TestCase):
    def test_cache_path_is_stable_and_windows_safe(self):
        from core.intelligence.disclosure_pdf import build_pdf_cache_path

        url = 'https://static.cninfo.com.cn/finalpage/2026-01-02/a:b*c?d<e>|f.PDF'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}):
                first = build_pdf_cache_path(url, code='300502', published_at='2026/01/02')
                second = build_pdf_cache_path(url, code='300502', published_at='2026/01/02')

        self.assertEqual(first, second)
        self.assertEqual(first.suffix, '.pdf')
        self.assertTrue(
            str(first.parent).endswith(str(Path('documents') / 'disclosures' / 'announcements'))
        )
        self.assertTrue(str(first).startswith(tmp))
        for char in '<>:"/\\|?*':
            self.assertNotIn(char, first.name)
        self.assertIn('300502', first.name)
        self.assertIn('2026-01-02', first.name)
        self.assertIn(hashlib.sha256(url.encode('utf-8')).hexdigest()[:16], first.name)

    def test_pdf_cache_path_routes_periodic_reports_separately(self):
        from core.intelligence.disclosure_pdf import build_pdf_cache_path

        url = 'https://static.cninfo.com.cn/finalpage/2026-04-29/annual.PDF'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}):
                path = build_pdf_cache_path(
                    url,
                    code='600584',
                    published_at='2026-04-29',
                    document_kind='periodic_report',
                )

        self.assertTrue(
            str(path.parent).endswith(str(Path('documents') / 'disclosures' / 'periodic_reports'))
        )
        self.assertTrue(str(path).startswith(tmp))
        self.assertEqual(path.suffix, '.pdf')

    def test_pdf_cache_path_defaults_to_announcement_for_old_callers(self):
        from core.intelligence.disclosure_pdf import build_pdf_cache_path

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}):
                path = build_pdf_cache_path('https://example.com/default.PDF')

        self.assertTrue(
            str(path.parent).endswith(str(Path('documents') / 'disclosures' / 'announcements'))
        )

    def test_download_pdf_uses_cache_without_http_request(self):
        from core.intelligence.disclosure_pdf import download_pdf

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / 'cached.pdf'
            cache_path.write_bytes(b'%PDF-1.4 cached')
            with patch('core.http_client.get') as http_get:
                result = download_pdf('https://example.com/cached.pdf', cache_path)

        http_get.assert_not_called()
        self.assertEqual(result.status, 'cached')
        self.assertEqual(result.cache_path, cache_path)
        self.assertEqual(result.error, '')

    def test_download_pdf_writes_successful_response(self):
        from core.intelligence.disclosure_pdf import download_pdf

        class Response:
            content = b'%PDF-1.4 body'

            def raise_for_status(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / 'downloaded.pdf'
            with patch('core.http_client.get', return_value=Response()) as http_get:
                result = download_pdf('https://example.com/downloaded.pdf', cache_path)
            payload = cache_path.read_bytes()

        http_get.assert_called_once()
        self.assertEqual(result.status, 'ok')
        self.assertEqual(payload, b'%PDF-1.4 body')
        self.assertEqual(result.error, '')

    def test_download_pdf_converts_http_error(self):
        from core.intelligence.disclosure_pdf import download_pdf

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / 'failed.pdf'
            with patch('core.http_client.get', side_effect=RuntimeError('network failed')):
                result = download_pdf('https://example.com/failed.pdf', cache_path)

        self.assertEqual(result.status, 'download_error')
        self.assertIn('network failed', result.error)
        self.assertFalse(cache_path.exists())

    def test_extract_pdf_text_success_with_mocked_reader(self):
        from core.intelligence.disclosure_pdf import extract_pdf_text

        class Page:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class Reader:
            def __init__(self, stream):
                self.pages = [Page('First page'), Page('Second page')]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ok.pdf'
            path.write_bytes(b'%PDF-1.4 ok')
            with patch('core.intelligence.disclosure_pdf._load_pdf_reader', return_value=(Reader, 'fake', '')):
                result = extract_pdf_text(path)

        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.text, 'First page\n\nSecond page')
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.error, '')

    def test_extract_pdf_text_reports_unsupported_when_library_missing(self):
        from core.intelligence.disclosure_pdf import extract_pdf_text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'unsupported.pdf'
            path.write_bytes(b'%PDF-1.4 body')
            with patch('core.intelligence.disclosure_pdf._load_pdf_reader', return_value=(None, '', 'no pdf parser')):
                result = extract_pdf_text(path)

        self.assertEqual(result.status, 'unsupported')
        self.assertIn('no pdf parser', result.error)
        self.assertEqual(result.text, '')

    def test_extract_pdf_text_reports_extract_error(self):
        from core.intelligence.disclosure_pdf import extract_pdf_text

        class Reader:
            def __init__(self, stream):
                raise ValueError('broken pdf')

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'broken.pdf'
            path.write_bytes(b'not a pdf')
            with patch('core.intelligence.disclosure_pdf._load_pdf_reader', return_value=(Reader, 'fake', '')):
                result = extract_pdf_text(path)

        self.assertEqual(result.status, 'extract_error')
        self.assertIn('broken pdf', result.error)
        self.assertEqual(result.text, '')

    def test_extract_pdf_text_reports_empty_text(self):
        from core.intelligence.disclosure_pdf import extract_pdf_text

        class Page:
            def extract_text(self):
                return '   '

        class Reader:
            def __init__(self, stream):
                self.pages = [Page()]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'empty.pdf'
            path.write_bytes(b'%PDF-1.4 empty')
            with patch('core.intelligence.disclosure_pdf._load_pdf_reader', return_value=(Reader, 'fake', '')):
                result = extract_pdf_text(path)

        self.assertEqual(result.status, 'empty_text')
        self.assertEqual(result.text, '')
        self.assertEqual(result.page_count, 1)

    def test_periodic_report_document_manifest_and_section_files(self):
        from core.intelligence.periodic_report_documents import (
            build_periodic_report_doc_id,
            record_periodic_report_pdf_index,
            write_periodic_report_sections,
        )

        text = (
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u8425\u4e1a\u6536\u5165\u589e\u957f\u3002\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u9500\u552e\u989d\u5360\u6bd4\u63d0\u5347\u3002'
        )

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / 'annual.pdf'
            pdf_path.write_bytes(b'%PDF-1.4 annual report')
            with patch.dict(os.environ, {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}, clear=False):
                doc_id = build_periodic_report_doc_id(
                    code='300308',
                    title='\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                    url='https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
                    local_path=pdf_path,
                )
                sections_result = write_periodic_report_sections(doc_id, text)
                index_record = record_periodic_report_pdf_index(
                    code='300308',
                    name='\u4e2d\u9645\u65ed\u521b',
                    report_type='annual',
                    title='\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                    url='https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
                    local_path=pdf_path,
                    downloaded_at='2026-07-04T10:00:00',
                    parse_status='ok',
                    error='',
                    doc_id=doc_id,
                )

                index_path = Path(tmp) / 'documents' / 'metadata' / 'pdf_index.jsonl'
                section_path = (
                    Path(tmp)
                    / 'documents'
                    / 'extracted_text'
                    / 'periodic_reports'
                    / f'{doc_id}.sections.json'
                )

            self.assertEqual(sections_result.status, 'ok')
            self.assertTrue(section_path.exists())
            sections = json.loads(section_path.read_text(encoding='utf-8'))
            self.assertTrue({'main_business', 'customer_supplier'} <= {item['section_id'] for item in sections})
            self.assertTrue(index_path.exists())
            line = index_path.read_text(encoding='utf-8').strip().splitlines()[-1]
            payload = json.loads(line)

        self.assertEqual(payload['doc_id'], doc_id)
        self.assertEqual(payload['code'], '300308')
        self.assertEqual(payload['name'], '\u4e2d\u9645\u65ed\u521b')
        self.assertEqual(payload['report_type'], 'annual')
        self.assertEqual(payload['parse_status'], 'ok')
        self.assertEqual(payload['sha256'], hashlib.sha256(b'%PDF-1.4 annual report').hexdigest())
        self.assertEqual(index_record['local_path'], str(pdf_path))

    def test_periodic_report_table_extraction_reports_unsupported_without_pdfplumber(self):
        from core.intelligence.periodic_report_documents import extract_periodic_report_tables

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / 'annual.pdf'
            pdf_path.write_bytes(b'%PDF-1.4 annual report')
            with patch('core.intelligence.periodic_report_documents._load_pdfplumber', return_value=(None, 'missing')):
                result = extract_periodic_report_tables('doc123', pdf_path)

        self.assertEqual(result.status, 'unsupported')
        self.assertEqual(result.tables, [])
        self.assertIn('missing', result.error)

    def test_module_does_not_introduce_technical_analysis_terms(self):
        source = Path('core/intelligence/disclosure_pdf.py').read_text(encoding='utf-8')
        forbidden = [
            ''.join(['K', '\u7ebf']),
            ''.join(['M', 'A', 'C', 'D']),
            ''.join(['R', 'S', 'I']),
            ''.join(['\u4e70', '\u70b9']),
            ''.join(['\u6b62', '\u635f']),
        ]

        for term in forbidden:
            self.assertNotIn(term, source)


if __name__ == '__main__':
    unittest.main()
