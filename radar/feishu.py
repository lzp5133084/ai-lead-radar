"""Feishu Bitable connector using the official HTTPS API and stdlib only.

Official endpoints and request placement verified against larksuite's generated
Python SDK: create_app_table_record_request (client_token query parameter),
search_app_table_record_request (page_size query; filter in body), and
list_app_table_field_request. Secrets are never included in exception messages.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import uuid

from .collectors import CollectorError, safe_fetch

BASE_URL = "https://open.feishu.cn/open-apis/"
FIELD_NAMES = ["线索编号", "行业", "地区", "来源平台", "来源类型", "标题", "原文", "原始链接",
               "采集时间", "发布时间", "意向分", "判断依据", "公开联系方式", "联系方式来源",
               "跟进话术", "审核状态", "跟进状态"]
FIELD_TYPES = {name: (2 if name == "意向分" else 1) for name in FIELD_NAMES}


class FeishuError(RuntimeError):
    """Safe credential-free synchronization failure."""


class FeishuClient:
    def __init__(self, config: dict):
        self.config = dict(config)
        self._token = str(config.get("tenant_access_token") or "")
        self._token_until = float("inf") if self._token else 0
        self._lock = threading.RLock()
        self._schema_ok = False
        for name in ("app_token", "table_id"):
            value = str(config.get(name) or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", value):
                raise FeishuError("请先在本机配置有效的飞书 app_token 和 table_id。")
        if not self._token and not (config.get("app_id") and config.get("app_secret")):
            raise FeishuError("飞书凭证待配置：需要 App ID + App Secret，或 tenant_access_token。")
        self.table_path = ("bitable/v1/apps/" + urllib.parse.quote(str(config["app_token"]), safe="")
                           + "/tables/" + urllib.parse.quote(str(config["table_id"]), safe=""))

    def _raw_request(self, method: str, path: str, body=None, token="") -> dict:
        # Endpoint cannot be supplied by configuration; fixed official host only.
        headers = {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            response = safe_fetch(BASE_URL + path, method=method, headers=headers, body=payload, redirects=0)
            data = json.loads(response.body.decode("utf-8"))
        except (CollectorError, ValueError, TypeError):
            raise FeishuError("飞书请求失败：请检查本机网络、API 权限和配置；凭证未输出。") from None
        if not isinstance(data, dict):
            raise FeishuError("飞书返回了无法识别的响应。")
        code = data.get("code")
        if code != 0:
            # Never echo the remote 'msg': some services include request values.
            safe_code = code if isinstance(code, int) else "unknown"
            if safe_code in (99991661, 99991663, 99991664, 99991668):
                hint = "凭证无效或过期，请更新本机配置"
            elif safe_code in (99991672, 99991679, 1254302, 1254303):
                hint = "应用缺少权限，或未获该多维表格授权"
            elif safe_code in (1254000, 1254043, 1254044):
                hint = "请检查表格标识、字段名称和字段类型"
            else:
                hint = "请检查授权、字段设置和平台调用限额"
            raise FeishuError(f"飞书 API 错误（{safe_code}）：{hint}。")
        return data

    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            data = self._raw_request("POST", "auth/v3/tenant_access_token/internal",
                                     {"app_id": self.config.get("app_id"), "app_secret": self.config.get("app_secret")})
            token = data.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise FeishuError("飞书未返回应用访问凭证，请检查应用配置。")
            self._token = token
            expiry = data.get("expire", 7200)
            expiry = expiry if isinstance(expiry, (int, float)) else 7200
            self._token_until = time.monotonic() + max(0, expiry - 120)
            return token

    def _request(self, method: str, path: str, body=None) -> dict:
        return self._raw_request(method, path, body, self._get_token()).get("data", {})

    def check(self) -> dict:
        fields = []
        page_token = ""
        seen = set()
        for _page in range(20):
            query = {"page_size": 100}
            if page_token:
                query["page_token"] = page_token
            data = self._request("GET", self.table_path + "/fields?" + urllib.parse.urlencode(query))
            items = data.get("items", [])
            if not isinstance(items, list):
                raise FeishuError("飞书字段接口返回格式异常。")
            for item in items:
                if isinstance(item, dict):
                    fields.append({"field_name": item.get("field_name", ""), "type": item.get("type")})
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token or page_token in seen:
                raise FeishuError("飞书字段分页无法完成，请稍后重试。")
            seen.add(page_token)
        else:
            raise FeishuError("飞书字段数量超过本程序验证范围。")
        available = {field["field_name"]: field["type"] for field in fields}
        missing = [name for name in FIELD_NAMES if name not in available]
        wrong = [{"field_name": name, "expected": expected, "actual": available[name]}
                 for name, expected in FIELD_TYPES.items() if name in available and available[name] != expected]
        self._schema_ok = not missing and not wrong
        return {"ok": self._schema_ok, "fields": fields, "missing_fields": missing, "type_errors": wrong}

    @staticmethod
    def lead_fields(lead: dict) -> dict:
        def value(*names):
            result = next((lead[name] for name in names if lead.get(name) is not None), "")
            if isinstance(result, (list, tuple)):
                return "；".join(str(part) for part in result)
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        try:
            score = float(lead.get("score") or 0)
        except (TypeError, ValueError):
            raise FeishuError("线索意向分必须是数字。") from None
        if not 0 <= score <= 100:
            raise FeishuError("线索意向分必须在 0–100 之间。")
        return {"线索编号": value("id"), "行业": value("industry"), "地区": value("region"),
                "来源平台": value("platform"), "来源类型": value("source_type"), "标题": value("title"),
                "原文": value("text")[:50000], "原始链接": value("url"), "采集时间": value("collected_at"),
                "发布时间": value("published_at"), "意向分": score, "判断依据": value("reasons"),
                "公开联系方式": value("contact", "contacts"), "联系方式来源": value("contact_source"),
                "跟进话术": value("draft"), "审核状态": value("review_status"),
                "跟进状态": value("status", "followup_status")}

    def upsert(self, lead: dict) -> str:
        # Parent also checks this, but direct use must not bypass human review.
        if lead.get("review_status") != "approved":
            raise FeishuError("只允许同步已通过人工审核的线索。")
        search_summary = lead.get("source_kind") in ("brave_search", "tavily_search") or "搜索摘要" in str(lead.get("source_type") or "")
        if search_summary and not lead.get("followup_permission_confirmed"):
            raise FeishuError("搜索摘要需先核实原文邀请回复及内容使用许可，再同步。")
        lead_id = str(lead.get("id") or "")
        if not lead_id or len(lead_id) > 200:
            raise FeishuError("线索缺少稳定编号，无法安全同步。")
        with self._lock:
            if not self._schema_ok:
                schema = self.check()
                if not schema["ok"]:
                    raise FeishuError("飞书字段不完整或类型不符，请按说明创建字段后再同步。")
            fields = self.lead_fields(lead)
            search_body = {"field_names": ["线索编号"], "filter": {"conjunction": "and", "conditions": [
                {"field_name": "线索编号", "operator": "is", "value": [lead_id]}]}}
            existing = self._request("POST", self.table_path + "/records/search?page_size=2", search_body)
            records = existing.get("items", [])
            if not isinstance(records, list):
                raise FeishuError("飞书查重接口响应异常。")
            if len(records) > 1 or existing.get("has_more"):
                raise FeishuError("飞书已有重复线索编号，请人工合并后再同步。")
            if records:
                record_id = str(records[0].get("record_id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", record_id):
                    raise FeishuError("飞书未返回有效记录编号。")
                updated = self._request("PUT", self.table_path + "/records/" + record_id, {"fields": fields})
                returned = str((updated.get("record") or {}).get("record_id") or record_id)
            else:
                stable_key = str(uuid.uuid5(uuid.NAMESPACE_URL, "lead-radar:" + str(self.config["app_token"]) + ":" + str(self.config["table_id"]) + ":" + lead_id))
                created = self._request("POST", self.table_path + "/records?" + urllib.parse.urlencode({"client_token": stable_key}), {"fields": fields})
                returned = str((created.get("record") or {}).get("record_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", returned):
                raise FeishuError("飞书未确认写入成功，保留本地线索供重试。")
            return returned
