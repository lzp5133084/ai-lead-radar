"""Isolated checks of the decisions that can affect real people and data.

All examples are synthetic *test fixtures* in memory/TemporaryDirectory.
They are never loaded into production databases or shipped as collected leads.
"""
import csv
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar.core import (Database, analyze, canonical_url, csv_export,
                        filtered_leads, permitted_contacts, redact)


def task(**changes):
    value = dict(id=1, industry='装修', regions=['杭州', '余杭'],
                 keywords=['装修', '施工'], exclude_keywords=['招聘', '加盟'],
                 min_score=60, business_intro='提供装修报价与施工咨询')
    value.update(changes)
    return value


def source(**changes):
    value = dict(platform='已授权测试来源', source_type='rss', contact_consent=False)
    value.update(changes)
    return value


def item(**changes):
    value = dict(title='杭州装修咨询', text='杭州余杭的房子准备装修，求推荐靠谱施工公司。',
                 url='https://example.com/request/1', published_at='2026-10-01T10:00:00+08:00')
    value.update(changes)
    return value


class IntentAndPrivacyTests(unittest.TestCase):
    def test_real_source_content_trace_is_preserved(self):
        record = analyze(item(), task(), source())
        self.assertIsNotNone(record)
        self.assertEqual(record['url'], item()['url'])
        self.assertEqual(record['published_at'], item()['published_at'])
        self.assertEqual(record['platform'], source()['platform'])
        self.assertEqual(record['text'], item()['text'])
        datetime.fromisoformat(record['collected_at'])
        self.assertEqual(record['review_status'], 'pending')
        self.assertGreaterEqual(record['score'], 60)

    def test_region_keyword_and_demand_are_all_required(self):
        for candidate in (
            item(text='上海的房子准备装修，求推荐施工公司。', title='装修咨询'),
            item(text='杭州装修行业年度报告和施工指数。', title='行业资讯'),
            item(text='杭州准备购买一台电脑，求推荐品牌。', title='电脑咨询'),
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNone(analyze(candidate, task(), source()))

    def test_ads_exclusions_and_sensitive_insurance_are_skipped(self):
        self.assertIsNone(analyze(item(text='杭州准备装修，招聘施工人员。'), task(), source()))
        self.assertIsNone(analyze(item(text='杭州装修，想了解我们提供的专业施工。'), task(), source()))
        self.assertIsNone(analyze(item(title='杭州保险咨询', text='杭州想买保险，有糖尿病。'),
                                  task(industry='保险', keywords=['保险']), source()))
        self.assertIsNotNone(analyze(item(title='杭州保险咨询', text='杭州想买保险，求推荐投保方案。'),
                                     task(industry='保险', keywords=['保险']), source()))

    def test_contacts_are_not_a_scoring_feature(self):
        plain = analyze(item(), task(), source())
        with_contact = analyze(item(text=item()['text'] + '\n商务联系 13800138000'),
                               task(), source(contact_consent=True))
        self.assertEqual(plain['score'], with_contact['score'])
        self.assertEqual(with_contact['contact'], '13800138000')
        self.assertEqual(with_contact['contact_source'], item()['url'])
        self.assertNotIn('13800138000', with_contact['text'])
        self.assertNotIn('13800138000', with_contact['draft'])

    def test_opt_in_and_same_short_sentence_are_both_required(self):
        content = '报价请联系 13800138000，business@example.com，微信：sample_contact'
        self.assertEqual(permitted_contacts(content, source()), '')
        allowed = permitted_contacts(content, source(contact_consent=True))
        self.assertIn('13800138000', allowed)
        self.assertIn('business@example.com', allowed)
        self.assertIn('sample_contact', allowed)
        self.assertEqual(permitted_contacts('商务联系。个人号码 13800138000',
                                            source(contact_consent=True)), '')
        self.assertEqual(permitted_contacts('私人号码 13800138000',
                                            source(contact_consent=True)), '')
        self.assertEqual(permitted_contacts('商务联系 ' + '字' * 241 + '13800138000',
                                            source(contact_consent=True)), '')

    def test_original_text_always_masks_contacts_and_identity(self):
        value = '手机 13800138000，邮箱 private@example.com，微信：sample_contact；身份证 110101199001011234'
        safe = redact(value)
        for secret in ('13800138000', 'private@example.com', 'sample_contact', '110101199001011234'):
            self.assertNotIn(secret, safe)
        self.assertIn('[身份信息已隐藏]', safe)

    def test_explicit_contact_refusal_is_never_an_invitation(self):
        for content in ('不要商务联系 13800138000',
                        '拒绝商业合作 business@example.com',
                        '请勿商务联系，微信：sample_contact'):
            with self.subTest(content=content):
                self.assertEqual(permitted_contacts(content, source(contact_consent=True)), '')

    def test_custom_insurance_industry_does_not_bypass_privacy(self):
        record = analyze(item(title='杭州保险咨询', text='杭州想买保险，有糖尿病。'),
                         task(industry='保险代理', keywords=['保险']), source())
        self.assertIsNone(record)

    def test_search_snippets_never_supply_contact_lists(self):
        record = analyze(item(text=item()['text'] + '\n商务联系 13800138000',
                              source_type='搜索摘要（需人工核实原文）'),
                         task(), source(kind='brave_search', contact_consent=True))
        self.assertIsNotNone(record)
        self.assertEqual(record['contact'], '')
        self.assertEqual(record['contact_source'], '')

    def test_url_canonicalization_removes_tracking_but_preserves_query(self):
        value = canonical_url('https://EXAMPLE.com/request?a=1&utm_source=x&fbclid=abc#part')
        self.assertEqual(value, 'https://example.com/request?a=1')
        for value in ('file:///secret.txt', 'https://user:password@example.com/', 'javascript:alert(1)'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_url(value)


class DatabaseAndExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lead-radar-test-')
        self.db = Database(Path(self.temp.name) / 'isolated.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_first_database_has_no_seeds(self):
        for table in ('sources', 'tasks', 'leads', 'runs'):
            self.assertEqual(self.db.all(table), [])

    def test_duplicate_collection_preserves_manual_review_and_draft(self):
        record = analyze(item(), task(), source())
        self.assertTrue(self.db.insert_lead(record))
        self.db.update_lead(record['id'], {'review_status': 'approved', 'status': 'contacted',
                                          'draft': '人工审核后的实际话术', 'notes': '审核记录'})
        rediscovered = analyze(item(url='https://example.org/syndicated-copy'), task(), source())
        self.assertEqual(record['id'], rediscovered['id'])
        self.assertFalse(self.db.insert_lead(rediscovered))
        saved = self.db.get('leads', record['id'])
        self.assertEqual(saved['review_status'], 'approved')
        self.assertEqual(saved['status'], 'contacted')
        self.assertEqual(saved['draft'], '人工审核后的实际话术')
        self.assertEqual(saved['notes'], '审核记录')
        self.assertEqual(len(self.db.all('leads')), 1)

    def test_filter_and_csv_use_same_selected_records(self):
        first = analyze(item(), task(), source())
        second = analyze(item(title='上海装修咨询', text='上海准备装修，求报价施工方案。',
                              url='https://example.com/request/2'),
                         task(id=2, regions=['上海'], industry='自定义服务'), source())
        self.db.insert_lead(first)
        self.db.insert_lead(second)
        self.db.update_lead(first['id'], {'review_status': 'approved', 'status': 'contacted'})
        selected = filtered_leads(self.db, {'region': '杭州', 'min_score': '60',
                                           'review_status': 'approved', 'status': 'contacted'})
        self.assertEqual([value['id'] for value in selected], [first['id']])
        industry_selected = filtered_leads(self.db, {'industry': '自定义服务'})
        self.assertEqual([value['id'] for value in industry_selected], [second['id']])
        exported = list(csv.reader(io.StringIO(csv_export(selected).decode('utf-8-sig'))))
        self.assertEqual(len(exported), 2)
        self.assertEqual(exported[1][0], first['id'])
        with self.assertRaises(ValueError):
            filtered_leads(self.db, {'min_score': 'invalid'})

    def test_export_hides_unreviewed_contact_and_draft(self):
        record = analyze(item(text=item()['text'] + '\n商务联系 13800138000'),
                         task(), source(contact_consent=True))
        for review in ('pending', 'rejected'):
            record['review_status'] = review
            value = csv_export([record]).decode('utf-8-sig')
            self.assertNotIn('13800138000', value)
            self.assertNotIn(record['draft'], value)
            self.assertIn('[待人工审核]', value)
        record['review_status'] = 'approved'
        approved = csv_export([record]).decode('utf-8-sig')
        self.assertIn('13800138000', approved)
        self.assertIn(record['draft'], approved)

    def test_export_neutralizes_spreadsheet_formulas(self):
        record = analyze(item(), task(), source())
        record.update(title='=HYPERLINK("https://example.com")', notes='+cmd',
                      platform='@SUM(1,1)', review_status='approved', draft='-2+3')
        rows = list(csv.reader(io.StringIO(csv_export([record]).decode('utf-8-sig'))))
        cells = dict(zip(rows[0], rows[1]))
        for name in ('标题', '备注', '来源平台', '人工跟进话术'):
            self.assertTrue(cells[name].startswith("'"), name)

    def test_retention_and_delete_only_affect_local_records(self):
        record = analyze(item(), task(), source())
        old = dict(record, id='old', collected_at=(datetime.now(timezone.utc) -
                   timedelta(days=91)).isoformat(timespec='seconds'), record_id='rec-remote')
        self.db.insert_lead(old)
        self.db.insert_lead(record)
        self.assertEqual(self.db.retain(90), 1)
        self.assertEqual([value['id'] for value in self.db.all('leads')], [record['id']])
        self.db.delete('leads', record['id'])
        self.assertEqual(self.db.all('leads'), [])


if __name__ == '__main__':
    unittest.main()
