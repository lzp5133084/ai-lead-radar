"""Precision regressions and bounded breadth; fixtures never enter production."""
import unittest
from unittest.mock import patch
from radar.core import analyze
from radar.collectors import collect, tavily_query_plan, CollectorError

TASK = dict(id=1, industry='保险', regions=['泸州', '江阳区', '龙马潭区'],
            keywords=['保险', '投保'], exclude_keywords=[], min_score=60, search_budget=6)
SOURCE = dict(kind='tavily_search', platform='test', source_type='搜索摘要')

class BuyerIntentTests(unittest.TestCase):
    def item(self, title, text):
        return dict(title=title, text=text, url='https://public.example/post')

    def test_screenshot_advertisements_are_rejected(self):
        titles = ['泸州公积金管理中心简介_沃保保险网', '泸州装修报价清单-土巴兔装修网',
                  '四川泸州旧房翻新实力企业深度解析-商业品牌网',
                  '装修合同陷阱识别技巧避坑指南|泸州市城市人家装饰有限公司',
                  '四川泸州装修设计公司怎么选？本土老牌整装实力解析']
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(analyze(self.item(title, '泸州想买保险，求推荐方案。'), TASK, SOURCE))

    def test_footer_and_generic_information_do_not_become_requests(self):
        for text in ['泸州保险行业需要咨询服务。求助中心', '泸州保险配置怎么选，专家推荐方案。',
                     '泸州准备买保险。网页底部：求助中心', '求推荐装修师傅。泸州保险行业资讯。']:
            self.assertIsNone(analyze(self.item('资讯', text), TASK, SOURCE))

    def test_concrete_buyer_request_retains_evidence_and_no_contact(self):
        lead = analyze(self.item('求助', '我在泸州想买保险，求推荐投保方案。'), TASK, SOURCE)
        self.assertTrue(lead)
        self.assertIn('想买保险', lead['demand_evidence'])
        self.assertLessEqual(lead['score'], 80)
        self.assertEqual(lead['contact'], '')
        self.assertEqual(lead['review_status'], 'pending')

    def test_budget_covers_configured_regions_and_varies_intent(self):
        plan = tavily_query_plan(TASK)
        self.assertEqual(len(plan), 6)
        self.assertEqual(len(set(plan)), 6)
        for region in TASK['regions']:
            self.assertTrue(any(region in query for query in plan))
        with self.assertRaises(CollectorError):
            tavily_query_plan({**TASK, 'search_budget': 100})

    def test_multiple_queries_deduplicate_and_stop_after_failure(self):
        item = self.item('求助', '我在泸州想买保险，求推荐方案。')
        with patch('radar.collectors._collect_once', return_value=[item]) as fetch:
            result = collect(SOURCE, TASK)
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(len(result), 1)
        with patch('radar.collectors._collect_once', side_effect=[[item], CollectorError('额度不足')]) as fetch:
            result = collect(SOURCE, TASK)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result), 1)
