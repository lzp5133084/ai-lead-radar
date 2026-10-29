"""Task orchestration, scheduling, manual review and approved-only Feishu sync."""
import re
import os
import ipaddress
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from .core import Database, analyze, now
from .security import Config


def terms(value, label):
    if isinstance(value, str):
        value = re.split(r'[,，;；\n]', value)
    if not isinstance(value, list):
        raise ValueError(label + '需要为列表')
    vals = list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))
    if len(vals) > 40 or any(len(v) > 60 for v in vals):
        raise ValueError(label + '最多 40 项，每项不超过 60 字')
    return vals


class Radar:
    def __init__(self, data_dir, lan=False, collector=None, feishu_factory=None):
        self.data_dir = Path(data_dir)
        self.db = Database(self.data_dir / 'radar.sqlite3')
        self.config = Config(data_dir)
        self.lan = lan
        self.collector = collector
        self.feishu_factory = feishu_factory
        self.task_lock = threading.RLock()
        self.source_lock = threading.RLock()
        self.active = set()
        self.capacity = threading.BoundedSemaphore(2)
        self.sync_lock = threading.Lock()
        self.lead_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.lan_urls = []
        self.service_started_at = now()
        from .workspace import Workspace
        self.workspace = Workspace(self)
        # A restart must expose interrupted runs instead of claiming completion.
        for run in self.db.all('runs'):
            if run['status'] == 'running':
                run.update(status='failed', finished_at=now(), message='上次服务中断，本次未确认完成')
                self.db.save('runs', run)

    def log(self, message, status='failed', errors=None):
        return self.db.save('runs', dict(task_id=0, task_name='系统 / 飞书', started_at=now(),
                 finished_at=now(), status=status, fetched=0, accepted=0, duplicates=0,
                 errors=errors or [], message=message))

    def safe_error(self, exc):
        msg = str(exc)[:700]
        try:
            conf = self.config.feishu()
            for key in ('app_secret', 'tenant_access_token', 'app_token', 'table_id'):
                if conf.get(key):
                    msg = msg.replace(conf[key], '[凭证已隐藏]')
        except Exception:
            pass
        return re.sub(r'(Bearer\s+|(?:token|secret|key|password|authorization)\s*[=:]\s*)[^\s&,]+',
                      r'\1[已隐藏]', msg, flags=re.I)

    def save_source(self, given, uid=None):
        with self.source_lock:
            return self._save_source(given, uid)

    def _save_source(self, given, uid=None):
        old = self.db.get('sources', uid) if uid else {}
        if not uid and len(self.db.all('sources')) >= 50:
            raise ValueError('本地版本最多配置 50 个数据源')
        allowed = ('name', 'kind', 'url', 'platform', 'source_type', 'permission_confirmed',
                   'contact_consent', 'body_selector', 'api_token_env', 'api_authorized',
                   'storage_authorized', 'storage_authorization_note', 'model_endpoint')
        data = dict(old)
        data.update({k: given[k] for k in allowed if k in given})
        for key in ('name', 'url', 'platform', 'source_type'):
            data[key] = str(data.get(key) or '').strip()
        if not data['name'] or len(data['name']) > 100:
            raise ValueError('数据源名称需要 1–100 字')
        if data.get('kind') not in ('rss', 'web', 'json_api', 'brave_search', 'tavily_search', 'bing_rss', 'volc_search', 'baidu_search'):
            raise ValueError('请选择 RSS、获准网页或授权搜索/API')
        if data.get('permission_confirmed') is not True:
            raise ValueError('请先确认拥有合法访问和使用该来源内容的权限')
        if data['kind'] in ('json_api', 'brave_search', 'tavily_search', 'bing_rss', 'volc_search', 'baidu_search') and data.get('api_authorized') is not True:
            raise ValueError('API 需要确认已获接口授权')
        kind_changed = bool(old and old.get('kind') != data['kind'])
        if kind_changed and data['kind'] in ('json_api', 'brave_search', 'tavily_search', 'volc_search', 'baidu_search'):
            if not given.get('api_token') and not given.get('api_token_env'):
                raise ValueError('更换 API 服务时，请重新填写该服务的密钥或专用环境变量名')
            data['api_token_env'] = given.get('api_token_env') or ''
        data['storage_authorized'] = data.get('storage_authorized') is True
        data['storage_authorization_note'] = str(data.get('storage_authorization_note') or '').strip()
        if data['kind'] == 'brave_search':
            if not data['storage_authorized'] or not 10 <= len(data['storage_authorization_note']) <= 500:
                raise ValueError('Brave 普通套餐不允许保存结果。请另获书面存储许可并填写 10–500 字许可依据后使用')
            data['url'] = 'https://api.search.brave.com/res/v1/web/search'
        if data['kind'] == 'tavily_search':
            data['url'] = 'https://api.tavily.com/search'
        if data['kind'] == 'bing_rss':
            data['url'] = 'https://cn.bing.com/search'
            data['api_token_env'] = ''
        if data['kind'] == 'volc_search':
            data['url'] = 'https://ark.cn-beijing.volces.com/api/v3/responses'
            data['model_endpoint'] = str(data.get('model_endpoint') or '').strip()
            if not re.fullmatch(r'ep-[A-Za-z0-9-]{3,64}', data['model_endpoint']):
                raise ValueError('请填写火山方舟接入点 ID（形如 ep-20260622001208-xxxxx）')
        else:
            data['model_endpoint'] = ''
        if data['kind'] == 'baidu_search':
            data['url'] = 'https://qianfan.baidubce.com/v2/ai_search/web_search'
        if data['kind'] in ('brave_search', 'tavily_search', 'bing_rss', 'volc_search', 'baidu_search'):
            data['contact_consent'] = False
            data['source_type'] = '搜索摘要（需人工核实原文）'
        if len(data['url']) > 2000:
            raise ValueError('来源 URL 太长')
        p = urlsplit(data['url'])
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError('请输入公开 HTTP/HTTPS URL，凭证不能写入 URL')
        if any(k.lower() in ('token', 'access_token', 'api_key', 'apikey', 'key', 'secret', 'password', 'app_secret',
                              'authorization', 'tenant_access_token', 'client_secret', 'x-subscription-token')
               for k, _ in parse_qsl(p.query, keep_blank_values=True)):
            raise ValueError('URL 不能包含凭证，请使用 API 凭证环境变量')
        if p.port and p.port not in (80, 443):
            raise ValueError('只允许公开网站的标准 80 / 443 端口')
        if p.hostname == 'localhost' or p.hostname.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
            raise ValueError('不能把本机、内网或保留域名作为采集来源')
        credential_scope_changed = kind_changed or bool(old and (urlsplit(old.get('url', '')).scheme,
                   urlsplit(old.get('url', '')).hostname) != (p.scheme, p.hostname))
        if credential_scope_changed and data['kind'] in ('json_api', 'brave_search', 'tavily_search', 'volc_search', 'baidu_search'):
            if not given.get('api_token') and not given.get('api_token_env'):
                raise ValueError('更换 API 服务地址时，请重新填写对应密钥或专用环境变量名')
            data['api_token_env'] = given.get('api_token_env') or ''
        try:
            literal_ip = ipaddress.ip_address(p.hostname)
        except ValueError:
            literal_ip = None
        if literal_ip is not None and (not literal_ip.is_global or literal_ip.is_multicast or literal_ip.is_reserved):
            raise ValueError('不能把非公开 IP 作为采集来源')
        if not data['platform']:
            data['platform'] = p.hostname
        if not data['source_type']:
            data['source_type'] = {'rss': 'RSS 内容', 'web': '公开网页', 'json_api': '授权 API',
                                   'brave_search': '搜索摘要（需人工核实原文）', 'tavily_search': '搜索摘要（需人工核实原文）',
                                   'bing_rss': '搜索摘要（需人工核实原文）', 'volc_search': '搜索摘要（需人工核实原文）',
                                   'baidu_search': '搜索摘要（需人工核实原文）'}[data['kind']]
        data['body_selector'] = str(data.get('body_selector') or '')[:100]
        data['api_token_env'] = str(data.get('api_token_env') or '')[:100]
        if data['api_token_env'] and not re.fullmatch(r'[A-Z][A-Z0-9_]{2,99}', data['api_token_env']):
            raise ValueError('凭证环境变量应使用大写字母、数字、下划线，如 MY_API_TOKEN')
        data['contact_consent'] = bool(data.get('contact_consent'))
        data['status'] = '待首次采集'
        data['last_error'] = ''
        saved = self.db.save('sources', data)
        if credential_scope_changed:
            self.config.delete_source_token(saved['id'])
        if given.get('api_token'):
            self.config.save_source_token(saved['id'], given['api_token'])
        saved['has_token'] = bool(self.config.source_token(saved['id']) or os.environ.get(saved['api_token_env']))
        return saved

    def sources(self):
        result = self.db.all('sources')
        for source in result:
            source['has_token'] = bool(self.config.source_token(source['id']) or os.environ.get(source.get('api_token_env', '')))
        return result

    def save_task(self, given, uid=None):
        old = self.db.get('tasks', uid) if uid else {}
        if not uid and len(self.db.all('tasks')) >= 20:
            raise ValueError('本地版本最多配置 20 个任务')
        allowed = ('name', 'industry', 'regions', 'keywords', 'exclude_keywords',
                   'interval_minutes', 'enabled', 'min_score', 'source_ids', 'business_intro', 'search_budget', 'domestic_search', 'search_domains', 'lead_mode')
        data = dict(old)
        data.update({k: given[k] for k in allowed if k in given})
        data['name'] = str(data.get('name') or '').strip()[:100]
        data['industry'] = str(data.get('industry') or '其他').strip()[:40]
        if not data['name']:
            raise ValueError('请输入任务名称')
        for key, label in [('regions', '地区'), ('keywords', '关键词'), ('exclude_keywords', '排除词')]:
            data[key] = terms(data.get(key, []), label)
        if not data['regions'] or not data['keywords']:
            raise ValueError('至少设置一个地区和一个需求关键词；地区只按原文匹配')
        data['source_ids'] = list(dict.fromkeys(int(i) for i in data.get('source_ids', [])))
        if not data['source_ids']:
            raise ValueError('请先添加真实数据源，并为任务选择至少一个来源')
        for sid in data['source_ids']:
            source = self.db.get('sources', sid)
            if source.get('kind') == 'brave_search' and (source.get('storage_authorized') is not True or
               not 10 <= len(str(source.get('storage_authorization_note') or '').strip()) <= 500):
                raise ValueError('所选 Brave 来源缺少书面存储许可，请改选已授权来源')
        interval = int(data.get('interval_minutes', 1440))
        if interval not in (60, 360, 1440):
            raise ValueError('定时频率可选每小时、每6小时或每天')
        data['interval_minutes'] = interval
        data['min_score'] = int(data.get('min_score', 60))
        data['search_budget'] = int(data.get('search_budget', 6))
        data['domestic_search'] = bool(data.get('domestic_search', True))
        from .collectors import search_domains
        data['search_domains'] = search_domains(data)
        if data['search_budget'] not in (1, 3, 6, 12):
            raise ValueError('每次搜索组数只能选择 1、3、6 或 12')
        if not 0 <= data['min_score'] <= 100:
            raise ValueError('最低分数必须在 0–100 之间')
        data['enabled'] = bool(data.get('enabled', True))
        if data.get('lead_mode', 'consumer') not in ('consumer', 'business'):
            raise ValueError('线索类型只能是个人求助或企业商机')
        data['lead_mode'] = data.get('lead_mode', 'consumer')
        data['business_intro'] = str(data.get('business_intro') or '提供相关服务')[:180]
        data['next_run_at'] = (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat(timespec='seconds')
        data.setdefault('last_run_at', '')
        return self.db.save('tasks', data)

    def run_task(self, uid, background=True):
        with self.task_lock:
            task = self.db.get('tasks', uid)
            if uid in self.active:
                raise ValueError('该任务正在采集，请等待完成')
            if not self.capacity.acquire(blocking=False):
                raise ValueError('已有两个任务在采集，请稍后再试')
            self.active.add(uid)
            run = self.db.save('runs', dict(task_id=uid, task_name=task['name'], started_at=now(),
                 finished_at='', status='running', fetched=0, accepted=0, duplicates=0, errors=[], message='真实来源采集中'))
            task['last_run_at'] = now()
            task['next_run_at'] = (datetime.now(timezone.utc) + timedelta(minutes=task['interval_minutes'])).isoformat(timespec='seconds')
            self.db.save('tasks', task)
        if background:
            threading.Thread(target=self._run, args=(task, run), daemon=True).start()
        else:
            self._run(task, run)
        return {'run_id': run['id']}

    def _run(self, task, run):
        successes = 0
        try:
            if self.collector is None:
                from .collectors import collect
                collector = collect
            else:
                collector = self.collector
            for sid in task['source_ids']:
                source = None
                try:
                    with self.source_lock:
                        source = self.db.get('sources', sid)
                        # Also guard old installations and custom collectors.
                        if source.get('kind') == 'brave_search' and (source.get('storage_authorized') is not True or
                           not 10 <= len(str(source.get('storage_authorization_note') or '').strip()) <= 500):
                            raise ValueError('Brave 来源缺少书面存储许可，已停止查询和保存。请改用获准来源')
                        private_source = dict(source)
                        token = self.config.source_token(sid)
                        if token:
                            private_source['_api_token'] = token
                    search_task = dict(task)
                    search_task.setdefault('search_budget', 6)
                    search_task.setdefault('domestic_search', True)
                    items = collector(private_source, search_task)
                    run.setdefault('search_queries', []).extend(getattr(items, 'queries', []))
                    run.setdefault('search_channels', []).extend(getattr(items, 'channels', []))
                    run['errors'].extend(getattr(items, 'errors', []))
                    for item in items:
                        run['fetched'] += 1
                        domain = urlsplit(item['url']).hostname
                        if domain:
                            site = next((r for r in run.setdefault('returned_sites',[]) if r['domain']==domain),None)
                            if site:
                                site['count'] += 1
                            else:
                                run['returned_sites'].append(dict(domain=domain,count=1))
                        lead = analyze(item, task, source)
                        if lead:
                            if self.db.insert_lead(lead):
                                run['accepted'] += 1
                            else:
                                run['duplicates'] += 1
                        else:
                            run['filtered'] = run.get('filtered', 0) + 1
                    successes += 1
                    current_source = self.db.get('sources', sid)
                    current_source.update(status='可访问', last_error='', last_fetch_at=now())
                    self.db.save('sources', current_source)
                except Exception as exc:
                    msg = self.safe_error(exc)
                    run['errors'].append(f'{source["name"] if source else "已移除来源"}：{msg}')
                    if source:
                        current_source = self.db.get('sources', sid)
                        current_source.update(status='采集失败', last_error=msg, last_fetch_at=now())
                        self.db.save('sources', current_source)
            run['status'] = 'partial' if run['errors'] and successes else ('failed' if run['errors'] else 'success')
            run['message'] = (f'采集 {run["fetched"]} 条真实内容，新增 {run["accepted"]} 条待审核线索，重复 {run["duplicates"]} 条。'
                              if run['accepted'] else f'采集 {run["fetched"]} 条真实内容，本次无新增匹配线索。')
            run['message'] += f' 已过滤 {run.get("filtered", 0)} 条不符合需求条件的内容；执行 {len(run.get("search_queries", []))} 组公开搜索。'
        except Exception as exc:
            run['status'] = 'failed'
            run['errors'].append(self.safe_error(exc))
            run['message'] = '本次任务失败，请查看原因'
        finally:
            run['finished_at'] = now()
            self.db.save('runs', run)
            with self.task_lock:
                self.active.discard(task['id'])
            self.capacity.release()

    def update_lead(self, uid, given):
        with self.lead_lock:
            return self._update_lead(uid, given)

    def _update_lead(self, uid, given):
        old = self.db.get('leads', uid)
        updates = {k: str(given[k]) for k in ('draft', 'review_status', 'status', 'notes') if k in given}
        if updates.get('review_status', old['review_status']) not in ('pending', 'approved', 'rejected'):
            raise ValueError('未知审核状态')
        if updates.get('status', old['status']) not in ('new', 'contacted', 'closed'):
            raise ValueError('未知跟进状态')
        if len(updates.get('draft', '')) > 4000 or len(updates.get('notes', '')) > 4000:
            raise ValueError('话术或备注不超过 4000 字')
        if updates.get('review_status') == 'approved' and not (updates.get('draft') or old['draft']).strip():
            raise ValueError('请先审阅或填写跟进话术')
        if 'review_status' in updates:
            updates['reviewed_at'] = now()
        if old.get('record_id') and updates:
            updates['synced_at'] = ''
        if 'followup_permission_confirmed' in given:
            updates['followup_permission_confirmed'] = given['followup_permission_confirmed'] is True
        search_summary = old.get('source_kind') in ('brave_search', 'tavily_search') or '搜索摘要' in old.get('source_type', '')
        if search_summary and updates.get('review_status', old['review_status']) == 'approved' and not updates.get(
                'followup_permission_confirmed', old.get('followup_permission_confirmed', False)):
            raise ValueError('请先打开原文，确认明确邀请咨询回复或商业联系，并勾选使用许可复核')
        return self.db.update_lead(uid, updates)

    def feishu_client(self):
        conf = self.config.feishu()
        if not conf.get('app_token') or not conf.get('table_id') or not (conf.get('tenant_access_token') or
           (conf.get('app_id') and conf.get('app_secret'))):
            raise ValueError('飞书待连接：请在本机设置合法应用凭证、app_token 和 table_id')
        if self.feishu_factory:
            return self.feishu_factory(conf)
        from .feishu import FeishuClient
        return FeishuClient(conf)

    def check_feishu(self):
        try:
            result = self.feishu_client().check()
            self.log('飞书连接检查已完成，请核对缺失字段和字段类型', 'success' if result.get('ok') else 'failed')
            return result
        except Exception as exc:
            self.log('飞书连接检查失败', errors=[self.safe_error(exc)])
            raise ValueError(self.safe_error(exc))

    def sync(self):
        if not self.sync_lock.acquire(blocking=False):
            raise ValueError('飞书同步正在进行')
        result = {'synced': 0, 'failed': 0, 'skipped': 0, 'errors': []}
        try:
            client = self.feishu_client()
            check = client.check()
            if not check.get('ok'):
                raise ValueError('飞书字段检查未通过：' + str(check.get('missing_fields') or check.get('type_errors') or '请核对权限'))
            pending = [r for r in self.db.all('leads') if r['review_status'] == 'approved' and not r.get('synced_at')]
            for lead in pending[:100]:
                try:
                    with self.lead_lock:
                        # Review/draft cannot change while this approved snapshot is sent.
                        lead = self.db.get('leads', lead['id'])
                        if lead['review_status'] != 'approved' or lead.get('synced_at'):
                            result['skipped'] += 1
                            continue
                        if (lead.get('source_kind') in ('brave_search', 'tavily_search') or '搜索摘要' in lead.get('source_type', '')) and not lead.get('followup_permission_confirmed'):
                            raise ValueError('搜索摘要尚未确认原文邀请回复及内容使用许可，请重新人工审核')
                        record_id = client.upsert(lead)
                        self.db.update_lead(lead['id'], {'record_id': record_id, 'synced_at': now(), 'sync_error': ''})
                        result['synced'] += 1
                except Exception as exc:
                    msg = self.safe_error(exc)
                    self.db.update_lead(lead['id'], {'sync_error': msg})
                    result['failed'] += 1
                    result['errors'].append(msg)
            self.log(f'飛书同步：成功 {result["synced"]} 条，失败 {result["failed"]} 条；仅发送已审核线索',
                     'partial' if result['failed'] else 'success', result['errors'])
            return result
        except Exception as exc:
            self.log('飞书同步失败', errors=[self.safe_error(exc)])
            raise ValueError(self.safe_error(exc))
        finally:
            self.sync_lock.release()

    def dashboard(self):
        leads = self.db.all('leads')
        return {'stats': {'total': len(leads), 'high_intent': sum(r['score'] >= 80 for r in leads),
                         'approved': sum(r['review_status'] == 'approved' for r in leads),
                         'pending_sync': sum(r['review_status'] == 'approved' and not r.get('synced_at') for r in leads)},
                'tasks': self.db.all('tasks'), 'sources': self.sources(), 'runs': self.db.all('runs')[:8],
                'lan_urls': self.lan_urls, 'engine': '具体求助筛选 v2（规则判断，需核实原文）', 'auto_outreach': False}

    def scheduler(self):
        last_retention = 0
        last_sync = 0
        while not self.stop_event.wait(5):
            try:
                current = now()
                for task in self.db.all('tasks'):
                    if task['enabled'] and task['next_run_at'] <= current and task['id'] not in self.active:
                        try:
                            self.run_task(task['id'])
                        except ValueError:
                            pass
                if time.time() - last_retention >= 86400:
                    removed = self.db.retain(self.config.value.get('retention_days', 90))
                    if removed:
                        self.log(f'按保留期限删除了 {removed} 条本机旧线索，未删除飞书记录', 'success')
                    last_retention = time.time()
                if time.time() - last_sync >= 60:
                    if self.config.feishu().get('auto_sync') and any(r['review_status'] == 'approved' and not r.get('synced_at') for r in self.db.all('leads')):
                        self.sync()
                    last_sync = time.time()
            except Exception as exc:
                self.log('后台任务异常', errors=[self.safe_error(exc)])
                last_sync = time.time()

    def start(self):
        self.scheduler_thread = threading.Thread(target=self.scheduler, daemon=True, name='radar-scheduler')
        self.scheduler_thread.start()
