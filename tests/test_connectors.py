"""Connector safety and integration tests. All synthetic content is test-only.

Fixtures never enter the application database or production source list.
"""
import json
import socket
import unittest
import urllib.parse
import uuid
from unittest.mock import patch

from radar import collectors
from radar.collectors import CollectorError, FetchResult, collect, safe_fetch, validate_public_url
from radar.feishu import FeishuClient, FeishuError, FIELD_TYPES

PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
CONFIG = {"app_token": "testbase", "table_id": "testtable", "tenant_access_token": "TEST_ONLY_SECRET"}


def response(body, url="https://public.example/feed", status=200, content_type="text/plain"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return FetchResult(body, {"content-type": content_type + "; charset=utf-8"}, url, status)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.dns = patch("radar.collectors.socket.getaddrinfo", return_value=PUBLIC_DNS)
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.source = {"id": 1, "kind": "rss", "url": "https://public.example/feed",
                       "permission_confirmed": True, "platform": "测试隔离来源"}
        self.task = {"keywords": ["装修"], "regions": ["上海"]}

    def test_permission_and_restricted_platforms_are_required(self):
        with self.assertRaises(CollectorError):
            collect({**self.source, "permission_confirmed": False}, self.task)
        with self.assertRaises(CollectorError):
            collect({**self.source, "url": "https://www.xiaohongshu.com/explore/1"}, self.task)
        with self.assertRaises(CollectorError):
            collect({**self.source, "kind": "json_api"}, self.task)

    def test_dns_private_mixed_addresses_and_credentials_are_blocked(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch("radar.collectors.socket.getaddrinfo", return_value=PUBLIC_DNS + private):
            with self.assertRaises(CollectorError):
                validate_public_url("https://public.example/")
        for url in ("file:///tmp/a", "http://user:password@public.example/", "http://localhost/", "http://public.example:8888/", "https://public.example/?api_key=TEST_ONLY_SECRET"):
            with self.subTest(url=url), self.assertRaises(CollectorError):
                validate_public_url(url)

    def test_rss_preserves_original_and_ignores_untraceable_entries(self):
        fixture = """<?xml version='1.0'?><rss><channel>
        <item><title>隔离测试标题</title><description><![CDATA[<p>上海装修需求隔离测试</p>]]></description>
        <link>https://public.example/post/1</link><pubDate>Fri, 02 Oct 2026 01:00:00 GMT</pubDate></item>
        <item><title>缺少原始链接</title><description>不会成为线索</description></item>
        <item><title>重复</title><description>上海装修需求隔离测试</description><link>https://public.example/post/1</link></item>
        </channel></rss>"""
        with patch("radar.collectors.safe_fetch", side_effect=[response("", status=404), response(fixture)]):
            items = collect(self.source, self.task)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "上海装修需求隔离测试")
        self.assertEqual(items[0]["url"], "https://public.example/post/1")
        self.assertTrue(items[0]["published_at"])
        self.assertNotIn("contact", items[0])

    def test_atom_and_xml_entity_refusal(self):
        fixture = """<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Atom 隔离测试</title>
        <summary>真实格式解析测试文本</summary><link href='/post/atom'/><updated>2026-10-02</updated>
        <author><name>测试作者</name></author></entry></feed>"""
        with patch("radar.collectors.safe_fetch", side_effect=[response("", status=404), response(fixture)]):
            items = collect(self.source, self.task)
        self.assertEqual(items[0]["author"], "测试作者")
        self.assertEqual(items[0]["url"], "https://public.example/post/atom")
        with patch("radar.collectors.safe_fetch", side_effect=[response("", status=404), response("<!DOCTYPE rss [<!ENTITY a 'bomb'>]><rss/>")]):
            with self.assertRaises(CollectorError):
                collect(self.source, self.task)

    def test_robots_denial_and_uncertain_permission_fail_closed(self):
        for robots in ("User-agent: *\nDisallow: /feed", "<html>login</html>"):
            with patch("radar.collectors.safe_fetch", return_value=response(robots)) as fetch:
                with self.assertRaises(CollectorError):
                    collect(self.source, self.task)
                self.assertEqual(fetch.call_count, 1)
        with patch("radar.collectors.safe_fetch", side_effect=CollectorError("HTTP 403")):
            with self.assertRaises(CollectorError):
                collect(self.source, self.task)

    def test_web_article_excludes_navigation_and_scripts_and_blocks_login(self):
        source = {**self.source, "kind": "web"}
        fixture = "<html><title>隔离测试文章</title><nav>电话不会提取</nav><main><p>上海装修需求来自指定页面。</p><script>恶意脚本</script></main></html>"
        with patch("radar.collectors.safe_fetch", side_effect=[response("", status=404), response(fixture, content_type="text/html")]):
            items = collect(source, self.task)
        self.assertEqual(items[0]["text"], "上海装修需求来自指定页面。")
        login = "<html><title>登录</title><form><input type=password></form></html>"
        with patch("radar.collectors.safe_fetch", side_effect=[response("", status=404), response(login, content_type="text/html")]):
            with self.assertRaises(CollectorError):
                collect(source, self.task)

    def test_json_api_environment_token_query_and_public_item_url(self):
        source = {**self.source, "kind": "json_api", "api_authorized": True,
                  "url": "https://public.example/search?q={query}&city={region}", "api_token_env": "RADAR_TEST_API_TOKEN"}
        fixture = json.dumps({"items": [{"title": "API 隔离测试", "text": "上海装修内容", "url": "https://public.example/post/api"},
                                        {"text": "禁止私网链接", "url": "http://localhost/private"}]}, ensure_ascii=False)
        with patch.dict("os.environ", {"RADAR_TEST_API_TOKEN": "TEST_ONLY_API_SECRET"}), patch("radar.collectors.safe_fetch", return_value=response(fixture)) as fetch:
            items = collect(source, self.task)
            called_url = fetch.call_args.args[0]
            self.assertIn(urllib.parse.quote("装修"), called_url)
            self.assertEqual(fetch.call_args.kwargs["headers"]["Authorization"], "Bearer TEST_ONLY_API_SECRET")
        self.assertEqual(len(items), 1)
        self.assertNotIn("TEST_ONLY_API_SECRET", json.dumps(items))

    def test_cross_origin_redirect_is_blocked_before_second_connection(self):
        class FakeResponse:
            status = 302
            def getheaders(self):
                return [("Location", "https://unapproved.example/other")]
        class FakeConnection:
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass
        with patch("radar.collectors._PinnedHTTPConnection", side_effect=FakeConnection) as connections:
            with self.assertRaises(CollectorError):
                safe_fetch("https://public.example/feed")
            self.assertEqual(connections.call_count, 1)

    def test_brave_fixed_endpoint_real_response_contract_and_summary_provenance(self):
        source = {**self.source, "kind": "brave_search", "api_authorized": True,
                  "storage_authorized": True, "storage_authorization_note": "隔离测试：模拟已获书面存储许可",
                  "url": "https://api.search.brave.com/res/v1/web/search", "api_token_env": "RADAR_TEST_BRAVE_TOKEN"}
        fixture = {"type": "search", "web": {"results": [
            {"title": "隔离搜索结果", "description": "上海<b>装修</b>求推荐 &amp; 预算咨询", "url": "https://public.example/lead/1"},
            {"title": "私网", "description": "不收集", "url": "http://localhost/private"},
            {"title": "受限平台索引摘要", "description": "不抓取链接页面或评论", "url": "https://www.xiaohongshu.com/explore/example"}]}}
        with patch.dict("os.environ", {"RADAR_TEST_BRAVE_TOKEN": "TEST_ONLY_BRAVE_SECRET"}), patch("radar.collectors.safe_fetch", return_value=response(json.dumps(fixture, ensure_ascii=False))) as fetch:
            items = collect(source, self.task)
            self.assertEqual(fetch.call_count, 1)  # API only; no linked-page requests.
            self.assertEqual(fetch.call_args.kwargs["headers"]["X-Subscription-Token"], "TEST_ONLY_BRAVE_SECRET")
            self.assertNotIn("Authorization", fetch.call_args.kwargs["headers"])
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.call_args.args[0]).query)
            self.assertEqual(query["q"], ['"上海" AND "装修"'])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source_type"], "搜索摘要（需人工核实原文）")
        self.assertEqual(items[0]["platform"], "public.example")
        self.assertEqual(items[0]["text"], "上海装修求推荐 & 预算咨询")
        self.assertNotIn("contact", items[0])

    def test_brave_requires_authorized_key_and_never_accepts_custom_token_endpoint(self):
        source = {**self.source, "kind": "brave_search", "url": "", "api_authorized": True,
                  "storage_authorized": True, "storage_authorization_note": "隔离测试：模拟已获书面存储许可"}
        with patch("radar.collectors.safe_fetch") as fetch:
            with self.assertRaises(CollectorError):
                collect(source, self.task)
            fetch.assert_not_called()

    def test_server_injected_protected_tokens_take_precedence_and_are_not_returned(self):
        fixture = {"type": "search", "web": {"results": [
            {"title": "隔离测试", "description": "上海装修索引摘要", "url": "https://public.example/a", "page_age": "2026-10-02"}]}}
        source = {**self.source, "kind": "brave_search", "url": "", "api_authorized": True,
                  "storage_authorized": True, "storage_authorization_note": "隔离测试：模拟已获书面存储许可",
                  "_api_token": "TEST_ONLY_PROTECTED_TOKEN", "api_token_env": "RADAR_TEST_BRAVE_TOKEN"}
        with patch.dict("os.environ", {"RADAR_TEST_BRAVE_TOKEN": "SHOULD_NOT_BE_USED"}), patch("radar.collectors.safe_fetch", return_value=response(json.dumps(fixture, ensure_ascii=False))) as fetch:
            items = collect(source, self.task)
            self.assertEqual(fetch.call_args.kwargs["headers"]["X-Subscription-Token"], "TEST_ONLY_PROTECTED_TOKEN")
        self.assertEqual(items[0]["published_at"], "")
        self.assertNotIn("TEST_ONLY_PROTECTED_TOKEN", json.dumps(items))
        api_source = {**source, "kind": "json_api", "url": self.source["url"]}
        with patch("radar.collectors.safe_fetch", return_value=response('{"items":[]}')) as fetch:
            self.assertEqual(collect(api_source, self.task), [])
            self.assertEqual(fetch.call_args.kwargs["headers"]["Authorization"], "Bearer TEST_ONLY_PROTECTED_TOKEN")
        with patch.dict("os.environ", {"RADAR_TEST_BRAVE_TOKEN": "TEST_ONLY_BRAVE_SECRET"}), patch("radar.collectors.safe_fetch") as fetch:
            with self.assertRaises(CollectorError):
                collect({**source, "url": "https://unapproved.example/steal", "api_token_env": "RADAR_TEST_BRAVE_TOKEN"}, self.task)
            fetch.assert_not_called()

    def test_brave_region_and_keyword_groups_use_any_semantics_without_truncation(self):
        source = {**self.source, "kind": "brave_search", "url": "", "api_authorized": True,
                  "storage_authorized": True, "storage_authorization_note": "隔离测试：模拟已获书面存储许可",
                  "_api_token": "TEST_ONLY_PROTECTED_TOKEN"}
        task = {"regions": ["杭州", "余杭"], "keywords": ["求装修", "装修报价", "找装修公司"]}
        with patch("radar.collectors.safe_fetch", return_value=response('{"type":"search","web":{"results":[]}}')) as fetch:
            self.assertEqual(collect(source, task), [])
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.call_args.args[0]).query)["q"][0]
        self.assertEqual(query, '("杭州" OR "余杭") AND ("求装修" OR "装修报价" OR "找装修公司")')
        # Alternative regions / intentions are never required together. Long
        # configurations fail explicitly rather than silently dropping terms.
        with patch("radar.collectors.safe_fetch") as fetch:
            with self.assertRaises(CollectorError) as error:
                collect(source, {"regions": ["杭州"], "keywords": ["需求" * 310, "装修"]})
            self.assertIn("减少地区或关键词", str(error.exception))
            fetch.assert_not_called()

    def test_brave_phrases_cannot_escape_added_quotes(self):
        query = collectors._brave_query({"regions": ['上海" OR site:private.example \\'], "keywords": ["求装修"]})
        self.assertEqual(query.count('"'), 4)
        self.assertEqual(query, '"上海 OR site:private.example" AND "求装修"')

    def test_response_size_limit(self):
        class FakeResponse:
            status = 200
            def getheaders(self):
                return [("Content-Length", str(collectors.MAX_BYTES + 1))]
        class FakeConnection:
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass
        with patch("radar.collectors._PinnedHTTPConnection", side_effect=FakeConnection):
            with self.assertRaises(CollectorError):
                safe_fetch("https://public.example/feed")

    def test_redirected_page_permission_checked_before_request(self):
        class FakeResponse:
            status = 302
            def getheaders(self):
                return [("Location", "/robots-denied-path")]
        class FakeConnection:
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass
        with patch("radar.collectors._PinnedHTTPConnection", side_effect=FakeConnection) as connection, patch("radar.collectors.check_robots", side_effect=CollectorError("robots denied")) as robots:
            with self.assertRaises(CollectorError):
                safe_fetch("https://public.example/feed", redirect_check=robots)
            connection.assert_called_once()
            robots.assert_called_once_with("https://public.example/robots-denied-path")

    def test_robots_interval_is_used_before_following_fetch(self):
        class FakeResponse:
            status = 200
            def getheaders(self):
                return []
            def read1(self, size):
                return b""
        class FakeConnection:
            sock = None
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass
        with patch("radar.collectors.safe_fetch", return_value=response("User-agent: *\nCrawl-delay: 3\nDisallow:")):
            collectors.check_robots("https://public.example/feed")
        with patch.dict(collectors._HOST_LAST_REQUEST, {"public.example": collectors.time.monotonic()}), patch("radar.collectors.time.sleep") as sleep, patch("radar.collectors._PinnedHTTPConnection", side_effect=FakeConnection):
            safe_fetch("https://public.example/feed")
            self.assertGreaterEqual(sleep.call_args.args[0], 2.5)
        collectors._HOST_MIN_INTERVAL.pop("public.example", None)


