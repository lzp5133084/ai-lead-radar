import io
import json
import tempfile
import unittest
import zipfile
from radar.service import Radar
from test_integration import IntegrationTests

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.radar=Radar(self.temp.name)
        self.w=self.radar.workspace
    def tearDown(self):self.temp.cleanup()
    def test_default_is_unconfirmed_and_not_published(self):
        self.assertEqual(self.w.geo()['entries'],[])
        with self.assertRaises(ValueError):self.w.export()
        self.assertEqual(self.w.guide()['sites'],[])
    def test_profile_persistence_export_escaping_and_review_gate(self):
        p=self.w.geo()['profile']
        p.update(name='测试<script>alert(1)</script>',facts_confirmed=True)
        self.w.save_profile(p)
        with self.assertRaises(ValueError):self.w.export()
        p.update(qualification='隔离测试资质说明，不用于生产')
        self.w.save_profile(p)
        self.assertEqual(Radar(self.temp.name).workspace.geo()['profile']['name'],p['name'])
        with zipfile.ZipFile(io.BytesIO(self.w.export())) as package:
            self.assertIsNone(package.testzip())
            page=package.read('介绍页.html').decode()
            self.assertNotIn('<script>alert',page)
            self.assertIn('&lt;script&gt;',page)
            self.assertIn('不保证',page)
            schema=json.loads(package.read('实体资料.json'))
            self.assertEqual(schema['@type'],'Person')
            self.assertNotIn('aggregateRating',schema)
    def test_crud_and_private_publication_urls(self):
        record=self.w.save_entry(dict(kind='draft',title='隔离草稿',status='draft',content='真实测试内容'))
        self.w.save_entry(dict(kind='draft',title='修改标题',status='reviewed',content='编辑后'),record['id'])
        self.assertEqual(self.w.geo()['entries'][0]['title'],'修改标题')
        for url in ('http://localhost/a','https://public.example/?token=secret'):
            with self.assertRaises(ValueError):self.w.save_entry(dict(kind='publication',title='测试',status='published',url=url))
        self.w.delete_entry(record['id'])
        self.assertEqual(self.w.geo()['entries'],[])
    def test_history_does_not_invent_returned_domains(self):
        self.radar.db.save('runs',dict(status='success',started_at='2026-10-03T00:00:00Z',finished_at='2026-10-03T00:01:00Z',search_channels=[dict(name='tieba.baidu.com',returned=0)]))
        guide=self.w.guide()
        self.assertEqual(guide['targeted_sites'],['tieba.baidu.com'])
        self.assertEqual(guide['sites'],[])

class WorkspaceHTTPTests(IntegrationTests):
    def test_new_endpoints_require_login_and_csrf(self):
        self.assertEqual(self.request('GET','/api/geo')[0],401)
        self.login()
        self.assertEqual(self.request('GET','/api/guide')[0],200)
        self.assertEqual(self.request('GET','/api/geo')[0],200)
        self.assertEqual(self.request('POST','/api/geo/profile',{},csrf=False)[0],403)
        self.assertEqual(self.request('GET','/api/geo/export.zip')[0],400)
