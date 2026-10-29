"""Privacy and configuration regressions; isolated fixtures only."""
import tempfile
import unittest
from radar.core import analyze, redact
from radar.service import Radar


class RegressionTests(unittest.TestCase):
    def test_separated_phone_and_wechat_label_are_masked(self):
        for value in ('138 0012 3456', '138-0012-3456', '+86 138-0012-3456', '021-12345678', '微信号：sample_contact'):
            masked = redact('私人联系方式 ' + value)
            self.assertNotIn(value, masked)
            self.assertIn('[联系方式已隐藏]', masked)

    def test_no_contact_request_is_not_accepted_as_marketing_lead(self):
        item = {'title': '杭州装修需求', 'text': '杭州准备装修，求推荐。不要商务联系 13800138000', 'url': 'https://example.com/post'}
        task = {'id': 1, 'industry': '装修', 'regions': ['杭州'], 'keywords': ['装修'], 'exclude_keywords': []}
        self.assertIsNone(analyze(item, task, {'platform': '测试', 'source_type': '测试', 'contact_consent': True}))

    def test_search_draft_truthfully_mentions_summary(self):
        item = {'title': '杭州装修需求', 'text': '杭州准备装修，求推荐。', 'url': 'https://example.com/post'}
        task = {'id': 1, 'industry': '装修', 'regions': ['杭州'], 'keywords': ['装修'], 'exclude_keywords': []}
        lead = analyze(item, task, {'kind': 'brave_search', 'platform': 'Brave', 'source_type': '搜索摘要'})
        self.assertIn('摘要', lead['draft'])
        self.assertNotIn('的公开内容中看到你', lead['draft'])

    def test_source_secret_url_rejected_before_persisting(self):
        with tempfile.TemporaryDirectory() as temp:
            radar = Radar(temp)
            for url in ('https://example.com/?apikey=private', 'https://example.com/?%61pp_secret=private',
                        'https://example.com/?key=private', 'http://127.0.0.1/feed', 'http://example.com:8080/feed'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    radar.save_source({'kind': 'rss', 'name': '隔离测试', 'url': url, 'permission_confirmed': True})
            self.assertEqual(radar.db.all('sources'), [])


if __name__ == '__main__':
    unittest.main()
