"""Search contracts and permission gates. Fixtures stay in temporary databases."""
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from radar.collectors import CollectorError, FetchResult, collect
from radar.core import analyze, csv_export
from radar.feishu import FeishuClient, FeishuError
from radar.service import Radar


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
SOURCE = {'name': '隔离搜索测试', 'kind': 'tavily_search', 'url': 'https://api.tavily.com/search',
          'platform': '测试', 'source_type': '搜索摘要（需人工核实原文）',
          'api_authorized': True, 'permission_confirmed': True, '_api_token': 'TEST_ONLY_TOKEN'}
TASK = {'id': 1, 'name': '隔离任务', 'industry': '保险', 'regions': ['泸州', '泸州市'],
        'keywords': ['保险', '投保', '保障方案'], 'exclude_keywords': [], 'interval_minutes': 1440,
        'min_score': 60, 'source_ids': [1]}
ITEM = {'title': '隔离夹具', 'text': '泸州想买保险，求推荐配置方案。',
        'url': 'https://public.example/isolated-post', 'platform': 'public.example',
        'source_type': '搜索摘要（需人工核实原文）'}


class SearchPermissionTests(unittest.TestCase):
    def setUp(self):
        self.dns = patch('radar.collectors.socket.getaddrinfo', return_value=PUBLIC_DNS)
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def response(self, payload):
        return FetchResult(json.dumps(payload, ensure_ascii=False).encode(),
                           {'content-type': 'application/json'}, SOURCE['url'], 200)

    def test_tavily_contract_uses_basic_source_snippets_and_one_api_request(self):
        payload = {'answer': '任何生成式答案不得入库', 'results': [
            {'title': ITEM['title'], 'content': ITEM['text'], 'url': ITEM['url'], 'published_date': 'estimated-date',
             'raw_content': '完整网页不得伪装为摘要'},
            {'title': '重复', 'content': ITEM['text'], 'url': ITEM['url']},
            {'title': '私网', 'content': ITEM['text'], 'url': 'http://localhost/private'},
            {'title': '受限平台', 'content': ITEM['text'], 'url': 'https://xiaohongshu.com/example'},
            {'title': '敏感', 'content': '个人诊断和病史信息', 'url': 'https://public.example/health'},
        ]}
        with patch('radar.collectors.official_tavily_fetch', return_value=self.response(payload)) as fetch:
            items = collect({**SOURCE, 'contact_consent': True}, TASK)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], 'https://api.tavily.com/search')
        self.assertEqual(fetch.call_args.kwargs['method'], 'POST')
        self.assertEqual(fetch.call_args.kwargs['headers']['Authorization'], 'Bearer TEST_ONLY_TOKEN')
        body = json.loads(fetch.call_args.kwargs['body'])
        self.assertEqual(body['search_depth'], 'basic')
        self.assertEqual(body['max_results'], 20)
        for flag in ('include_answer', 'include_raw_content', 'include_images', 'auto_parameters'):
            self.assertIs(body[flag], False)
        for term in TASK['regions'] + TASK['keywords']:
            self.assertIn(term, body['query'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['text'], ITEM['text'])
        self.assertEqual(items[0]['published_at'], '')
        self.assertEqual(items[0]['platform'], 'public.example')
        self.assertNotIn('任何生成式答案', json.dumps(items, ensure_ascii=False))
        self.assertNotIn('TEST_ONLY_TOKEN', json.dumps(items))

    def test_tavily_bad_keys_sensitive_queries_and_custom_endpoints_make_no_request(self):
        bad_sources = [{**SOURCE, '_api_token': ''}, {**SOURCE, 'api_authorized': False},
                       {**SOURCE, 'permission_confirmed': False}, {**SOURCE, '_api_token': 'token\ninjection'},
                       {**SOURCE, 'url': 'https://unapproved.example/steal'}]
        with patch('radar.collectors.official_tavily_fetch') as fetch:
            for source in bad_sources:
                with self.subTest(source=source), self.assertRaises(CollectorError):
                    collect(source, TASK)
            for term in ('病史', '13800138000', '微信号：sample_contact', '保险' * 400):
                with self.subTest(term=term), self.assertRaises(CollectorError):
                    collect(SOURCE, {**TASK, 'keywords': [term]})
            fetch.assert_not_called()

    def test_malformed_search_results_do_not_fabricate_items_or_echo_secrets(self):
        for payload in ({}, {'results': {}}, {'results': [], 'error': 'TEST_ONLY_TOKEN'}):
            with self.subTest(payload=payload), patch('radar.collectors.official_tavily_fetch', return_value=self.response(payload)):
                with self.assertRaises(CollectorError) as exc:
                    collect(SOURCE, TASK)
                self.assertNotIn('TEST_ONLY_TOKEN', str(exc.exception))
        with patch('radar.collectors.official_tavily_fetch', return_value=self.response({'results': []})):
            self.assertEqual(collect(SOURCE, TASK), [])

    def test_ordinary_brave_key_cannot_fetch_or_create_source(self):
        brave = {**SOURCE, 'kind': 'brave_search', 'url': 'https://api.search.brave.com/res/v1/web/search'}
        with patch('radar.collectors.safe_fetch') as fetch:
            for fields in ({}, {'storage_authorized': True},
                           {'storage_authorized': 'yes', 'storage_authorization_note': '普通注册不等于书面许可'}):
                with self.subTest(fields=fields), self.assertRaises(CollectorError):
                    collect({**brave, **fields}, TASK)
            fetch.assert_not_called()
        with tempfile.TemporaryDirectory() as temp:
            radar = Radar(temp)
            with self.assertRaises(ValueError):
                radar.save_source(brave)
            self.assertEqual(radar.db.all('sources'), [])

    def test_brave_custom_storage_permission_still_cannot_send_sensitive_queries(self):
        brave = {**SOURCE, 'kind': 'brave_search', 'url': 'https://api.search.brave.com/res/v1/web/search',
                 'storage_authorized': True, 'storage_authorization_note': '隔离测试：模拟书面存储授权'}
        with patch('radar.collectors.safe_fetch') as fetch:
            for term in ('病史', '13800138000', 'a@example.com'):
                with self.subTest(term=term), self.assertRaises(CollectorError):
                    collect(brave, {**TASK, 'keywords': [term]})
            fetch.assert_not_called()

    def test_old_brave_source_and_task_fail_before_collector_or_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            radar = Radar(temp, collector=lambda s, t: self.fail('Unauthorized collector was called'))
            source = radar.db.save('sources', {**SOURCE, 'kind': 'brave_search', 'url': 'https://api.search.brave.com/res/v1/web/search'})
            task = radar.db.save('tasks', {**TASK, 'id': None, 'source_ids': [source['id']]})
            with self.assertRaises(ValueError):
                radar.save_task(task, task['id'])
            radar.run_task(task['id'], background=False)
            self.assertEqual(radar.db.all('runs')[0]['status'], 'failed')
            self.assertEqual(radar.db.all('leads'), [])
            self.assertIn('书面存储许可', radar.db.get('sources', source['id'])['last_error'])

    def test_tavily_requires_invitation_no_contacts_and_manual_approval_permission(self):
        source = {**SOURCE, 'contact_consent': True}
        lead = analyze({**ITEM, 'text': ITEM['text'] + ' 商务联系 13800138000'}, TASK, source)
        self.assertIsNotNone(lead)
        self.assertEqual(lead['contact'], '')
        self.assertNotIn('13800138000', lead['text'])
        self.assertIn('摘要', lead['draft'])
        self.assertFalse(lead['followup_permission_confirmed'])
        self.assertIsNone(analyze({**ITEM, 'text': '泸州准备买保险。'}, TASK, source))
        self.assertIsNone(analyze({**ITEM, 'text': ITEM['text'] + '个人病史'}, TASK, source))
        with tempfile.TemporaryDirectory() as temp:
            radar = Radar(temp)
            radar.db.insert_lead(lead)
            with self.assertRaises(ValueError):
                radar.update_lead(lead['id'], {'review_status': 'approved'})
            self.assertEqual(radar.db.get('leads', lead['id'])['review_status'], 'pending')
            approved = radar.update_lead(lead['id'], {'review_status': 'approved', 'followup_permission_confirmed': True})
            self.assertIn(approved['draft'], csv_export([approved]).decode('utf-8-sig'))
            with self.assertRaises(ValueError):
                radar.update_lead(lead['id'], {'followup_permission_confirmed': False})

    def test_legacy_approved_summary_is_blocked_by_csv_and_feishu_until_reconfirmed(self):
        lead = {**analyze(ITEM, TASK, SOURCE), 'review_status': 'approved'}
        lead.pop('followup_permission_confirmed', None)
        lead.pop('source_kind', None)  # Legacy record recognition via summary label.
        exported = csv_export([lead]).decode('utf-8-sig')
        self.assertNotIn(lead['draft'], exported)
        client = FeishuClient({'app_token': 'test', 'table_id': 'test', 'tenant_access_token': 'TEST_ONLY_TOKEN'})
        with patch.object(client, '_request') as request:
            with self.assertRaises(FeishuError):
                client.upsert(lead)
            request.assert_not_called()

    def test_source_provider_change_does_not_reuse_protected_key(self):
        with tempfile.TemporaryDirectory() as temp:
            radar = Radar(temp)
            source = radar.save_source({**SOURCE, 'api_token_env': 'ISOLATED_TAVILY_TOKEN'})
            with patch.object(radar.config, 'source_token', return_value='TEST_ONLY_OLD_TOKEN'), \
                    patch.object(radar.config, 'delete_source_token') as clear:
                with self.assertRaises(ValueError):
                    radar.save_source({'kind': 'json_api', 'url': 'https://other.example/feed'}, source['id'])
                self.assertEqual(radar.db.get('sources', source['id'])['kind'], 'tavily_search')
                radar.save_source({'kind': 'json_api', 'url': 'https://other.example/feed',
                                   'api_token_env': 'ISOLATED_NEW_TOKEN'}, source['id'])
                clear.assert_called_once_with(source['id'])


if __name__ == '__main__':
    unittest.main()
