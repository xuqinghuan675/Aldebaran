import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntelligenceRouteProfileTest(unittest.TestCase):
    def test_builtin_route_profiles_cover_current_tracking_templates(self):
        from core.intelligence.route_profile import build_route_profile

        cpo_codes = ('300308', '300502')
        for code in cpo_codes:
            profile = build_route_profile(code, code)
            self.assertEqual(profile.template_id, 'cpo_optical_module')
            self.assertIn('CPO', profile.keywords)
            self.assertIn('optical module', profile.keywords)
            self.assertIn('company_disclosure', profile.field_routes)
            self.assertIn('export', profile.field_routes)

        semi = build_route_profile('600584', '\u957f\u7535\u79d1\u6280')
        self.assertEqual(semi.template_id, 'semiconductor_packaging')
        self.assertIn('advanced packaging', semi.keywords)
        self.assertIn('semiconductor', semi.sectors)
        self.assertIn('cost', semi.field_routes)

        copyright_profile = build_route_profile('000681', '\u89c6\u89c9\u4e2d\u56fd')
        self.assertEqual(copyright_profile.template_id, 'copyright_aigc')
        self.assertIn('AIGC', copyright_profile.keywords)
        self.assertIn('risk', copyright_profile.field_routes)

    def test_unknown_stock_gets_internal_profile_without_hardcoded_template(self):
        from core.intelligence.route_profile import build_route_profile

        profile = build_route_profile(
            '123456',
            'Example Robotics',
            context={'sectors': ['robotics', 'automation']},
        )

        self.assertEqual(profile.template_id, 'generic')
        self.assertIn('Example Robotics', profile.keywords)
        self.assertIn('123456', profile.keywords)
        self.assertEqual(profile.sectors, ('robotics', 'automation'))
        self.assertIn('news_sentiment', profile.field_routes)


if __name__ == '__main__':
    unittest.main()
