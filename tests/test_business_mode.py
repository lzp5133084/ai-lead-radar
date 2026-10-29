"""企业商机（B端）线索模式：公告语气入库，不破坏个人求助逻辑。"""
import unittest
from radar.core import analyze
from radar.collectors import bing_query_plan, tavily_query_plan, CollectorError

BIZ_TASK = dict(id=3, industry='保险', lead_mode='business', regions=['泸州', '泸县'],
                keywords=['团体保险', '员工福利保险', '保险招标'], exclude_keywords=[], min_score=60, search_budget=6)
CONSUMER_TASK = dict(id=1, industry='保险', regions=['泸州'], keywords=['保险', '投保'],
                     exclude_keywords=[], min_score=60, search_budget=6)
BING = dict(kind='bing_rss', platform='必应国内版', source_type='搜索摘要（需人工核实原文）')


def item(title, text):
    return dict(title=title, text=text, url='https://public.example/notice')


class BusinessModeTests(unittest.TestCase):
    def test_procurement_notice_is_accepted_without_contact(self):
        lead = analyze(item('泸州市某单位团体意外保险采购项目招标公告',
                            '泸县某单位员工福利保险采购项目，现公开比选，欢迎符合条件的供应商参与。'), BIZ_TASK, BING)
        self.assertTrue(lead)
        self.assertEqual(lead['contact'], '')
        self.assertIn('商机公告片段：', ''.join(lead['reasons']))
        self.assertIn('公告', lead['draft'])

    def test_company_name_in_title_not_rejected_in_business_mode(self):
        lead = analyze(item('泸州某某保险经纪有限公司员工福利保险项目比选公告',
                            '泸州企业团体保险项目比选公告，项目地点泸县。'), BIZ_TASK, BING)
        self.assertTrue(lead)

    def test_consumer_plea_not_accepted_in_business_mode(self):
        self.assertIsNone(analyze(item('求助', '我在泸州想买团体保险，求推荐方案。'), BIZ_TASK, BING))

    def test_notice_without_topic_keyword_rejected(self):
        self.assertIsNone(analyze(item('泸州食堂承包招标公告', '泸州某单位食堂承包项目公开招标公告。'), BIZ_TASK, BING))

    def test_sensitive_health_info_still_rejected(self):
        self.assertIsNone(analyze(item('泸州员工福利保险采购公告',
                            '泸州团体保险采购公告，含既往住院病史人员清单。'), BIZ_TASK, BING))

    def test_business_notice_not_accepted_in_consumer_mode(self):
        self.assertIsNone(analyze(item('泸州保险采购招标公告',
                            '泸州团体保险项目公开招标公告，欢迎参与。'), CONSUMER_TASK, BING))

    def test_nav_recruitment_link_not_treated_as_exclusion(self):
        # 招标网站全站导航常有"招聘"链接；公告是否招聘类只看标题。
        text = '采购项目名称：泸州市某单位团体意外险项目 | 公告类型：结果公告 | 首页 新闻 招聘 加盟'
        lead = analyze(item('泸州某单位团体意外险项目结果公告', text),
                       {**BIZ_TASK, 'keywords': BIZ_TASK['keywords'] + ['团体意外险'],
                        'exclude_keywords': ['招聘', '加盟']}, BING)
        self.assertTrue(lead)

    def test_recruitment_title_still_excluded(self):
        self.assertIsNone(analyze(item('泸州保险公司招聘启事', '泸州团体保险项目招聘公告'),
                                  {**BIZ_TASK, 'exclude_keywords': ['招聘']}, BING))

    def test_bing_business_plan_uses_notice_intents_and_covers_regions(self):
        plan = bing_query_plan(BIZ_TASK)
        self.assertEqual(len(plan), 6)
        self.assertEqual(len(set(plan)), 6)
        for region in BIZ_TASK['regions']:
            self.assertTrue(any(region in q for q in plan))
        self.assertTrue(any('公告' in q or '公示' in q or '征集' in q for q in plan))
        with self.assertRaises(CollectorError):
            bing_query_plan({**BIZ_TASK, 'search_budget': 100})

    def test_tavily_business_plan_mentions_notices(self):
        plan = tavily_query_plan(BIZ_TASK)
        self.assertEqual(len(plan), 6)
        self.assertEqual(len(set(plan)), 6)
        for q in plan:
            self.assertTrue('公告' in q or '公示' in q)
        for region in BIZ_TASK['regions']:
            self.assertTrue(any(region in q for q in plan))


if __name__ == '__main__':
    unittest.main()
