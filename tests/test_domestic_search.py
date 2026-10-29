import unittest
from unittest.mock import patch
from radar.collectors import collect, search_domains, CollectorError
from test_search_permissions import SOURCE, TASK

class DomesticSearchTests(unittest.TestCase):
    def test_domains_reject_secrets_private_hosts_and_restricted_platforms(self):
        for domain in ['localhost', '127.0.0.1', 'xiaohongshu.com', 'site.com/token?key=secret', 'site.com:443']:
            with self.subTest(domain=domain), self.assertRaises(CollectorError):
                search_domains({'search_domains':[domain]})
        self.assertEqual(search_domains({'search_domains':['tieba.baidu.com']}), ['tieba.baidu.com'])

    def test_six_queries_alternate_general_and_domestic_domains(self):
        with patch('radar.collectors._collect_once', return_value=[]) as fetch:
            result = collect(SOURCE, {**TASK, 'domestic_search':True, 'search_budget':6})
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual([c['name'] for c in result.channels], ['国内网页优先','tieba.baidu.com','国内网页优先','zhidao.baidu.com','国内网页优先','xiaomayi.net'])
        self.assertEqual(result, [])
        self.assertEqual(fetch.call_args_list[1].args[1]['_search_domains'], ['tieba.baidu.com'])
