"""Isolated fixtures exercise HTTP and storage; never used in production databases."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Server
from radar.service import Radar


def isolated_item():
    return {'title': '杭州装修需求', 'text': '杭州西湖准备装修，想找装修公司，本月需要方案。',
            'url': 'https://example.com/isolated-test-post', 'platform': '隔离测试来源',
            'source_type': '测试夹具', 'published_at': ''}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='radar-isolated-test-')
        self.radar = Radar(self.temp.name, collector=lambda s, t: [isolated_item()])
        self.server = Server(('127.0.0.1', 0), self.radar)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''
        self.csrf = ''

    def tearDown(self):
        self.radar.stop_event.set()
        scheduler = getattr(self.radar, 'scheduler_thread', None)
        if scheduler is not None:
            scheduler.join(8)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, method, path, body=None, csrf=True, extra=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Cookie': self.cookie}
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        headers.update(extra or {})
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        status, returned = response.status, dict(response.getheaders())
        conn.close()
        return status, json.loads(raw) if 'application/json' in returned.get('Content-Type', '') else raw, returned

    def login(self):
        status, data, headers = self.request('POST', '/api/login', {'password': self.radar.config.initial_password})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = data['data']['csrf']
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])

    def source_task(self):
        source = self.radar.save_source({'name': '隔离测试来源', 'kind': 'rss',
                       'url': 'https://example.com/feed.xml', 'platform': '隔离测试',
                       'source_type': '测试夹具', 'permission_confirmed': True})
        task = self.radar.save_task({'name': '隔离测试任务', 'industry': '装修', 'regions': ['杭州'],
                    'keywords': ['装修'], 'source_ids': [source['id']], 'interval_minutes': 60})
        return source, task

    def test_auth_csrf_origin_and_private_paths(self):
        self.assertEqual(self.request('GET', '/api/leads')[0], 401)
        self.assertEqual(self.request('GET', '/api/profile')[0], 401)
        self.assertEqual(self.request('GET', '/data/config.json')[0], 401)
        self.login()
        self.assertEqual(self.request('GET', '/api/session')[1]['data']['role'], 'admin')
        profile = self.request('GET', '/api/profile')[1]['data']
        self.assertEqual(profile['industry'], '保险')
        self.assertFalse(profile['enabled'])
        self.assertEqual(self.radar.db.all('tasks'), [])
        self.assertEqual(self.radar.db.all('sources'), [])
        self.assertEqual(self.radar.db.all('leads'), [])
        self.assertEqual(self.request('POST', '/api/tasks', {}, csrf=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/tasks', {}, extra={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/dashboard', extra={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/data/config.json')[0], 404)

    def test_full_local_http_pipeline_and_duplicate(self):
        self.login()
        self.assertEqual(self.request('GET', '/api/leads')[1]['data'], [])
        source, task = self.source_task()
        result = self.request('POST', f'/api/tasks/{task["id"]}/run', {})
        self.assertEqual(result[0], 200)
        deadline = time.time() + 5
        while self.radar.active and time.time() < deadline:
            time.sleep(0.03)
        leads = self.request('GET', '/api/leads')[1]['data']
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['review_status'], 'pending')
        uid = leads[0]['id']
        updated = self.request('PATCH', '/api/leads/' + uid,
                               {'review_status': 'approved', 'draft': '人工审核话术', 'status': 'contacted'})
        self.assertEqual(updated[0], 200)
        self.radar.run_task(task['id'], background=False)
        self.assertEqual(len(self.radar.db.all('leads')), 1)
        self.assertEqual(self.radar.db.get('leads', uid)['draft'], '人工审核话术')
        self.assertIn('人工审核话术', self.request('GET', '/api/export.csv?review_status=approved')[1].decode('utf-8-sig'))
        self.assertEqual(self.request('DELETE', '/api/sources/' + str(source['id']))[0], 400)
        self.assertEqual(self.request('DELETE', '/api/leads/' + uid)[0], 200)
        self.assertEqual(self.radar.db.all('leads'), [])

    def test_failed_and_no_match_runs_never_create_leads(self):
        _, task = self.source_task()
        self.radar.collector = lambda s, t: []
        self.radar.run_task(task['id'], background=False)
        self.assertEqual(self.radar.db.all('runs')[0]['status'], 'success')
        self.assertEqual(self.radar.db.all('leads'), [])
        def fail(s, t):
            raise RuntimeError('HTTP 403 拒绝访问')
        self.radar.collector = fail
        self.radar.run_task(task['id'], background=False)
        self.assertEqual(self.radar.db.all('runs')[0]['status'], 'failed')
        self.assertEqual(self.radar.db.all('leads'), [])
        self.assertEqual(self.radar.db.all('sources')[0]['status'], '采集失败')

    def test_review_gate_and_repeat_sync(self):
        class TestRemote:
            writes = []
            def __init__(self, conf):
                pass
            def check(self):
                return {'ok': True}
            def upsert(self, lead):
                self.writes.append(lead['id'])
                return 'isolated-remote-record'
        _, task = self.source_task()
        self.radar.run_task(task['id'], background=False)
        self.radar.feishu_factory = TestRemote
        with patch.dict(os.environ, {'LEAD_RADAR_FEISHU_APP_TOKEN': 'isolated-app',
                                    'LEAD_RADAR_FEISHU_TABLE_ID': 'isolated-table',
                                    'LEAD_RADAR_FEISHU_TENANT_ACCESS_TOKEN': 'isolated-token'}):
            self.assertEqual(self.radar.sync()['synced'], 0)
            lead = self.radar.db.all('leads')[0]
            self.radar.update_lead(lead['id'], {'review_status': 'approved'})
            self.assertEqual(self.radar.sync()['synced'], 1)
            self.assertEqual(self.radar.sync()['synced'], 0)
            self.assertEqual(TestRemote.writes, [lead['id']])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_credentials_encrypted_and_absent_from_http(self):
        self.login()
        self.radar.config.update({'feishu': {'app_id': 'isolated-id', 'app_secret': 'never-print-this-secret',
                       'app_token': 'isolated-app', 'table_id': 'isolated-table'}})
        stored = Path(self.temp.name, 'config.json').read_text('utf-8')
        self.assertNotIn('never-print-this-secret', stored)
        self.assertEqual(self.radar.config.feishu()['app_secret'], 'never-print-this-secret')
        data = self.request('GET', '/api/settings')[1]
        self.assertNotIn('never-print-this-secret', json.dumps(data))
        source = self.radar.save_source({'name': '搜索', 'kind': 'tavily_search', 'platform': 'Tavily',
                       'permission_confirmed': True, 'api_authorized': True, 'api_token': 'isolated-search-key'})
        self.assertTrue(source['has_token'])
        self.assertNotIn('isolated-search-key', json.dumps(self.radar.sources()))
        self.assertNotIn('isolated-search-key', Path(self.temp.name, 'config.json').read_text('utf-8'))
        self.assertEqual(self.radar.config.source_token(source['id']), 'isolated-search-key')

    def test_due_task_scheduler_runs_and_stops(self):
        _, task = self.source_task()
        task['next_run_at'] = '2000-01-01T00:00:00+00:00'
        self.radar.db.save('tasks', task)
        self.radar.start()
        deadline = time.time() + 7
        while not self.radar.db.all('leads') and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(len(self.radar.db.all('leads')), 1)
        self.assertGreater(self.radar.db.get('tasks', task['id'])['next_run_at'], '2000')


if __name__ == '__main__':
    unittest.main()
