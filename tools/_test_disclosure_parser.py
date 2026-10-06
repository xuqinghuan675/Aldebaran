import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _by_field(items):
    return {item.field: item for item in items}


class DisclosureParserTest(unittest.TestCase):
    def test_contract_body_produces_order_contract_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u516c\u53f8\u8fd1\u65e5\u4e0e\u91cd\u8981\u5ba2\u6237'
            '\u7b7e\u7f72\u9500\u552e\u5408\u540c\uff0c'
            '\u5408\u540c\u6807\u7684\u4e3a\u9ad8\u901f\u5149\u6a21\u5757\u4ea7\u54c1\u3002'
        )

        fields = _by_field(parse_announcement_text(text, title='\u5408\u540c\u516c\u544a'))

        self.assertIn('order_contract', fields)
        evidence = fields['order_contract']
        self.assertEqual(evidence.layer, 'order_contract')
        self.assertIn('\u5408\u540c', evidence.matched_keywords)
        self.assertIn('\u9500\u552e\u5408\u540c', evidence.evidence_text)

    def test_capacity_body_produces_capacity_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u516c\u53f8\u52df\u6295\u9879\u76ee\u5efa\u8bbe\u987a\u5229\uff0c'
            '\u65b0\u589e\u4ea7\u7ebf\u5df2\u8fdb\u5165\u8bd5\u751f\u4ea7\u9636\u6bb5\uff0c'
            '\u9884\u8ba1\u6295\u4ea7\u540e\u5c06\u63d0\u5347\u4ea7\u80fd\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertIn('capacity', fields)
        self.assertEqual(fields['capacity'].layer, 'capacity')
        self.assertTrue({'\u52df\u6295\u9879\u76ee', '\u4ea7\u7ebf', '\u4ea7\u80fd'} & set(fields['capacity'].matched_keywords))

    def test_risk_body_produces_risk_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u516c\u53f8\u6536\u5230\u76d1\u7ba1\u51fd\uff0c'
            '\u5e76\u5c31\u91cd\u5927\u8bc9\u8bbc\u4e8b\u9879\u5c65\u884c\u4fe1\u606f\u62ab\u9732\u4e49\u52a1\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertIn('risk', fields)
        self.assertEqual(fields['risk'].layer, 'risk')
        self.assertIn('\u76d1\u7ba1\u51fd', fields['risk'].matched_keywords)

    def test_financial_body_produces_financial_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u516c\u53f8\u53d1\u5e03\u4e1a\u7ee9\u9884\u544a\uff0c'
            '\u9884\u8ba1\u8425\u6536\u548c\u51c0\u5229\u6da6\u540c\u6bd4\u589e\u957f\uff0c'
            '\u6bdb\u5229\u7387\u8f83\u4e0a\u5e74\u6539\u5584\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertIn('financial', fields)
        self.assertEqual(fields['financial'].layer, 'financial')
        self.assertTrue({'\u4e1a\u7ee9\u9884\u544a', '\u51c0\u5229\u6da6', '\u6bdb\u5229\u7387'} & set(fields['financial'].matched_keywords))

    def test_overseas_customer_order_produces_export_and_customer_supplier(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u516c\u53f8\u4e0e\u6d77\u5916\u5927\u5ba2\u6237\u7b7e\u7f72\u51fa\u53e3\u8ba2\u5355\uff0c'
            '\u56fd\u9645\u5ba2\u6237\u9700\u6c42\u589e\u52a0\uff0c'
            '\u5c06\u652f\u6301\u516c\u53f8\u5916\u8d38\u4e1a\u52a1\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertIn('export', fields)
        self.assertIn('customer_supplier', fields)
        self.assertEqual(fields['export'].layer, 'export')
        self.assertEqual(fields['customer_supplier'].layer, 'customer_supplier')

    def test_equipment_procurement_risk_is_not_customer_supplier_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u672c\u9879\u76ee\u5b9e\u65bd\u5c1a\u9700\u4e00\u5b9a\u5468\u671f\uff0c'
            '\u53ef\u80fd\u9762\u4e34\u884c\u4e1a\u653f\u7b56\u53ca\u5e02\u573a\u73af\u5883\u53d8\u5316\u3001'
            '\u8bbe\u5907\u91c7\u8d2d\u53ca\u5b89\u88c5\u8c03\u8bd5\u5ef6\u671f\u7b49\u56e0\u7d20\uff0c'
            '\u5bfc\u81f4\u9879\u76ee\u5b9e\u65bd\u8fdb\u5ea6\u4e0d\u8fbe\u9884\u671f\u7684\u98ce\u9669\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertNotIn('customer_supplier', fields)

    def test_inventory_body_produces_inventory_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u53d7\u4e0b\u6e38\u8865\u5e93\u5f71\u54cd\uff0c'
            '\u516c\u53f8\u5e93\u5b58\u548c\u5b58\u8d27\u7ed3\u6784\u6709\u6240\u6539\u5584\uff0c'
            '\u5907\u8d27\u8282\u594f\u8d8b\u4e8e\u7a33\u5b9a\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertIn('inventory', fields)
        self.assertEqual(fields['inventory'].layer, 'inventory')
        self.assertTrue({'\u5e93\u5b58', '\u5b58\u8d27', '\u8865\u5e93'} & set(fields['inventory'].matched_keywords))

    def test_multi_field_body_keeps_distinct_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        text = (
            '\u516c\u53f8\u4e0e\u5ba2\u6237\u7b7e\u7f72\u91c7\u8d2d\u534f\u8bae\u3002'
            '\u540c\u65f6\uff0c\u52df\u6295\u9879\u76ee\u5efa\u8bbe\u63a8\u8fdb\uff0c'
            '\u65b0\u4ea7\u80fd\u8fdb\u5165\u6295\u4ea7\u9636\u6bb5\u3002'
            '\u516c\u53f8\u4e5f\u62ab\u9732\u76d1\u7ba1\u51fd\u76f8\u5173\u98ce\u9669\u3002'
        )

        fields = _by_field(parse_announcement_text(text))

        self.assertTrue({'order_contract', 'capacity', 'risk'} <= set(fields))
        self.assertEqual(len(fields), len({item.field for item in parse_announcement_text(text)}))

    def test_technical_terms_do_not_create_business_evidence(self):
        from core.intelligence.disclosure_parser import parse_announcement_text

        technical = ''.join(['M', 'A', 'C', 'D'])
        text = (
            technical
            + '\u91d1\u53c9\u540e\u51fa\u73b0'
            + ''.join(['\u4e70', '\u70b9'])
            + '\uff0c'
            + ''.join(['\u6b62', '\u635f'])
            + '\u4f4d\u9700\u8981\u5173\u6ce8\u3002'
        )

        self.assertEqual(parse_announcement_text(text), [])

    def test_long_body_extracts_relevant_sentences_only(self):
        from core.intelligence.disclosure_parser import extract_relevant_sentences, parse_announcement_text

        filler = '\u65e0\u5173\u80cc\u666f\u63cf\u8ff0' * 80
        hit = '\u516c\u53f8\u4e0e\u5ba2\u6237\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u3002'
        text = filler + '\u3002' + hit + filler

        sentences = extract_relevant_sentences(text, ['\u5408\u540c'], max_sentences=1)
        evidence = _by_field(parse_announcement_text(text))['order_contract']

        self.assertEqual(sentences, [hit])
        self.assertLess(len(evidence.evidence_text), len(text) // 3)
        self.assertIn('\u91cd\u5927\u5408\u540c', evidence.evidence_text)

    def test_disclosure_evidence_to_normalized_item_preserves_fields(self):
        from core.intelligence.disclosure_parser import (
            disclosure_evidence_to_normalized_item,
            parse_announcement_text,
        )
        from core.intelligence.models import NormalizedIntelItem

        text = '\u516c\u53f8\u4e0e\u6d77\u5916\u5ba2\u6237\u7b7e\u7f72\u51fa\u53e3\u8ba2\u5355\u3002'
        evidence = _by_field(parse_announcement_text(
            text,
            title='\u51fa\u53e3\u8ba2\u5355\u516c\u544a',
            source_url='https://example.com/demo.pdf',
            published_at='2026-07-04',
            code='300502',
            name='\u65b0\u6613\u76db',
        ))['export']

        item = disclosure_evidence_to_normalized_item(evidence)

        self.assertIsInstance(item, NormalizedIntelItem)
        self.assertEqual(item.source_id, 'requests:company_announcements_pdf')
        self.assertEqual(item.layer, 'export')
        self.assertEqual(item.evidence_type, 'announcement_pdf')
        self.assertEqual(item.url, 'https://example.com/demo.pdf')
        self.assertEqual(item.source_url, 'https://example.com/demo.pdf')
        self.assertEqual(item.published_at, '2026-07-04')
        self.assertIn('\u6d77\u5916', item.matched_keywords)
        self.assertIn('export', item.raw_ref)
        self.assertEqual(item.related_codes, ['300502'])
        self.assertEqual(item.related_names, ['\u65b0\u6613\u76db'])
        self.assertIsNone(item.current_value)
        self.assertEqual(item.metric_name, '')


if __name__ == '__main__':
    unittest.main()