class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.client = FeishuClient(CONFIG)
        self.lead = {"id": "isolated-test-lead-001", "review_status": "approved", "text": "仅在测试中存在",
                     "industry": "装修", "score": 76, "reasons": ["关键词匹配"], "contact": "", "status": "new"}
        self.schema = {"items": [{"field_name": name, "type": field_type} for name, field_type in FIELD_TYPES.items()]}

    def test_schema_requires_exact_names_and_types(self):
        with patch.object(self.client, "_request", return_value=self.schema):
            self.assertTrue(self.client.check()["ok"])
        bad = {"items": self.schema["items"][:-1] + [{"field_name": "跟进状态", "type": 3}]}
        with patch.object(self.client, "_request", return_value=bad):
            result = self.client.check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["type_errors"][0]["field_name"], "跟进状态")

    def test_review_gate_prevents_any_external_call(self):
        with patch.object(self.client, "_request") as request:
            with self.assertRaises(FeishuError):
                self.client.upsert({**self.lead, "review_status": "pending"})
            request.assert_not_called()

    def test_create_uses_query_filter_and_stable_client_uuid_then_updates(self):
        requests = []
        record_created = False
        def api(method, path, body=None):
            nonlocal record_created
            requests.append((method, path, body))
            if "/fields?" in path:
                return self.schema
            if "/records/search?" in path:
                return {"items": [{"record_id": "rectest001"}] if record_created else []}
            if method == "POST" and "/records?" in path:
                record_created = True
                return {"record": {"record_id": "rectest001"}}
            if method == "PUT":
                return {"record": {"record_id": "rectest001"}}
            self.fail("unexpected API call")
        with patch.object(self.client, "_request", side_effect=api):
            first = self.client.upsert(self.lead)
            second = self.client.upsert({**self.lead, "draft": "审核后的更新"})
        self.assertEqual(first, second)
        creations = [r for r in requests if r[0] == "POST" and "/records?" in r[1]]
        self.assertEqual(len(creations), 1)
        token = urllib.parse.parse_qs(urllib.parse.urlsplit(creations[0][1]).query)["client_token"][0]
        self.assertEqual(str(uuid.UUID(token)), token)
        search = next(r for r in requests if "/records/search?" in r[1])
        self.assertEqual(search[2]["filter"]["conditions"][0]["value"], [self.lead["id"]])
        self.assertEqual(creations[0][2]["fields"]["公开联系方式"], "")
        self.assertEqual(creations[0][2]["fields"]["意向分"], 76)
        self.assertTrue(any(r[0] == "PUT" for r in requests))

    def test_remote_failure_redacts_secrets(self):
        payload = {"code": 99991663, "msg": "TEST_ONLY_SECRET echoed by remote"}
        with patch("radar.feishu.safe_fetch", return_value=response(json.dumps(payload))):
            with self.assertRaises(FeishuError) as error:
                self.client.check()
        self.assertNotIn("TEST_ONLY_SECRET", str(error.exception))

    def test_duplicate_remote_id_fails_without_write(self):
        self.client._schema_ok = True
        with patch.object(self.client, "_request", return_value={"items": [{"record_id": "rec1"}, {"record_id": "rec2"}]}) as request:
            with self.assertRaises(FeishuError):
                self.client.upsert(self.lead)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
