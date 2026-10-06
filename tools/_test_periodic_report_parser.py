import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _by_field(items):
    out = {}
    for item in items:
        out.setdefault(item.field, []).append(item)
    return out


def _normalized_periodic_item(
    field,
    *,
    item_id='raw:1',
    summary='',
    quality=0.8,
    values=None,
    raw_ref='periodic_report:annual:table:page1:field:hash',
    category='periodic_report_body',
):
    from core.intelligence.models import NormalizedIntelItem

    layer_by_field = {
        'business_segments': 'financial',
        'customer_concentration': 'customer_supplier',
        'supplier_concentration': 'customer_supplier',
    }
    return NormalizedIntelItem(
        id=item_id,
        source_id='requests:company_announcements',
        title='Stock A 2025 annual report',
        summary=summary or f'{field} table row',
        layer=layer_by_field.get(field, field),
        direction='neutral',
        related_codes=['600584'],
        related_names=['Stock A'],
        related_sectors=[],
        evidence_type='periodic_report',
        published_at='2026-04-20',
        fetched_at='2026-07-05T10:00:00',
        url='https://example.com/report.pdf',
        source_url='https://example.com/report.pdf',
        trust_level='primary',
        confidence=8,
        category=category,
        raw_ref=raw_ref,
        metadata={
            'field': field,
            'source_title': 'Stock A 2025 annual report',
            'table_quality_score': quality,
            'table_values': dict(values or {}),
            'table_row_index': 1,
        },
    )


