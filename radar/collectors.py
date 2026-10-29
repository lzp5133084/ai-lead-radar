"""Public-source collectors. Production never generates sample leads.

Network connections are pinned to validated public DNS addresses. Every redirect
is checked, private networks and restricted consumer platforms are refused, and
RSS/web access observes robots.txt. This is deliberately a one-page collector,
not a crawler, browser login tool, or CAPTCHA solver.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

USER_AGENT = "LeadRadar/1.0 (+manual-approved-public-source; no-contact-automation)"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 50
TIMEOUT = 12
_HOST_PACING_LOCK = threading.Lock()
_HOST_LAST_REQUEST: dict[str, float] = {}
_HOST_MIN_INTERVAL: dict[str, float] = {}
RESTRICTED_DOMAINS = (
    "xiaohongshu.com", "xhslink.com", "xhs.cn", "douyin.com",
    "iesdouyin.com", "tiktok.com", "weixin.qq.com", "wx.qq.com",
    "kuaishou.com", "zhihu.com",
)
SEARCH_KINDS = ("brave_search", "tavily_search", "bing_rss", "volc_search", "baidu_search")
API_KINDS = ("json_api",) + SEARCH_KINDS


class CollectorError(RuntimeError):
    """A safe, user-facing collection failure; messages contain no credentials."""


def _restricted(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in RESTRICTED_DOMAINS)


def _resolve_addresses(host: str, port: int) -> list:
    """Bound OS DNS lookup, whose native timeout may otherwise be very long.

    A resolver thread is daemonized so an unavailable DNS service cannot keep
    the application alive at shutdown. The connection still uses only the
    addresses returned here; it never resolves the hostname a second time.
    """
    result_queue = queue.Queue(maxsize=1)
    def lookup():
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            addresses = None
        result_queue.put(addresses)
    threading.Thread(target=lookup, daemon=True, name="radar-dns").start()
    try:
        addresses = result_queue.get(timeout=8)
    except queue.Empty:
        raise CollectorError("来源域名解析超时，请检查本机网络和 DNS。") from None
    if addresses is None:
        raise CollectorError("来源域名无法解析，请检查网络或 URL。")
    return addresses


def validate_public_url(url: str, *, allow_restricted: bool = False, reference_only: bool = False) -> tuple[str, str, int, list]:
    if not isinstance(url, str) or not url or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise CollectorError("来源 URL 无效：请使用完整的公开 HTTP(S) 地址。")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (ValueError, UnicodeError):
        raise CollectorError("来源 URL 格式无效。") from None
    if parsed.scheme.lower() not in ("http", "https") or not host or parsed.username is not None or parsed.password is not None:
        raise CollectorError("仅支持不含账号密码的公开 HTTP(S) URL。")
    sensitive_query_names = {"access_token", "api_key", "apikey", "token", "password", "passwd", "secret", "app_secret", "key", "auth", "authorization"}
    if any(name.lower().replace("-", "_") in sensitive_query_names for name, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)):
        raise CollectorError("URL 不得包含凭证；请在本机的受保护配置中填写 API 密钥。")
    if port not in (80, 443) or "\\" in url or "%" in host:
        raise CollectorError("只允许公开网站的标准 80 / 443 端口。")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise CollectorError("禁止访问本机、局域网或保留域名。")
    if not allow_restricted and _restricted(host):
        raise CollectorError("该平台不支持公开页面采集；请使用合法授权的独立 API 服务。")
    # Search references are displayed links, not request destinations. DNS
    # lookup here would discard every link on a legitimate synthetic-DNS VPN.
    # Literal private addresses still cannot be retained as public references.
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and (not literal_ip.is_global or literal_ip.is_multicast or literal_ip.is_reserved):
        raise CollectorError("来源指向本机、局域网或非公开地址，已阻止。")
    addresses = [] if reference_only else _resolve_addresses(host, port)
    if not addresses and not reference_only:
        raise CollectorError("来源域名没有可访问地址。")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError:
            raise CollectorError("来源域名返回了无效地址。") from None
        if (not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or (getattr(ip, "ipv4_mapped", None) is not None and not ip.ipv4_mapped.is_global)):
            raise CollectorError("来源指向本机、局域网或非公开地址，已阻止。")
    netloc = ("[" + host + "]") if ":" in host else host
    if port != (443 if parsed.scheme.lower() == "https" else 80):
        netloc += ":" + str(port)
    normalized = urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    return normalized, host, port, addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: list, secure: bool, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._addresses = addresses
        self._secure = secure

    def connect(self):
        last = None
        # Connection uses these DNS results, never a second hostname resolution.
        deadline = time.monotonic() + self.timeout
        for family, socktype, proto, _canon, sockaddr in self._addresses:
            sock = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(remaining)
                sock.connect(sockaddr)
                if self._secure:
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
                self.sock = sock
                return
            except (OSError, ssl.SSLError) as exc:
                last = exc
                if sock is not None:
                    sock.close()
        raise OSError("public endpoint connection failed") from last


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    headers: dict
    url: str
    status: int


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectorError("官方搜索接口发生重定向，已停止；密钥不会转发到其他地址。")


def official_tavily_fetch(url: str, *, method: str, headers: dict, body: bytes) -> FetchResult:
    """Normal verified HTTPS only for the hard-coded, authorized API.

    This path supports Windows VPN synthetic DNS / user-configured system
    proxies. It cannot be used for arbitrary URLs, RSS, webpages or redirects.
    TLS hostname and certificate validation remain mandatory. Source result
    URLs are never followed through this transport.
    """
    endpoint = "https://api.tavily.com/search"
    if url != endpoint or method != "POST":
        raise CollectorError("网络兼容连接仅支持固定的 Tavily 官方 HTTPS 搜索接口。")
    if not isinstance(body, bytes) or len(body) > 64 * 1024:
        raise CollectorError("搜索请求格式或大小无效。")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(_RejectRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(endpoint, data=body, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            status = response.status
            if not 200 <= status < 300:
                raise CollectorError(f"搜索接口返回 HTTP {status}，请检查凭证和额度。")
            result_headers = {k.lower(): v for k, v in response.headers.items()}
            if result_headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise CollectorError("搜索接口未返回支持的未压缩响应。")
            length = result_headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise CollectorError("搜索响应超过安全大小限制。")
            content = bytearray()
            deadline = time.monotonic() + TIMEOUT
            while len(content) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise CollectorError("搜索接口响应超时，请稍后再试。")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise CollectorError("搜索响应超过安全大小限制。")
            return FetchResult(bytes(content), result_headers, endpoint, status)
    except CollectorError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (301, 302, 303, 307, 308):
            raise CollectorError("官方搜索接口重定向已阻止。") from None
        reason = {401: "密钥无效或已失效，请在本机重新填写 Tavily Key。",
                  403: "接口拒绝访问，请检查账号授权。", 429: "请求过于频繁，请稍后再试。",
                  432: "免费额度或账号用量已达到限制，请先停止采集。",
                  433: "账号用量已达到设置的限制，请先停止采集。"}.get(status, "请检查账号状态、额度和网络。")
        raise CollectorError(f"Tavily 返回 HTTP {status}：{reason}") from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise CollectorError("官方搜索接口连接失败；请检查本机网络或代理，未保存采集结果。") from None


def official_bing_fetch(url: str, *, headers: dict) -> FetchResult:
    """Normal verified HTTPS only for the hard-coded Bing China RSS endpoint.

    Same transport policy as official_tavily_fetch: supports Windows VPN
    synthetic DNS / user-configured system proxies, mandatory TLS validation,
    no redirects. Endpoint host is fixed; only the query string may vary.
    """
    endpoint = "https://cn.bing.com/search"
    if not url.startswith(endpoint + "?"):
        raise CollectorError("网络兼容连接仅支持固定的必应国内版官方 HTTPS 搜索地址。")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(_RejectRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            status = response.status
            if not 200 <= status < 300:
                raise CollectorError(f"必应国内搜索返回 HTTP {status}，请稍后再试。")
            result_headers = {k.lower(): v for k, v in response.headers.items()}
            if result_headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise CollectorError("必应国内搜索未返回支持的未压缩响应。")
            length = result_headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise CollectorError("必应国内搜索响应超过安全大小限制。")
            content = bytearray()
            deadline = time.monotonic() + TIMEOUT
            while len(content) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise CollectorError("必应国内搜索响应超时，请稍后再试。")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise CollectorError("必应国内搜索响应超过安全大小限制。")
            return FetchResult(bytes(content), result_headers, url, status)
    except CollectorError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (301, 302, 303, 307, 308):
            raise CollectorError("必应国内搜索重定向已阻止。") from None
        reason = {403: "接口拒绝访问，请稍后再试或检查本机网络。",
                  429: "请求过于频繁，请稍后再试。"}.get(status, "请检查本机网络或代理。")
        raise CollectorError(f"必应国内搜索返回 HTTP {status}：{reason}") from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise CollectorError("必应国内搜索连接失败；请检查本机网络或代理，未保存采集结果。") from None


VOLC_TIMEOUT = 150  # 方舟 Responses 含多次联网检索与推理，单次调用明显慢于普通搜索。


def official_volc_fetch(url: str, *, headers: dict, body: bytes) -> FetchResult:
    """Normal verified HTTPS only for the hard-coded Volcengine Ark Responses API.

    Same transport policy as official_tavily_fetch: VPN synthetic DNS and
    system proxies are honored, TLS validation is mandatory, redirects are
    rejected so credentials can never leak to another host.
    """
    endpoint = "https://ark.cn-beijing.volces.com/api/v3/responses"
    if url != endpoint:
        raise CollectorError("网络兼容连接仅支持固定的火山方舟官方 HTTPS 接口。")
    if not isinstance(body, bytes) or len(body) > 64 * 1024:
        raise CollectorError("搜索请求格式或大小无效。")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(_RejectRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(endpoint, data=body, headers=request_headers, method="POST")
    try:
        with opener.open(request, timeout=VOLC_TIMEOUT) as response:
            status = response.status
            if not 200 <= status < 300:
                raise CollectorError(f"火山方舟联网搜索返回 HTTP {status}，请检查凭证、接入点与插件额度。")
            result_headers = {k.lower(): v for k, v in response.headers.items()}
            if result_headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise CollectorError("火山方舟未返回支持的未压缩响应。")
            length = result_headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise CollectorError("火山方舟响应超过安全大小限制。")
            content = bytearray()
            deadline = time.monotonic() + VOLC_TIMEOUT
            while len(content) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise CollectorError("火山方舟响应超时，请稍后再试。")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise CollectorError("火山方舟响应超过安全大小限制。")
            return FetchResult(bytes(content), result_headers, url, status)
    except CollectorError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        detail = ""
        try:
            detail = exc.read(600).decode("utf-8", "replace")
        except Exception:
            pass
        exc.close()
        if status in (301, 302, 303, 307, 308):
            raise CollectorError("火山方舟接口重定向已阻止，凭证不会转发。") from None
        reason = {401: "凭证无效，请检查 API 密钥。", 403: "接入点无权限或联网插件未开通。",
                  404: "接入点不存在或未开通联网搜索插件。", 429: "调用过于频繁或额度不足，请稍后再试。"}.get(status, "请检查网络或稍后再试。")
        raise CollectorError(f"火山方舟联网搜索返回 HTTP {status}：{reason}{detail[:200]}") from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise CollectorError("火山方舟连接失败；请检查本机网络或代理，未保存采集结果。") from None


def _volc_items(payload) -> list:
    """Ark Responses output → candidate items.

    Only url_citation annotations are used: those URLs were genuinely fetched
    by the search tool. The model's free text is never turned into a lead,
    which keeps hallucinated links out of the database.
    """
    if not isinstance(payload, dict):
        raise CollectorError("火山方舟响应格式异常。")
    if payload.get("error"):
        message = payload["error"].get("message", "") if isinstance(payload["error"], dict) else str(payload["error"])
        raise CollectorError("火山方舟联网搜索未成功：" + str(message)[:200])
    raw_items = []
    from .core import SENSITIVE
    for block in payload.get("output") or []:
        if not isinstance(block, dict) or block.get("type") != "message":
            continue
        for part in block.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            for cite in part.get("annotations") or []:
                if not isinstance(cite, dict) or cite.get("type") != "url_citation":
                    continue
                title = html_to_text(str(cite.get("title") or ""))
                summary = html_to_text(str(cite.get("summary") or ""))
                if not summary or SENSITIVE.search(title + "\n" + summary):
                    continue
                raw_items.append({"title": title, "text": summary,
                                  "url": str(cite.get("url") or ""),
                                  "published_at": str(cite.get("publish_time") or "")[:100], "author": ""})
    if not raw_items:
        raise CollectorError("火山方舟本次未返回可核实的引用来源。")
    return raw_items


def official_baidu_fetch(url: str, *, headers: dict, body: bytes) -> FetchResult:
    """Normal verified HTTPS only for the hard-coded Baidu AI Search API.

    Same transport policy as official_volc_fetch: VPN synthetic DNS and
    system proxies are honored, TLS validation is mandatory, redirects are
    rejected so credentials can never leak to another host. Baidu answers
    quickly, so the ordinary TIMEOUT applies instead of VOLC_TIMEOUT.
    """
    endpoint = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    if url != endpoint:
        raise CollectorError("网络兼容连接仅支持固定的百度AI搜索官方 HTTPS 接口。")
    if not isinstance(body, bytes) or len(body) > 64 * 1024:
        raise CollectorError("搜索请求格式或大小无效。")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(_RejectRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(endpoint, data=body, headers=request_headers, method="POST")
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            status = response.status
            if not 200 <= status < 300:
                raise CollectorError(f"百度AI搜索返回 HTTP {status}，请检查凭证与调用额度。")
            result_headers = {k.lower(): v for k, v in response.headers.items()}
            if result_headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise CollectorError("百度AI搜索未返回支持的未压缩响应。")
            length = result_headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise CollectorError("百度AI搜索响应超过安全大小限制。")
            content = bytearray()
            deadline = time.monotonic() + TIMEOUT
            while len(content) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise CollectorError("百度AI搜索响应超时，请稍后再试。")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise CollectorError("百度AI搜索响应超过安全大小限制。")
            return FetchResult(bytes(content), result_headers, url, status)
    except CollectorError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        detail = ""
        try:
            detail = exc.read(600).decode("utf-8", "replace")
        except Exception:
            pass
        exc.close()
        if status in (301, 302, 303, 307, 308):
            raise CollectorError("百度AI搜索接口重定向已阻止，凭证不会转发。") from None
        reason = {401: "凭证无效，请检查 API Key。", 403: "接口未开通或无权限。",
                  404: "接口地址不存在。", 429: "调用过于频繁或额度不足，请稍后再试。"}.get(status, "请检查网络或稍后再试。")
        raise CollectorError(f"百度AI搜索返回 HTTP {status}：{reason}{detail[:200]}") from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise CollectorError("百度AI搜索连接失败；请检查本机网络或代理，未保存采集结果。") from None


def _baidu_items(payload) -> list:
    """Baidu AI Search references → candidate items.

    Only entries in the references array are used: those URLs were genuinely
    retrieved by Baidu's search index. Free-text model output is never turned
    into a lead, which keeps hallucinated links out of the database.
    """
    if not isinstance(payload, dict):
        raise CollectorError("百度AI搜索响应格式异常。")
    if payload.get("error") or payload.get("code") not in (None, 0, "0"):
        message = payload.get("message") or payload.get("error") or payload.get("code")
        raise CollectorError("百度AI搜索未成功：" + str(message)[:200])
    references = payload.get("references")
    if not isinstance(references, list):
        raise CollectorError("百度AI搜索本次未返回可核实的引用来源。")
    raw_items = []
    from .core import SENSITIVE
    for cite in references:
        if not isinstance(cite, dict):
            continue
        title = html_to_text(str(cite.get("title") or ""))
        snippet = html_to_text(str(cite.get("content") or cite.get("summary") or cite.get("snippet") or ""))
        if not snippet or SENSITIVE.search(title + "\n" + snippet):
            continue
        raw_items.append({"title": title, "text": snippet,
                          "url": str(cite.get("url") or ""),
                          "published_at": str(cite.get("publish_time") or cite.get("date") or "")[:100], "author": ""})
    if not raw_items:
        raise CollectorError("百度AI搜索本次未返回可核实的引用来源。")
    return raw_items


def safe_fetch(url: str, *, method: str = "GET", headers: dict | None = None,
               body: bytes | None = None, allow_restricted: bool = False,
               allow_status: tuple = (), max_bytes: int = MAX_BYTES,
               redirects: int = 3, redirect_check=None) -> FetchResult:
    """Fetch public HTTP(S), with pinned DNS and no ambient proxy credentials."""
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    original_host = None
    original_scheme = None
    for attempt in range(redirects + 1):
        normalized, host, port, addresses = validate_public_url(url, allow_restricted=allow_restricted)
        parts = urllib.parse.urlsplit(normalized)
        if original_host is None:
            original_host, original_scheme = host, parts.scheme
        if host != original_host or (original_scheme == "https" and parts.scheme != "https"):
            raise CollectorError("来源重定向到未获批准的域名或不安全连接，已阻止。")
        # Every request, including robots, is paced. This prevents concurrent
        # task scans from sending bursts to one origin. Honor longer robots
        # intervals after the robots policy is read.
        with _HOST_PACING_LOCK:
            delay = _HOST_MIN_INTERVAL.get(host, 1.0)
            wait = delay - (time.monotonic() - _HOST_LAST_REQUEST.get(host, 0))
            if wait > 0:
                time.sleep(min(wait, 10.0))
            _HOST_LAST_REQUEST[host] = time.monotonic()
        connection = _PinnedHTTPConnection(host, port, addresses, parts.scheme == "https", TIMEOUT)
        try:
            path = parts.path + (("?" + parts.query) if parts.query else "")
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            result_headers = {k.lower(): v for k, v in response.getheaders()}
            status = response.status
            if status in (301, 302, 303, 307, 308):
                if method != "GET" or attempt >= redirects or not result_headers.get("location"):
                    raise CollectorError("来源重定向无法安全处理。")
                url = urllib.parse.urljoin(normalized, result_headers["location"])
                redirected, redirected_host, _port, _addresses = validate_public_url(url, allow_restricted=allow_restricted)
                if redirected_host != original_host or (original_scheme == "https" and urllib.parse.urlsplit(redirected).scheme != "https"):
                    raise CollectorError("来源重定向到未获批准的域名或不安全连接，已阻止。")
                if redirect_check:
                    # Check permission for the new path before any request to it.
                    redirect_check(redirected)
                continue
            if status not in allow_status and not 200 <= status < 300:
                raise CollectorError(f"来源返回 HTTP {status}，请确认授权、限额和地址。")
            if result_headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise CollectorError("来源未提供未压缩响应，请使用公开 RSS 或标准 API。")
            length = result_headers.get("content-length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise CollectorError("来源响应超过配置的安全大小限制。")
            chunks = []
            total = 0
            deadline = time.monotonic() + TIMEOUT
            while total <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectorError("来源响应耗时超过安全限制。")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            content = b"".join(chunks)
            if len(content) > max_bytes:
                raise CollectorError("来源响应超过配置的安全大小限制。")
            return FetchResult(content, result_headers, normalized, status)
        except CollectorError:
            raise
        except (OSError, ValueError, http.client.HTTPException, ssl.SSLError):
            raise CollectorError("来源请求失败或超时；未保存采集结果。") from None
        finally:
            connection.close()
    raise CollectorError("来源重定向次数过多。")


def _decode(result: FetchResult) -> str:
    content_type = result.headers.get("content-type", "")
    match = re.search(r"charset=[\"']?([\w-]+)", content_type, flags=re.I)
    if not match:
        match = re.search(rb"(?:charset|encoding)\s*=\s*[\"']([\w-]+)", result.body[:4096], flags=re.I)
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", errors="replace")
    try:
        return result.body.decode(charset, errors="replace")
    except LookupError:
        return result.body.decode("utf-8", errors="replace")


def check_robots(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    result = safe_fetch(robots_url, allow_status=(404,), max_bytes=256 * 1024)
    if result.status == 404:
        return
    text = _decode(result)
    if "<html" in text.lower() or (text.strip() and not re.search(r"^\s*(user-agent|sitemap|#)", text, flags=re.I | re.M)):
        raise CollectorError("robots.txt 无法确认采集权限，已停止。")
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        raise CollectorError("来源 robots.txt 不允许本程序采集。")
    delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*")
    request_rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
    if request_rate:
        delay = max(delay or 0, request_rate.seconds / max(1, request_rate.requests))
    if delay and delay > 10:
        raise CollectorError("来源要求较长采集间隔；请改用经授权的 RSS / API。")
    if delay:
        host = (parsed.hostname or "").lower()
        with _HOST_PACING_LOCK:
            _HOST_MIN_INTERVAL[host] = max(1.0, float(delay))


_IGNORED_TAGS = {"script", "style", "nav", "footer", "header", "form", "noscript", "svg", "button", "iframe"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _PageParser(HTMLParser):
    def __init__(self, selector: str = ""):
        super().__init__(convert_charrefs=True)
        self.selector = selector.strip()
        if self.selector and not re.fullmatch(r"(?:[A-Za-z][\w-]*)?(?:[.#][\w-]+)?", self.selector):
            raise CollectorError("正文选择器仅支持 main、article、#id、.class 或 tag.class。")
        self.stack = []
        self.parts = []
        self.title_parts = []
        self.matches = 0
        self.has_password = False

    def _matches(self, tag: str, attrs: dict) -> bool:
        selector = self.selector
        if not selector:
            return tag in ("main", "article")
        match = re.fullmatch(r"([A-Za-z][\w-]*)?([.#])?([\w-]+)?", selector)
        wanted_tag, mode, name = match.groups()
        if wanted_tag and tag != wanted_tag.lower():
            return False
        if mode == "#":
            return attrs.get("id") == name
        if mode == ".":
            return name in attrs.get("class", "").split()
        return tag == selector.lower()

    def handle_starttag(self, tag: str, attributes: list):
        attrs = dict(attributes)
        ignored = tag in _IGNORED_TAGS or "hidden" in attrs or attrs.get("aria-hidden") == "true"
        ignored = ignored or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I))
        if tag == "input" and attrs.get("type", "").lower() == "password":
            self.has_password = True
        active = self._matches(tag, attrs)
        if active:
            self.matches += 1
        if tag in _VOID_TAGS:
            if tag in {"br", "hr"}:
                self.parts.append("\n")
            return
        self.stack.append((tag, ignored, active))
        if tag in ("p", "div", "section", "article", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in ("p", "div", "section", "article", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data: str):
        if any(t[0] == "title" for t in self.stack):
            self.title_parts.append(data)
        if any(t[1] for t in self.stack):
            return
        if self.selector and not any(t[2] for t in self.stack):
            return
        self.parts.append(data)


def _clean_text(value: str) -> str:
    value = unescape(value).replace("\x00", "")
    return "\n".join(re.sub(r"[\t \r\f\v]+", " ", line).strip() for line in value.splitlines() if line.strip())[:50000]


def html_to_text(value: str) -> str:
    parser = _PageParser()
    parser.feed(value)
    return _clean_text("".join(parser.parts))


def _brave_query(task: dict) -> str:
    """Match any configured region and any keyword, as the local rules do.

    Brave documents uppercase OR / AND and quoted exact phrases at
    https://api-dashboard.search.brave.com/documentation/resources/search-operators
    Operator support is experimental; the local matcher remains authoritative.
    """
    from .core import SENSITIVE, redact
    groups = []
    for name in ("regions", "keywords"):
        terms = task.get(name) or []
        if not isinstance(terms, (list, tuple)):
            raise CollectorError("搜索地区和关键词必须分别填写为列表。")
        quoted = []
        for term in terms:
            if not isinstance(term, str):
                raise CollectorError("搜索地区和关键词必须是文本。")
            # Keep operators inside a phrase. Embedded quotes and backslashes
            # cannot close or escape the surrounding quote added here.
            phrase = re.sub(r"\s+", " ", term.replace('"', '').replace('\\', ' ')).strip()
            if not phrase:
                raise CollectorError("搜索地区或关键词为空，请删除空项。")
            if SENSITIVE.search(phrase) or redact(phrase) != phrase:
                raise CollectorError("搜索只支持普通地区和需求词，请勿输入个人联系方式、病史或其他敏感信息。")
            quoted.append('"' + phrase + '"')
        if not quoted:
            raise CollectorError("Brave 搜索需要至少一个地区和一个需求关键词。")
        groups.append("(" + " OR ".join(quoted) + ")" if len(quoted) > 1 else quoted[0])
    query = " AND ".join(groups)
    if len(query) > 600 or len(query.split()) > 75:
        raise CollectorError("Brave 搜索查询超过 600 字符 / 75 个词；请减少地区或关键词后重试。")
    return query


def _tavily_query(task: dict) -> str:
    """Send ordinary demand words only, never contact or health targeting.

    Tavily accepts natural-language queries. The configured lists are expressed
    as alternatives, with no unsupported search-operator promises. Local rules
    still require the actual region, keyword and an invitation to reply.
    """
    from .core import SENSITIVE, redact
    groups = []
    for name in ("regions", "keywords"):
        terms = task.get(name) or []
        if not isinstance(terms, (list, tuple)) or not terms:
            raise CollectorError("公开搜索需要至少一个地区和一个需求关键词。")
        cleaned = []
        for term in terms:
            if not isinstance(term, str):
                raise CollectorError("搜索地区和关键词必须是文本。")
            phrase = re.sub(r"\s+", " ", term).strip()
            if not phrase or SENSITIVE.search(phrase) or redact(phrase) != phrase:
                raise CollectorError("搜索只支持普通地区和需求词，请勿输入个人联系方式、病史或其他敏感信息。")
            cleaned.append(phrase)
        groups.append("、".join(cleaned))
    query = (f"查找地区为以下任一地区的公开需求：{groups[0]}。"
             f"内容含以下任一关键词：{groups[1]}。"
             "仅找本人主动求推荐、请提供方案或明确邀请咨询回复的内容；排除广告、新闻、病史、个人健康或保单信息。")
    # This is the application's conservative limit, not an asserted API limit.
    if len(query) > 600:
        raise CollectorError("公开搜索查询超过本机 600 字限制；请减少地区或关键词后重试。")
    return query


def _rss_items(result: FetchResult, source: dict) -> list[dict]:
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", result.body, re.I):
        raise CollectorError("RSS 含不支持的 XML 声明，已停止解析。")
    try:
        root = ET.fromstring(result.body)
    except (ET.ParseError, ValueError):
        raise CollectorError("来源不是有效 RSS / Atom；可能返回了登录或验证页面。") from None
    local_name = lambda node: node.tag.rsplit("}", 1)[-1].lower()
    if local_name(root) not in ("rss", "feed", "rdf"):
        raise CollectorError("来源不是支持的 RSS / Atom 订阅。")
    items = []
    for entry in root.iter():
        if local_name(entry) not in ("item", "entry"):
            continue
        values = {}
        links = []
        for node in entry:
            name = local_name(node)
            content = "".join(node.itertext()).strip()
            if name == "link":
                if node.attrib.get("rel", "alternate") == "alternate":
                    links.append(node.attrib.get("href") or content)
            elif name == "author":
                child_name = next(("".join(c.itertext()) for c in node if local_name(c) == "name"), content)
                values[name] = child_name
            else:
                values[name] = content
        link = next((link for link in links if link), "")
        # Feed item must have its own traceable original URL, no fabricated fallback.
        if not link:
            continue
        title = html_to_text(values.get("title", ""))
        text = html_to_text(values.get("encoded") or values.get("content") or values.get("description") or values.get("summary") or "")
        if not text:
            text = title
        items.append({"title": title[:1000], "text": text, "url": urllib.parse.urljoin(result.url, link),
                      "published_at": values.get("pubdate") or values.get("published") or values.get("updated") or "",
                      "author": values.get("author") or values.get("creator") or ""})
        if len(items) >= MAX_ITEMS:
            break
    return items


class CollectionBatch(list):
    def __init__(self, items, queries, errors):
        super().__init__(items)
        self.queries = queries
        self.errors = errors
        self.channels = []


def search_domains(task):
    domains = task.get('search_domains', ['tieba.baidu.com', 'zhidao.baidu.com', 'xiaomayi.net'])
    if not isinstance(domains, list) or len(domains) > 12:
        raise CollectorError('定向搜索网站最多填写 12 个域名。')
    output = []
    for domain in domains:
        if not isinstance(domain, str) or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?', domain):
            raise CollectorError('请填写网站域名，不含网址路径、密钥或端口。')
        domain = domain.lower()
        validate_public_url('https://' + domain + '/', reference_only=True)
        if _restricted(domain):
            raise CollectorError('该平台需适用授权接口，不能作为普通定向网页来源。')
        output.append(domain)
    return list(dict.fromkeys(output))


def tavily_query_plan(task):
    _tavily_query(task)  # Validate every configured term before any network call.
    budget = int(task.get('search_budget', 1))
    if budget not in (1, 3, 6, 12):
        raise CollectorError('每次搜索组数只能选择 1、3、6 或 12。')
    if budget == 1:
        return [_tavily_query(task)]
    if task.get('lead_mode') == 'business':
        # B端商机：短关键词组合，Tavily 对自然语言长句与 OR 语法会退化成泛泛结果。
        return _business_query_plan(task, budget)
    insurance = '保险' in str(task.get('industry', '')) or any('保险' in k for k in task['keywords'])
    intents = (['想买保险 求推荐', '家庭保险 求保障方案', '投保 找保险顾问', '孩子保险 请推荐', '保险配置 求助', '保险咨询 请提供方案']
               if insurance else ['准备装修 求推荐', '装修 求报价', '装修 找师傅', '我家装修 求助', '装修 请提供方案', '装修 找设计师'])
    if task.get('industry') not in ('保险', '装修'):
        intents = [k + ' 求推荐 求助' for k in task['keywords']]
    regions = list(dict.fromkeys(task['regions']))
    # Distribute all configured regions between groups; groups are alternatives.
    count = min(budget, max(1, len(regions)))
    groups = [regions[i::count] for i in range(count)]
    return [f'地区为以下任一地区：{"、".join(groups[i % count])}。寻找本人发帖：{intents[i % len(intents)]}。排除公司推广、新闻、报价目录。'
            for i in range(budget)]


def _business_query_plan(task, budget):
    """B端商机：每组一个地区词 + 一个关键词 + 公告短语（连写），短查询不做 OR 组合。"""
    intents = ['招标公告', '采购公告', '比选公告', '承保公告', '中标公示', '遴选公告',
               '磋商公告', '询价公告', '征集公告', '招募公告', '成交公告', '结果公示']
    regions = list(dict.fromkeys(task['regions'])) or ['']
    return [f'{regions[i % len(regions)]} {task["keywords"][i % len(task["keywords"])]} {intents[i % len(intents)]}'.strip()
            for i in range(budget)]


def bing_query_plan(task):
    """必应国内版 RSS：地区分组与求助意图两两组合，每组一条关键词查询。"""
    _tavily_query(task)  # 复用地区、关键词与排除词的填写校验。
    budget = int(task.get('search_budget', 1))
    if budget not in (1, 3, 6, 12):
        raise CollectorError('每次搜索组数只能选择 1、3、6 或 12。')
    if task.get('lead_mode') == 'business':
        # B端商机：公告语气词而非求助语气词；必应 RSS 对 OR 组合同样会退化。
        return _business_query_plan(task, budget)
    insurance = '保险' in str(task.get('industry', '')) or any('保险' in k for k in task['keywords'])
    intents = (['想买保险 求推荐', '家庭保险 求保障方案', '投保 找保险顾问', '孩子保险 请推荐', '保险配置 求助', '保险咨询 请提供方案']
               if insurance else ['准备装修 求推荐', '装修 求报价', '装修 找师傅', '我家装修 求助', '装修 请提供方案', '装修 找设计师'])
    if task.get('industry') not in ('保险', '装修'):
        intents = [k + ' 求推荐 求助' for k in task['keywords']]
    regions = list(dict.fromkeys(task['regions']))
    count = min(budget, max(1, len(regions)))
    groups = [regions[i::count] for i in range(count)]
    plans = []
    for i in range(budget):
        region_or = ' OR '.join(groups[i % count])
        plans.append(f'({region_or}) {intents[i % len(intents)]}' if ' OR ' in region_or
                     else f'{region_or} {intents[i % len(intents)]}')
    return plans


def collect(source: dict, task: dict) -> list[dict]:
    kind = source.get('kind')
    if kind not in ('tavily_search', 'bing_rss', 'volc_search', 'baidu_search'):
        return _collect_once(source, task)
    items, seen, attempted, errors = [], set(), [], []
    domains = search_domains(task) if task.get('domestic_search', False) else []
    channels = []
    plan = bing_query_plan(task) if kind in ('bing_rss', 'volc_search', 'baidu_search') else tavily_query_plan(task)
    for index, query in enumerate(plan):
        attempted.append(query)
        targeted = [domains[(index // 2) % len(domains)]] if domains and index % 2 else []
        search_task = dict(task, _search_domains=targeted)
        if targeted:
            channel = targeted[0]
        elif kind == 'bing_rss':
            channel = '必应国内网页'
        elif kind == 'volc_search':
            channel = '火山方舟联网搜索'
        elif kind == 'baidu_search':
            channel = '百度AI搜索'
        else:
            channel = '国内网页优先' if task.get('domestic_search') else '公开网页'
        try:
            batch = _collect_once(source, search_task, query)
        except CollectorError as exc:
            if not items:
                raise
            errors.append(str(exc))
            channels.append(dict(name=channel, query=query, status='failed', returned=0, error=str(exc)))
            break  # Never retry or continue spending after a provider failure.
        for item in batch:
            key = (item['url'], item['text'])
            if key not in seen:
                seen.add(key)
                items.append(item)
        channels.append(dict(name=channel, query=query, status='success', returned=len(batch)))
    result = CollectionBatch(items, attempted, errors)
    result.channels = channels
    return result


def _collect_once(source: dict, task: dict, query=None) -> list[dict]:
    if source.get("permission_confirmed") is not True:
        raise CollectorError("该来源尚未确认采集授权；请先确认站点条款和公开访问权限。")
    kind = source.get("kind")
    if kind not in ("rss", "web") + API_KINDS:
        raise CollectorError("不支持的来源类型。")
    url = str(source.get("url", ""))
    substitutions = {"query": " ".join(task.get("keywords") or []), "region": " ".join(task.get("regions") or [])}
    if kind == "brave_search":
        if source.get("storage_authorized") is not True or len(str(source.get("storage_authorization_note") or "").strip()) < 10:
            raise CollectorError("Brave 普通套餐不允许保存搜索结果。需另获书面存储许可并填写许可依据后才能采集。")
        endpoint = "https://api.search.brave.com/res/v1/web/search"
        if url and url.rstrip("/") != endpoint:
            raise CollectorError("Brave 搜索使用固定官方 API 地址，请勿填写其他服务地址。")
        query = _brave_query(task)
        url = endpoint + "?" + urllib.parse.urlencode({"q": query, "count": 20,
               "text_decorations": "false", "result_filter": "web", "search_lang": "zh-hans"})
    request_body = None
    if kind == "tavily_search":
        endpoint = "https://api.tavily.com/search"
        if url and url.rstrip("/") != endpoint:
            raise CollectorError("Tavily 搜索使用固定官方 API 地址，请勿填写其他服务地址。")
        url = endpoint
        options = {"query": query or _tavily_query(task), "search_depth": "basic",
                "max_results": 20, "topic": "general", "auto_parameters": False,
                "include_answer": False, "include_raw_content": False, "include_images": False,
                "include_published_date": False, "exclude_domains": list(RESTRICTED_DOMAINS)}
        if task.get('domestic_search'):
            options['country'] = 'china'
        if task.get('_search_domains'):
            options.update(include_domains=task['_search_domains'], include_domains_mode='restrict')
        request_body = json.dumps(options, ensure_ascii=False).encode('utf-8')
    if kind == "bing_rss":
        endpoint = "https://cn.bing.com/search"
        if url and url.rstrip("/") != endpoint:
            raise CollectorError("必应国内版公开搜索使用固定官方地址，请勿填写其他服务地址。")
        text_query = query or " ".join(task.get("keywords") or [])
        if task.get("_search_domains"):
            text_query = f"site:{task['_search_domains'][0]} {text_query}"
        url = endpoint + "?" + urllib.parse.urlencode(
            {"q": text_query, "format": "rss", "count": 30, "mkt": "zh-CN", "setlang": "zh-hans"})
    if kind == "volc_search":
        endpoint = "https://ark.cn-beijing.volces.com/api/v3/responses"
        if url and url.rstrip("/") != endpoint:
            raise CollectorError("火山方舟联网搜索使用固定官方 API 地址，请勿填写其他服务地址。")
        url = endpoint
        model_endpoint = str(source.get("model_endpoint") or "")
        if not re.fullmatch(r"ep-[A-Za-z0-9-]{3,64}", model_endpoint):
            raise CollectorError("请在该来源中填写火山方舟接入点 ID（形如 ep-20260622001208-xxxxx）。")
        text_query = query or " ".join(task.get("keywords") or [])
        if task.get("_search_domains"):
            text_query = f"site:{task['_search_domains'][0]} {text_query}"
        request_body = json.dumps({"model": model_endpoint, "input": text_query,
                                   "tools": [{"type": "web_search"}]}, ensure_ascii=False).encode("utf-8")
    if kind == "baidu_search":
        endpoint = "https://qianfan.baidubce.com/v2/ai_search/web_search"
        if url and url.rstrip("/") != endpoint:
            raise CollectorError("百度AI搜索使用固定官方 API 地址，请勿填写其他服务地址。")
        url = endpoint
        text_query = query or " ".join(task.get("keywords") or [])
        if task.get("_search_domains"):
            text_query = f"site:{task['_search_domains'][0]} {text_query}"
        # 百度千帆 v2 接口只认 messages 格式；query 字段会被判为空请求。
        # 时间窗取一年：本地公告类内容更新慢，month 几乎搜不到泸州级结果。
        request_body = json.dumps({"messages": [{"role": "user", "content": text_query}],
                                   "search_recency_filter": "year",
                                   "search_source": "baidu_search"}, ensure_ascii=False).encode("utf-8")
    if kind == "json_api":
        unknown = set(re.findall(r"\{([^}]+)\}", url)) - set(substitutions)
        if unknown:
            raise CollectorError("API URL 只支持 {query} 和 {region} 占位符。")
        for name, value in substitutions.items():
            url = url.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
    allow_platform_api = kind in API_KINDS and source.get("api_authorized") is True
    if kind in API_KINDS and not allow_platform_api:
        raise CollectorError("请先确认该 API 的合法调用授权和使用范围。")
    normalized, _host, _port, _addresses = validate_public_url(url, allow_restricted=allow_platform_api,
                                                            reference_only=kind in ("tavily_search", "bing_rss", "volc_search", "baidu_search"))
    headers = {}
    if kind not in API_KINDS:
        check_robots(normalized)
    else:
        env_name = source.get("api_token_env", "")
        token = source.get("_api_token") or ""
        if token and (not isinstance(token, str) or len(token) > 4096 or "\n" in token or "\r" in token):
            raise CollectorError("本机 API 凭证格式无效。")
        if not token and env_name:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", str(env_name)):
                raise CollectorError("API 凭证环境变量名格式错误。")
            token = os.environ.get(env_name, "")
            if not token:
                raise CollectorError("尚未在本机配置该 API 的凭证环境变量。")
        if kind in SEARCH_KINDS and kind != "bing_rss" and not token:
            raise CollectorError("公开搜索待连接：请在本机设置 API 密钥或其环境变量。")
        if token and (not isinstance(token, str) or len(token) > 4096 or "\n" in token or "\r" in token):
            raise CollectorError("本机 API 凭证格式无效。")
        if token:
            if kind == "brave_search":
                headers["X-Subscription-Token"] = token
            else:
                headers["Authorization"] = "Bearer " + token
        if kind == "bing_rss":
            headers["Accept"] = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8"
        else:
            headers["Accept"] = "application/json"
        if kind in ("tavily_search", "volc_search", "baidu_search"):
            headers["Content-Type"] = "application/json"
    if kind == "tavily_search":
        result = official_tavily_fetch(normalized, method="POST", headers=headers, body=request_body)
    elif kind == "volc_search":
        result = official_volc_fetch(normalized, headers=headers, body=request_body)
    elif kind == "baidu_search":
        result = official_baidu_fetch(normalized, headers=headers, body=request_body)
    elif kind == "bing_rss":
        result = official_bing_fetch(normalized, headers=headers)
    else:
        result = safe_fetch(normalized, headers=headers, allow_restricted=allow_platform_api,
                            redirect_check=check_robots if kind in ("web", "rss") else None)
    if kind == "rss":
        raw_items = _rss_items(result, source)
    elif kind == "bing_rss":
        raw_items = _rss_items(result, source)
    elif kind == "web":
        content_type = result.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type and "text/plain" not in content_type:
            raise CollectorError("网页来源未返回 HTML / 文本内容。")
        text = _decode(result)
        parser = _PageParser(str(source.get("body_selector") or ""))
        parser.feed(text)
        title = _clean_text("".join(parser.title_parts))
        if parser.has_password or re.search(r"^(?:请登录|登录|登陆|安全验证|人机验证|访问验证|验证|sign in|log in|captcha|access denied)(?:\s|[-|_·]|$)", title, re.I):
            raise CollectorError("来源要求登录或验证，程序已停止且不会绕过访问控制。")
        if parser.selector and not parser.matches:
            raise CollectorError("未找到所配置的正文选择器，请检查来源页面。")
        if not parser.selector and parser.matches:
            # Restrict extraction to the page's article/main when present.
            main = _PageParser("main")
            main.feed(text)
            if not main.matches:
                main = _PageParser("article")
                main.feed(text)
            parser = main
        body_text = _clean_text("".join(parser.parts))
        if len(body_text) < 10:
            raise CollectorError("没有可采集正文；页面可能需要 JavaScript 或授权。")
        raw_items = [{"title": title[:1000], "text": body_text, "url": result.url, "published_at": "", "author": ""}]
    else:
        try:
            payload = json.loads(_decode(result))
        except (ValueError, TypeError):
            raise CollectorError("授权 API 未返回有效 JSON。") from None
        if kind == "brave_search":
            if not isinstance(payload, dict) or payload.get("type") == "ErrorResponse":
                raise CollectorError("Brave 搜索 API 请求未成功，请检查本机凭证和调用限额。")
            web = payload.get("web") or {}
            if not isinstance(web, dict) or not isinstance(web.get("results", []), list):
                raise CollectorError("Brave 搜索 API 响应格式异常。")
            raw_items = []
            for result_item in web.get("results", [])[:20]:
                if not isinstance(result_item, dict) or not isinstance(result_item.get("description"), str):
                    continue
                raw_items.append({"title": html_to_text(str(result_item.get("title") or "")),
                                  "text": html_to_text(result_item["description"]),
                                  "url": result_item.get("url", ""),
                                  # Search index page_age may be a modification
                                  # estimate. Do not invent an original publish date.
                                  "published_at": "", "author": ""})
        elif kind == "tavily_search":
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or payload.get("error") or payload.get("detail"):
                raise CollectorError("Tavily 搜索 API 请求未成功或格式异常，请检查本机凭证和调用限额。")
            # Only source snippets, never an LLM answer or a fetched page body.
            raw_items = []
            from .core import SENSITIVE
            for result_item in payload["results"][:20]:
                if not isinstance(result_item, dict) or not isinstance(result_item.get("content"), str):
                    continue
                title = html_to_text(str(result_item.get("title") or ""))
                snippet = html_to_text(result_item["content"])
                if SENSITIVE.search(title + "\n" + snippet):
                    continue
                raw_items.append({"title": title, "text": snippet,
                                  "url": result_item.get("url", ""), "published_at": "", "author": ""})
        elif kind == "volc_search":
            raw_items = _volc_items(payload)
        elif kind == "baidu_search":
            raw_items = _baidu_items(payload)
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise CollectorError("API 必须返回标准格式：{items:[{title,text,url,...}]}。")
            raw_items = payload["items"][:MAX_ITEMS]
    output = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item.get("text", "").strip():
            continue
        if not isinstance(item.get("url"), str) or not item.get("url"):
            continue
        try:
            item_url, _item_host, _port, _addr = validate_public_url(urllib.parse.urljoin(result.url, item["url"]),
                            allow_restricted=allow_platform_api, reference_only=kind in ("tavily_search", "bing_rss", "volc_search", "baidu_search"))
        except CollectorError:
            continue
        if kind in ("tavily_search", "bing_rss", "volc_search", "baidu_search") and _restricted(_item_host):
            continue
        if kind == 'tavily_search' and task.get('_search_domains') and not any(
                _item_host == domain or _item_host.endswith('.' + domain) for domain in task['_search_domains']):
            continue
        identity = (item_url, item["text"])
        if identity in seen:
            continue
        seen.add(identity)
        output.append({"title": str(item.get("title") or "")[:1000], "text": _clean_text(item["text"]),
                       "url": item_url, "platform": _item_host if kind in SEARCH_KINDS else str(source.get("platform") or source.get("name") or "公开来源"),
                       "source_type": "搜索摘要（需人工核实原文）" if kind in SEARCH_KINDS else str(source.get("source_type") or kind),
                       "published_at": str(item.get("published_at") or "")[:200],
                       "author": str(item.get("author") or "")[:1000]})
    return output