class PeriodicReportParserTest(unittest.TestCase):
    def test_annual_report_title_detects_annual(self):
        from core.intelligence.periodic_report_parser import detect_report_type

        self.assertEqual(
            detect_report_type('\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a', ''),
            'annual',
        )

    def test_semiannual_report_title_detects_semiannual(self):
        from core.intelligence.periodic_report_parser import detect_report_type

        self.assertEqual(
            detect_report_type('\u65b0\u6613\u76db2026\u5e74\u534a\u5e74\u5ea6\u62a5\u544a', ''),
            'semiannual',
        )

    def test_quarterly_report_title_detects_quarterly(self):
        from core.intelligence.periodic_report_parser import detect_report_type

        self.assertEqual(
            detect_report_type('\u957f\u7535\u79d1\u62802026\u5e74\u7b2c\u4e09\u5b63\u5ea6\u62a5\u544a', ''),
            'quarterly',
        )
        self.assertEqual(detect_report_type('2026\u5e74\u4e00\u5b63\u62a5', ''), 'quarterly')
        self.assertEqual(detect_report_type('2026\u5e74\u4e09\u5b63\u62a5', ''), 'quarterly')

    def test_main_business_composition_emits_financial_and_demand(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u516c\u53f8\u4e3b\u8425\u4ea7\u54c1\u4e3a\u9ad8\u901f\u5149\u6a21\u5757\uff0c'
            '\u672c\u671f\u5b9e\u73b0\u8425\u4e1a\u6536\u5165100\u4ebf\u5143\uff0c'
            '\u4ea7\u54c1\u9500\u552e\u91cf\u540c\u6bd4\u589e\u957f\uff0c'
            '\u4e0b\u6e38\u9700\u6c42\u6301\u7eed\u6539\u5584\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text, title='2025\u5e74\u5e74\u5ea6\u62a5\u544a'))

        self.assertIn('financial', fields)
        self.assertIn('demand', fields)
        self.assertEqual(fields['financial'][0].category, 'main_business_composition')
        self.assertEqual(fields['demand'][0].layer, 'demand')

    def test_region_or_overseas_revenue_emits_export(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u5206\u5730\u533a\u6536\u5165\n'
            '\u5883\u5916\u6536\u5165\u5360\u6bd4\u63d0\u5347\uff0c'
            '\u6d77\u5916\u4e1a\u52a1\u548c\u5916\u9500\u5ba2\u6237\u8d21\u732e\u589e\u52a0\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertIn('export', fields)
        self.assertEqual(fields['export'][0].category, 'region_revenue')
        self.assertIn('\u5883\u5916\u6536\u5165', fields['export'][0].evidence_text)

    def test_top_customers_and_suppliers_emit_customer_supplier(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u9500\u552e\u989d\u5360\u5e74\u5ea6\u9500\u552e\u603b\u989d\u6bd4\u4f8b\u4e3a42%\u3002'
            '\u524d\u4e94\u5927\u4f9b\u5e94\u5546\u91c7\u8d2d\u989d\u5360\u6bd4\u4e3a38%\uff0c'
            '\u5ba2\u6237\u96c6\u4e2d\u5ea6\u6709\u6240\u63d0\u5347\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertIn('customer_supplier', fields)
        self.assertEqual(fields['customer_supplier'][0].category, 'top_customer_supplier')
        self.assertTrue(
            {'\u524d\u4e94\u5927\u5ba2\u6237', '\u524d\u4e94\u5927\u4f9b\u5e94\u5546'}
            & set(fields['customer_supplier'][0].matched_keywords)
        )

    def test_inventory_and_write_down_emit_inventory(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u5b58\u8d27\n'
            '\u671f\u672b\u5b58\u8d27\u4f59\u989d\u4e3a12\u4ebf\u5143\uff0c'
            '\u516c\u53f8\u8ba1\u63d0\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\uff0c'
            '\u5e93\u5b58\u5468\u8f6c\u4fdd\u6301\u7a33\u5b9a\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertIn('inventory', fields)
        self.assertEqual(fields['inventory'][0].category, 'inventory_ar_margin')
        self.assertIn('\u8dcc\u4ef7\u51c6\u5907', fields['inventory'][0].evidence_text)

    def test_operating_cost_and_raw_materials_emit_cost(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u6210\u672c\u6784\u6210\n'
            '\u516c\u53f8\u8425\u4e1a\u6210\u672c\u4e2d\u539f\u6750\u6599\u6210\u672c\u5360\u6bd4\u8f83\u9ad8\uff0c'
            '\u4eba\u5de5\u53ca\u5236\u9020\u8d39\u7528\u4fdd\u6301\u7a33\u5b9a\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertIn('cost', fields)
        self.assertEqual(fields['cost'][0].category, 'cost_structure')
        self.assertIn('\u539f\u6750\u6599', fields['cost'][0].matched_keywords)

    def test_industry_and_price_competition_emit_competition(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u884c\u4e1a\u7ade\u4e89\u683c\u5c40\n'
            '\u884c\u4e1a\u7ade\u4e89\u52a0\u5267\uff0c'
            '\u540c\u884c\u4f01\u4e1a\u4ef7\u683c\u7ade\u4e89\u53ef\u80fd\u5f71\u54cd\u5e02\u573a\u4efd\u989d\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertIn('competition', fields)
        self.assertEqual(fields['competition'][0].category, 'competition_section')
        self.assertIn('\u4ef7\u683c\u7ade\u4e89', fields['competition'][0].evidence_text)

    def test_risk_factors_emit_risk(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u98ce\u9669\u56e0\u7d20\n'
            '\u516c\u53f8\u9762\u4e34\u7ecf\u8425\u98ce\u9669\u3001'
            '\u6c47\u7387\u98ce\u9669\u3001\u8d38\u6613\u98ce\u9669\u4ee5\u53ca\u5ba2\u6237\u96c6\u4e2d\u98ce\u9669\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertIn('risk', fields)
        self.assertEqual(fields['risk'][0].category, 'annual_report_risk')
        self.assertIn('\u6c47\u7387\u98ce\u9669', fields['risk'][0].evidence_text)

    def test_section_index_records_positions_for_key_report_sections(self):
        from core.intelligence.periodic_report_parser import build_periodic_report_section_index

        text = (
            '\u7b2c\u4e09\u8282 \u7ba1\u7406\u5c42\u8ba8\u8bba\u4e0e\u5206\u6790\n'
            '\u516c\u53f8\u7ecf\u8425\u60c5\u51b5\u7a33\u5b9a\u3002\n'
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u516c\u53f8\u4e3b\u8981\u4e1a\u52a1\u4e3a\u9ad8\u901f\u5149\u6a21\u5757\u3002\n'
            '\u5206\u884c\u4e1a\u3001\u5206\u4ea7\u54c1\u3001\u5206\u5730\u533a\u60c5\u51b5\n'
            '\u5883\u5916\u6536\u5165\u5360\u6bd4\u63d0\u5347\u3002\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546\n'
            '\u5ba2\u6237\u96c6\u4e2d\u5ea6\u63d0\u5347\u3002\n'
            '\u5b58\u8d27\n'
            '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\u589e\u52a0\u3002\n'
            '\u6210\u672c\u6784\u6210\n'
            '\u8425\u4e1a\u6210\u672c\u4e2d\u539f\u6750\u6599\u5360\u6bd4\u8f83\u9ad8\u3002\n'
            '\u98ce\u9669\u56e0\u7d20\n'
            '\u516c\u53f8\u9762\u4e34\u6c47\u7387\u98ce\u9669\u3002\n'
            '\u8d22\u52a1\u62a5\u8868\n'
            '\u5408\u5e76\u8d44\u4ea7\u8d1f\u503a\u8868\u3002'
        )

        sections = build_periodic_report_section_index(text)
        by_id = {section['section_id']: section for section in sections}

        self.assertTrue({
            'management_discussion',
            'main_business',
            'business_segments',
            'customer_supplier',
            'inventory',
            'cost',
            'risk_factors',
            'financial_statements',
        } <= set(by_id))
        self.assertLess(by_id['main_business']['start_char'], by_id['customer_supplier']['start_char'])
        self.assertGreater(by_id['financial_statements']['end_char'], by_id['financial_statements']['start_char'])

    def test_table_blocks_emit_core_periodic_report_fields(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 18,
                'section_guess': 'customer_supplier',
                'header': ['\u5ba2\u6237\u540d\u79f0', '\u9500\u552e\u989d', '\u5360\u5e74\u5ea6\u9500\u552e\u603b\u989d\u6bd4\u4f8b'],
                'rows': [['\u7b2c\u4e00\u540d\u5ba2\u6237', '12\u4ebf\u5143', '21%']],
                'confidence': 0.9,
                'extractor': 'pdfplumber',
            },
            {
                'page': 33,
                'section_guess': 'inventory',
                'header': ['\u9879\u76ee', '\u671f\u672b\u4f59\u989d', '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907'],
                'rows': [['\u5b58\u8d27', '8\u4ebf\u5143', '0.4\u4ebf\u5143']],
                'confidence': 0.86,
                'extractor': 'pdfplumber',
            },
            {
                'page': 20,
                'section_guess': 'business_segments',
                'header': ['\u5206\u5730\u533a', '\u8425\u4e1a\u6536\u5165', '\u6bdb\u5229\u7387'],
                'rows': [['\u5883\u5916', '31\u4ebf\u5143', '38%']],
                'confidence': 0.88,
                'extractor': 'pdfplumber',
            },
            {
                'page': 21,
                'section_guess': 'cost',
                'header': ['\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u5360\u6bd4'],
                'rows': [['\u539f\u6750\u6599', '42\u4ebf\u5143', '66%']],
                'confidence': 0.84,
                'extractor': 'pdfplumber',
            },
        ]

        fields = _by_field(parse_periodic_report_text(
            '',
            title='\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            table_blocks=tables,
        ))

        self.assertTrue({'customer_concentration', 'inventory', 'business_segments', 'export', 'cost'} <= set(fields))
        self.assertEqual(fields['customer_concentration'][0].section, 'customer_supplier')
        self.assertEqual(fields['inventory'][0].category, 'inventory_ar_margin')
        self.assertEqual(fields['cost'][0].category, 'cost_structure')
        self.assertIn('\u5883\u5916', fields['export'][0].evidence_text)
        self.assertGreaterEqual(fields['business_segments'][0].table_quality_score, fields['cost'][0].table_quality_score)
        self.assertTrue(all(item.raw_ref.startswith('periodic_report:annual:') for values in fields.values() for item in values))

    def test_table_evidence_preserves_page_header_and_source_metadata(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        table = {
            'page': 27,
            'section_guess': 'inventory',
            'header': ['\u9879\u76ee', '\u8d26\u9762\u4f59\u989d', '\u8dcc\u4ef7\u51c6\u5907', '\u8d26\u9762\u4ef7\u503c'],
            'rows': [['\u5e93\u5b58\u5546\u54c1', '12\u4ebf\u5143', '0.8\u4ebf\u5143', '11.2\u4ebf\u5143']],
            'confidence': 0.91,
            'extractor': 'pdfplumber',
        }

        evidence = _by_field(parse_periodic_report_text(
            '',
            title='\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            source_url='https://example.com/annual.pdf',
            table_blocks=[table],
        ))['inventory'][0]

        self.assertEqual(evidence.page, 27)
        self.assertEqual(evidence.section, 'inventory')
        self.assertEqual(evidence.table_header, table['header'])
        self.assertEqual(evidence.source_url, 'https://example.com/annual.pdf')
        self.assertIn('\u8d26\u9762\u4ef7\u503c', evidence.matched_keywords)
        self.assertIn('page27', evidence.raw_ref)
        self.assertGreaterEqual(evidence.confidence, 8)

    def test_table_classifier_does_not_use_query_name_or_stock_name_as_evidence(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 5,
                'section_guess': 'table',
                'header': ['\u80a1\u7968\u540d\u79f0', '\u67e5\u8be2\u5173\u952e\u8bcd'],
                'rows': [['\u957f\u7535\u79d1\u6280', '\u5b58\u8d27 \u8425\u4e1a\u6210\u672c \u5883\u5916']],
                'confidence': 0.8,
            },
        ]

        self.assertEqual(parse_periodic_report_text('', name='\u957f\u7535\u79d1\u6280', table_blocks=tables), [])

    def test_table_classifier_uses_precise_headers_not_broad_row_keywords(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 8,
                'section_guess': 'table',
                'header': ['\u9879\u76ee', '\u5907\u6ce8'],
                'rows': [['\u7b2c\u4e00\u884c', '\u5907\u6ce8\u5305\u542b\u5b58\u8d27\u3001\u8425\u4e1a\u6210\u672c\u548c\u5883\u5916\u7b49\u67e5\u8be2\u8bcd']],
                'confidence': 0.82,
            },
        ]

        self.assertEqual(parse_periodic_report_text('', table_blocks=tables), [])

    def test_table_classifier_does_not_treat_ai_product_text_as_labor_cost(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 12,
                'section_guess': 'business_segments',
                'header': ['400G\u5149\u6a21\u5757', '\u4eba\u5de5\u667a\u80fd\u7b97\u529b\u96c6\u7fa4'],
                'rows': [['\u5e94\u7528\u573a\u666f', '\u4e91\u6570\u636e\u4e2d\u5fc3']],
                'confidence': 0.86,
            },
        ]

        self.assertNotIn('cost', {item.field for item in parse_periodic_report_text('', table_blocks=tables)})

    def test_customer_table_only_emits_customer_concentration(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 18,
                'section_guess': 'customer_supplier',
                'header': [
                    '\u524d\u4e94\u540d\u5ba2\u6237\u5408\u8ba1\u9500\u552e\u91d1\u989d',
                    '\u5360\u5e74\u5ea6\u9500\u552e\u603b\u989d\u6bd4\u4f8b',
                ],
                'rows': [['\u524d\u4e94\u540d\u5ba2\u6237\u5408\u8ba1', '120,000.00', '42%']],
                'confidence': 0.91,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))

        self.assertIn('customer_concentration', fields)
        self.assertNotIn('supplier_concentration', fields)
        self.assertNotIn('customer_supplier', fields)
        self.assertEqual(fields['customer_concentration'][0].layer, 'customer_supplier')
        self.assertEqual(fields['customer_concentration'][0].category, 'customer_concentration')
        self.assertIn('\u9500\u552e\u91d1\u989d', fields['customer_concentration'][0].matched_keywords)

    def test_supplier_table_only_emits_supplier_concentration(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 19,
                'section_guess': 'customer_supplier',
                'header': [
                    '\u524d\u4e94\u540d\u4f9b\u5e94\u5546\u5408\u8ba1\u91c7\u8d2d\u91d1\u989d',
                    '\u5360\u5e74\u5ea6\u91c7\u8d2d\u603b\u989d\u6bd4\u4f8b',
                ],
                'rows': [['\u524d\u4e94\u540d\u4f9b\u5e94\u5546\u5408\u8ba1', '88,000.00', '37%']],
                'confidence': 0.9,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))

        self.assertIn('supplier_concentration', fields)
        self.assertNotIn('customer_concentration', fields)
        self.assertNotIn('customer_supplier', fields)
        self.assertEqual(fields['supplier_concentration'][0].layer, 'customer_supplier')
        self.assertEqual(fields['supplier_concentration'][0].category, 'supplier_concentration')
        self.assertIn('\u91c7\u8d2d\u91d1\u989d', fields['supplier_concentration'][0].matched_keywords)

    def test_region_table_emits_export_only_for_overseas_rows(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 20,
                'section_guess': 'business_segments',
                'header': [
                    '\u5206\u5730\u533a',
                    '\u8425\u4e1a\u6536\u5165',
                    '\u8425\u4e1a\u6210\u672c',
                    '\u6bdb\u5229\u7387',
                    '\u8425\u4e1a\u6536\u5165\u6bd4\u4e0a\u5e74\u540c\u671f\u589e\u51cf',
                    '\u8425\u4e1a\u6210\u672c\u6bd4\u4e0a\u5e74\u540c\u671f\u589e\u51cf',
                    '\u6bdb\u5229\u7387\u6bd4\u4e0a\u5e74\u540c\u671f\u589e\u51cf',
                ],
                'rows': [
                    ['\u534e\u4e1c\u5730\u533a', '10,000', '6,500', '35%', '12%', '9%', '1.2%'],
                    ['\u5883\u5916\u5730\u533a', '30,000', '18,000', '40%', '31%', '27%', '2.5%'],
                ],
                'confidence': 0.93,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))

        self.assertIn('business_segments', fields)
        self.assertEqual(len(fields['business_segments']), 2)
        self.assertEqual(len(fields['export']), 1)
        self.assertIn('\u5883\u5916\u5730\u533a', fields['export'][0].evidence_text)
        self.assertNotIn('\u534e\u4e1c\u5730\u533a', fields['export'][0].evidence_text)
        values = fields['business_segments'][1].table_values
        self.assertEqual(values['segment_name'], '\u5883\u5916\u5730\u533a')
        self.assertEqual(values['revenue'], '30,000')
        self.assertEqual(values['cost'], '18,000')
        self.assertEqual(values['gross_margin'], '40%')
        self.assertEqual(values['revenue_yoy'], '31%')
        self.assertEqual(values['cost_yoy'], '27%')
        self.assertEqual(values['gross_margin_change'], '2.5%')

    def test_product_segment_table_emits_business_segments_without_export(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 16,
                'section_guess': 'business_segments',
                'header': [
                    '\u5206\u4ea7\u54c1',
                    '\u8425\u4e1a\u6536\u5165',
                    '\u8425\u4e1a\u6210\u672c',
                    '\u6bdb\u5229\u7387',
                ],
                'rows': [['400G\u5149\u6a21\u5757', '80,000', '50,000', '37.5%']],
                'confidence': 0.92,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))

        self.assertIn('business_segments', fields)
        self.assertNotIn('export', fields)
        self.assertEqual(fields['business_segments'][0].layer, 'financial')
        self.assertEqual(fields['business_segments'][0].table_values['segment_name'], '400G\u5149\u6a21\u5757')

    def test_segment_header_rows_do_not_become_business_segment_evidence(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 16,
                'section_guess': 'business_segments',
                'header': ['\u5206\u884c\u4e1a', '\u8425\u4e1a\u6536\u5165', '\u8425\u4e1a\u6210\u672c', '\u6bdb\u5229\u7387'],
                'rows': [
                    ['\u5206\u4ea7\u54c1', '', '', ''],
                    ['\u8425\u4e1a\u6536\u5165', '200,000', '120,000', '40%'],
                    ['\u7535\u5b50\u5143\u5668\u4ef6', '200,000', '120,000', '40%'],
                ],
                'confidence': 0.91,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))
        names = {item.table_values['segment_name'] for item in fields['business_segments']}

        self.assertEqual(names, {'\u7535\u5b50\u5143\u5668\u4ef6'})

    def test_inventory_table_extracts_inventory_components(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 33,
                'section_guess': 'inventory',
                'header': ['\u9879\u76ee', '\u8d26\u9762\u4f59\u989d', '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907', '\u8d26\u9762\u4ef7\u503c'],
                'rows': [
                    ['\u539f\u6750\u6599', '12,000', '300', '11,700'],
                    ['\u5e93\u5b58\u5546\u54c1', '20,000', '800', '19,200'],
                    ['\u53d1\u51fa\u5546\u54c1', '8,000', '100', '7,900'],
                ],
                'confidence': 0.9,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))

        self.assertEqual(len(fields['inventory']), 3)
        names = {item.table_values['item_name'] for item in fields['inventory']}
        self.assertEqual(names, {'\u539f\u6750\u6599', '\u5e93\u5b58\u5546\u54c1', '\u53d1\u51fa\u5546\u54c1'})
        self.assertEqual(fields['inventory'][0].table_values['write_down'], '300')
        self.assertEqual(fields['inventory'][0].table_values['book_value'], '11,700')

    def test_inventory_explanatory_rows_without_amounts_are_not_inventory_evidence(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 123,
                'section_guess': 'inventory',
                'header': ['\u9879\u76ee', '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907'],
                'rows': [[
                    '\u672c\u516c\u53f8\u6839\u636e\u5b58\u8d27\u6210\u672c\u9ad8\u4e8e\u5176\u53ef\u53d8\u73b0\u51c0\u503c\u7684\u5dee\u989d\u3001\u539f\u6750\u6599\u7684\u9884\u8ba1\u4f7f\u7528\u60c5\u51b5',
                    '\u4ee5\u524d\u671f\u95f4\u8ba1\u63d0\u4e86\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\u7684\u5b58\u8d27\u53ef\u53d8\u73b0\u51c0\u503c\u4e0a\u5347',
                ]],
                'confidence': 0.88,
            },
        ]

        self.assertNotIn('inventory', {item.field for item in parse_periodic_report_text('', table_blocks=tables)})

    def test_cost_structure_table_emits_cost_components(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 44,
                'section_guess': 'cost',
                'header': ['\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u5360\u6bd4'],
                'rows': [
                    ['\u539f\u6750\u6599', '42,000', '66%'],
                    ['\u4eba\u5de5\u6210\u672c', '5,000', '8%'],
                    ['\u5236\u9020\u8d39\u7528', '9,000', '14%'],
                    ['\u8fd0\u8f93\u8d39', '2,000', '3%'],
                ],
                'confidence': 0.88,
            },
        ]

        fields = _by_field(parse_periodic_report_text('', table_blocks=tables))

        self.assertEqual(len(fields['cost']), 4)
        names = {item.table_values['item_name'] for item in fields['cost']}
        self.assertEqual(names, {'\u539f\u6750\u6599', '\u4eba\u5de5\u6210\u672c', '\u5236\u9020\u8d39\u7528', '\u8fd0\u8f93\u8d39'})
        self.assertTrue(all(item.category == 'cost_structure' for item in fields['cost']))

    def test_cost_table_header_only_revenue_cost_margin_row_does_not_emit_cost(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 22,
                'section_guess': 'cost',
                'header': ['\u8425\u4e1a\u6536\u5165\u6bd4\u4e0a\u5e74', '\u8425\u4e1a\u6210\u672c\u6bd4\u4e0a\u5e74', '\u6bdb\u5229\u7387\u6bd4\u4e0a\u5e74'],
                'rows': [['\u8425\u4e1a\u6536\u5165', '\u8425\u4e1a\u6210\u672c', '\u6bdb\u5229\u7387']],
                'confidence': 0.83,
            },
        ]

        self.assertNotIn('cost', {item.field for item in parse_periodic_report_text('', table_blocks=tables)})

    def test_table_quality_score_orders_core_tables_before_noise(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 3,
                'section_guess': 'financial_statements',
                'header': ['\u9879\u76ee', '\u672c\u671f\u6570', '\u4e0a\u671f\u6570'],
                'rows': [['\u6d41\u52a8\u8d44\u4ea7', '100', '90']],
                'confidence': 0.7,
            },
            {
                'page': 20,
                'section_guess': 'business_segments',
                'header': ['\u5206\u4ea7\u54c1', '\u8425\u4e1a\u6536\u5165', '\u8425\u4e1a\u6210\u672c', '\u6bdb\u5229\u7387'],
                'rows': [
                    ['800G\u5149\u6a21\u5757', '90,000', '52,000', '42%'],
                    ['400G\u5149\u6a21\u5757', '50,000', '32,000', '36%'],
                ],
                'confidence': 0.95,
            },
            {
                'page': 21,
                'section_guess': 'cost',
                'header': ['\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u5360\u6bd4'],
                'rows': [['\u539f\u6750\u6599', '42,000', '66%']],
                'confidence': 0.88,
            },
        ]

        evidence = parse_periodic_report_text('', table_blocks=tables)

        self.assertEqual(evidence[0].field, 'business_segments')
        self.assertGreaterEqual(evidence[0].table_quality_score, evidence[-1].table_quality_score)
        self.assertNotIn('financial', {item.field for item in evidence if item.page == 3})

    def test_content_company_missing_inventory_or_supplier_table_reports_reason(self):
        from core.intelligence.periodic_report_parser import (
            parse_periodic_report_text,
            periodic_report_field_missing_reasons,
        )

        tables = [
            {
                'page': 16,
                'section_guess': 'business_segments',
                'header': ['\u5206\u4ea7\u54c1', '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165', '\u6bdb\u5229\u7387'],
                'rows': [['\u56fe\u7247\u6388\u6743', '8\u4ebf\u5143', '58%']],
                'confidence': 0.9,
            },
        ]
        sections = [
            {'section_id': 'customer_supplier', 'text': '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546'},
            {'section_id': 'inventory', 'text': '\u5b58\u8d27'},
        ]
        evidence = parse_periodic_report_text('', table_blocks=tables, sections=sections)

        missing = periodic_report_field_missing_reasons(
            evidence,
            sections=sections,
            table_blocks=tables,
        )

        self.assertIn('business_segments', {item.field for item in evidence})
        self.assertNotIn('export', {item.field for item in evidence})
        self.assertEqual(missing['customer_supplier'], 'section_without_matching_table')
        self.assertEqual(missing['inventory'], 'section_without_matching_table')

    def test_table_missing_reasons_distinguish_empty_rows_and_unmatched_header(self):
        from core.intelligence.periodic_report_parser import (
            parse_periodic_report_text,
            periodic_report_field_missing_reasons,
        )

        tables = [
            {
                'page': 41,
                'section_guess': 'customer_supplier',
                'header': ['\u9879\u76ee'],
                'rows': [],
                'confidence': 0.7,
            },
            {
                'page': 42,
                'section_guess': 'inventory',
                'header': ['\u9879\u76ee', '\u672c\u671f\u6570'],
                'rows': [['\u5176\u4ed6', '100']],
                'confidence': 0.7,
            },
        ]
        evidence = parse_periodic_report_text('', table_blocks=tables)

        missing = periodic_report_field_missing_reasons(evidence, table_blocks=tables)

        self.assertEqual(missing['customer_supplier'], 'table_rows_empty')
        self.assertEqual(missing['inventory'], 'table_present_but_header_unmatched')

    def test_asset_composition_table_does_not_emit_inventory_from_single_stock_row(self):
        from core.intelligence.periodic_report_parser import (
            parse_periodic_report_text,
            periodic_report_field_missing_reasons,
        )

        tables = [
            {
                'page': 29,
                'section_guess': 'inventory',
                'header': ['2025\u5e74\u672b', '2025\u5e74\u521d'],
                'rows': [
                    ['\u8d27\u5e01\u8d44\u91d1', '489,231,635.48', '533,519,752.08'],
                    ['\u5b58\u8d27', '1,063,000.00', '900,000.00'],
                ],
                'confidence': 0.82,
            },
        ]
        evidence = parse_periodic_report_text('', table_blocks=tables)
        missing = periodic_report_field_missing_reasons(evidence, table_blocks=tables)

        self.assertNotIn('inventory', {item.field for item in evidence})
        self.assertEqual(missing['inventory'], 'table_present_but_header_unmatched')

    def test_aging_table_does_not_emit_inventory_from_book_balance_header(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 143,
                'section_guess': 'table',
                'header': ['\u8d26\u9f84', '\u671f\u672b\u8d26\u9762\u4f59\u989d', '\u671f\u521d\u8d26\u9762\u4f59\u989d'],
                'rows': [['1\u5e74\u4ee5\u5185', '130,524,949.39', '141,889,552.46']],
                'confidence': 0.82,
            },
        ]

        self.assertNotIn('inventory', {item.field for item in parse_periodic_report_text('', table_blocks=tables)})

    def test_equity_investment_book_value_table_does_not_emit_inventory(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        tables = [
            {
                'page': 194,
                'section_guess': 'table',
                'header': ['\u9879\u76ee', '\u6743\u76ca\u5de5\u5177\u6295\u8d44\u8d26\u9762\u4ef7\u503c', '\u80a1\u4e1c\u6743\u76ca'],
                'rows': [['\u672c\u5e74', '14,899,113.37', '94,291,630.51']],
                'confidence': 0.82,
            },
        ]

        self.assertNotIn('inventory', {item.field for item in parse_periodic_report_text('', table_blocks=tables)})

    def test_missing_reasons_report_absent_table_or_section_fields(self):
        from core.intelligence.periodic_report_parser import (
            parse_periodic_report_text,
            periodic_report_field_missing_reasons,
        )

        evidence = parse_periodic_report_text(
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u516c\u53f8\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c\u4e3b\u8425\u4ea7\u54c1\u9500\u552e\u91cf\u63d0\u5347\u3002',
            title='\u65b0\u6613\u76db2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            table_blocks=[],
        )

        missing = periodic_report_field_missing_reasons(
            evidence,
            sections=[],
            table_blocks=[],
        )

        self.assertNotIn('financial', missing)
        self.assertNotIn('demand', missing)
        self.assertEqual(missing['customer_supplier'], 'no_matching_table_or_section')
        self.assertEqual(missing['inventory'], 'no_matching_table_or_section')
        self.assertEqual(missing['cost'], 'no_matching_table_or_section')

    def test_multi_field_text_keeps_distinct_evidence(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        text = (
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c\u4e3b\u8425\u4ea7\u54c1\u9500\u552e\u91cf\u63d0\u5347\u3002'
            '\u5206\u5730\u533a\u6536\u5165\u4e2d\u5883\u5916\u6536\u5165\u589e\u52a0\u3002'
            '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\u6709\u6240\u589e\u52a0\u3002'
            '\u884c\u4e1a\u7ade\u4e89\u52a0\u5267\u3002'
        )

        fields = _by_field(parse_periodic_report_text(text))

        self.assertTrue({'financial', 'demand', 'export', 'inventory', 'competition'} <= set(fields))

    def test_long_report_outputs_snippets_not_full_text(self):
        from core.intelligence.periodic_report_parser import (
            extract_periodic_report_sentences,
            parse_periodic_report_text,
        )

        filler = '\u65e0\u5173\u5e74\u62a5\u80cc\u666f\u63cf\u8ff0' * 120
        hit = '\u516c\u53f8\u524d\u4e94\u5927\u5ba2\u6237\u9500\u552e\u989d\u5360\u6bd4\u4e3a42%\u3002'
        text = filler + '\u3002' + hit + filler

        snippets = extract_periodic_report_sentences(text, ['\u524d\u4e94\u5927\u5ba2\u6237'], max_sentences=1)
        evidence = _by_field(parse_periodic_report_text(text))['customer_supplier'][0]

        self.assertEqual(snippets, [hit])
        self.assertLess(len(evidence.evidence_text), len(text) // 4)
        self.assertIn('\u524d\u4e94\u5927\u5ba2\u6237', evidence.evidence_text)

    def test_technical_analysis_terms_do_not_generate_evidence(self):
        from core.intelligence.periodic_report_parser import parse_periodic_report_text

        technical = ''.join(['M', 'A', 'C', 'D'])
        text = (
            technical
            + '\u91d1\u53c9\u540e\u51fa\u73b0'
            + ''.join(['\u4e70', '\u70b9'])
            + '\uff0c'
            + ''.join(['\u6b62', '\u635f'])
            + '\u4f4d\u9700\u8981\u5173\u6ce8\uff0cRSI\u4e5f\u8fdb\u5165\u5f3a\u52bf\u533a\u3002'
        )

        self.assertEqual(parse_periodic_report_text(text), [])

    def test_periodic_evidence_to_normalized_item_preserves_fields(self):
        from core.intelligence.models import NormalizedIntelItem
        from core.intelligence.periodic_report_parser import (
            parse_periodic_report_text,
            periodic_evidence_to_normalized_item,
        )

        text = '\u5883\u5916\u6536\u5165\u589e\u957f\uff0c\u6d77\u5916\u4e1a\u52a1\u8d21\u732e\u63d0\u5347\u3002'
        evidence = _by_field(parse_periodic_report_text(
            text,
            title='\u65b0\u6613\u76db2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            source_url='https://example.com/report.pdf',
            published_at='2026-04-20',
            code='300502',
            name='\u65b0\u6613\u76db',
        ))['export'][0]

        item = periodic_evidence_to_normalized_item(evidence)

        self.assertIsInstance(item, NormalizedIntelItem)
        self.assertEqual(item.source_id, 'requests:company_announcements_pdf')
        self.assertEqual(item.layer, 'export')
        self.assertEqual(item.evidence_type, 'periodic_report_section')
        self.assertEqual(item.category, 'region_revenue')
        self.assertEqual(item.url, 'https://example.com/report.pdf')
        self.assertEqual(item.source_url, 'https://example.com/report.pdf')
        self.assertEqual(item.published_at, '2026-04-20')
        self.assertIn('\u5883\u5916\u6536\u5165', item.matched_keywords)
        self.assertEqual(item.related_codes, ['300502'])
        self.assertEqual(item.related_names, ['\u65b0\u6613\u76db'])
        self.assertEqual(item.trust_level, 'primary')
        self.assertEqual(item.metric_name, '')
        self.assertIsNone(item.current_value)
        self.assertIn('annual', item.raw_ref)

    def test_field_summary_aggregates_business_segment_rows(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'business_segments',
                item_id='segment:800g',
                summary='800G optical module revenue increased year over year.',
                quality=0.72,
                values={'segment_name': '800G', 'revenue': '90,000'},
            ),
            _normalized_periodic_item(
                'business_segments',
                item_id='segment:400g',
                summary='400G optical module row has revenue, cost and gross margin.',
                quality=0.95,
                values={
                    'segment_name': '400G',
                    'revenue': '50,000',
                    'cost': '32,000',
                    'gross_margin': '36%',
                },
            ),
        ]

        summaries, status = build_periodic_field_summary_items(items)

        summary = next(item for item in summaries if item.metadata['field'] == 'business_segments')
        self.assertEqual(summary.evidence_type, 'periodic_field_summary')
        self.assertEqual(summary.category, 'periodic_field_summary')
        self.assertEqual(summary.metadata['evidence_count'], 2)
        self.assertEqual(summary.metadata['top_evidence_ids'][0], 'segment:400g')
        self.assertEqual(summary.metadata['strongest_source_title'], 'Stock A 2025 annual report')
        self.assertEqual(summary.metadata['source_period'], '2026-04-20')
        self.assertEqual(status['field_summary_by_field']['business_segments'], 1)

    def test_field_summary_keeps_customer_and_supplier_separate(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'customer_concentration',
                item_id='customer:top5',
                summary='Top five customers total sales accounted for 42%.',
                values={'item_name': 'top five customers total', 'share': '42%'},
            ),
            _normalized_periodic_item(
                'supplier_concentration',
                item_id='supplier:top5',
                summary='Top five suppliers procurement accounted for 37%.',
                values={'item_name': 'top five suppliers total', 'share': '37%'},
            ),
        ]

        summaries, status = build_periodic_field_summary_items(items)
        by_field = {item.metadata['field']: item for item in summaries}

        self.assertIn('customer_concentration', by_field)
        self.assertIn('supplier_concentration', by_field)
        self.assertEqual(by_field['customer_concentration'].metadata['top_evidence_ids'], ['customer:top5'])
        self.assertEqual(by_field['supplier_concentration'].metadata['top_evidence_ids'], ['supplier:top5'])
        self.assertNotIn('customer_supplier', status['field_summary_by_field'])

    def test_field_summary_direction_is_mixed_when_positive_and_negative_evidence_coexist(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item('export', item_id='export:positive', summary='Overseas revenue increased year over year.'),
            _normalized_periodic_item('export', item_id='export:negative', summary='Export gross margin declined year over year.'),
        ]

        summaries, status = build_periodic_field_summary_items(items)
        summary = next(item for item in summaries if item.metadata['field'] == 'export')

        self.assertEqual(summary.direction, 'mixed')
        self.assertEqual(summary.metadata['direction'], 'mixed')
        self.assertEqual(status['mixed_fields'], ['export'])
        self.assertIn('mixed positive and negative evidence', summary.metadata['conflict_reason'])

    def test_field_summary_without_change_terms_stays_neutral(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'inventory',
                item_id='inventory:raw_material',
                summary='Raw materials, finished goods and goods issued are listed in inventory table.',
                values={'item_name': 'raw materials', 'book_value': '11,700'},
            ),
        ]

        summaries, status = build_periodic_field_summary_items(items)
        summary = next(item for item in summaries if item.metadata['field'] == 'inventory')

        self.assertEqual(summary.direction, 'unknown')
        self.assertEqual(summary.metadata['direction'], 'unknown')
        self.assertNotIn('inventory', status['mixed_fields'])

    def test_field_summary_direction_detects_positive_negative_and_unknown(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'export',
                item_id='export:positive',
                values={'segment_name': '\u5883\u5916', 'revenue_yoy': '12.5%'},
            ),
            _normalized_periodic_item(
                'cost',
                item_id='cost:negative',
                values={'item_name': '\u539f\u6750\u6599', 'cost_yoy': '-6.2%'},
            ),
            _normalized_periodic_item(
                'inventory',
                item_id='inventory:unknown',
                values={'item_name': '\u539f\u6750\u6599', 'book_value': '11,700'},
            ),
        ]

        summaries, _status = build_periodic_field_summary_items(items)
        directions = {item.metadata['field']: item.metadata['direction'] for item in summaries}

        self.assertEqual(directions['export'], 'positive')
        self.assertEqual(directions['cost'], 'negative')
        self.assertEqual(directions['inventory'], 'unknown')

    def test_field_summary_high_count_low_quality_is_weak(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item('risk', item_id=f'risk:{index}', quality=0.3)
            for index in range(4)
        ]

        _summaries, status = build_periodic_field_summary_items(items)

        self.assertIn('risk', status['weak_fields'])

    def test_field_summary_top_refs_follow_quality_order(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'inventory',
                item_id='inventory:low',
                quality=0.2,
                raw_ref='periodic_report:annual:inventory:page1:inventory:low',
                values={'item_name': '\u539f\u6750\u6599', 'book_value': '1'},
            ),
            _normalized_periodic_item(
                'inventory',
                item_id='inventory:high',
                quality=0.9,
                raw_ref='periodic_report:annual:inventory:page1:inventory:high',
                values={'item_name': '\u539f\u6750\u6599', 'book_value': '9'},
            ),
        ]

        summaries, _status = build_periodic_field_summary_items(items)
        summary = next(item for item in summaries if item.metadata['field'] == 'inventory')

        self.assertEqual(summary.metadata['top_raw_refs'][0], 'periodic_report:annual:inventory:page1:inventory:high')

    def test_field_summary_ignores_non_export_overseas_structure_text(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'export',
                item_id='export:redchip',
                summary='\u5883\u5916\u7ea2\u7b79\u67b6\u6784\u62c6\u9664\u8fc7\u7a0b\u4e2d\u7684\u80a1\u6743\u8f6c\u8ba9\u4e8b\u9879',
                raw_ref='periodic_report:annual:main_business:text:export:redchip',
            ),
        ]

        summaries, status = build_periodic_field_summary_items(items)

        self.assertNotIn('export', {item.metadata['field'] for item in summaries})
        self.assertIn('export', status['missing_fields'])

    def test_field_summary_ignores_inventory_from_non_inventory_text_section(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'inventory',
                item_id='inventory:sales-volume',
                summary='\u5149\u901a\u4fe1\u6a21\u5757\u9500\u552e\u91cf\u548c\u671f\u521d\u5e93\u5b58\u91cf\u8868\u683c',
                raw_ref='periodic_report:annual:business_segments:text:inventory:sales-volume',
            ),
        ]

        summaries, status = build_periodic_field_summary_items(items)

        self.assertNotIn('inventory', {item.metadata['field'] for item in summaries})
        self.assertIn('inventory', status['missing_fields'])

    def test_field_summary_text_is_compact(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'financial',
                item_id=f'financial:{index}',
                summary='very long table fragment ' * 20,
                values={'item_name': 'very long table fragment ' * 8},
                quality=0.7,
            )
            for index in range(3)
        ]

        summaries, _status = build_periodic_field_summary_items(items)
        summary = next(item for item in summaries if item.metadata['field'] == 'financial')

        self.assertLessEqual(len(summary.metadata['summary_text']), 220)

    def test_field_summary_reports_missing_fields_without_pseudo_evidence(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        summaries, status = build_periodic_field_summary_items(
            [_normalized_periodic_item('financial', item_id='financial:1')],
            missing_reasons={
                'inventory': 'section_without_matching_table',
                'export': 'no_matching_table_or_section',
            },
        )

        self.assertEqual([item.metadata['field'] for item in summaries], ['financial'])
        self.assertEqual(status['missing_fields']['inventory'], 'section_without_matching_table')
        self.assertEqual(status['missing_fields']['export'], 'no_matching_table_or_section')

    def test_field_summary_filters_technical_analysis_items(self):
        from core.intelligence.field_summary import build_periodic_field_summary_items

        items = [
            _normalized_periodic_item(
                'risk',
                item_id='risk:technical',
                summary='MACD signal and buy point are technical-analysis text.',
            ),
        ]

        summaries, status = build_periodic_field_summary_items(items)

        self.assertEqual(summaries, [])
        self.assertEqual(status['field_summary_count'], 0)


if __name__ == '__main__':
    unittest.main()
