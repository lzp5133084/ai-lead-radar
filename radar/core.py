"""Transparent intent rules and SQLite persistence. No seeded or generated leads."""
import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def canonical_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('来源链接必须为公开 HTTP/HTTPS 地址')
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('spm', 'fbclid')]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', urlencode(pairs), ''))


PHONE = re.compile(r'(?<!\d)(?:\+?86[- ]?)?1[- ]?[3-9](?:[- ]?\d){9}(?!\d)')
LANDLINE = re.compile(r'(?<!\d)(?:\+?86[- ]?)?0\d{2,3}[- ]\d{7,8}(?!\d)')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
WECHAT = re.compile(r'(?:微信|微[信訊]|wechat|WeChat)\s*(?:号|ID)?\s*[:：]?\s*([A-Za-z][A-Za-z0-9_-]{5,19})', re.I)
IDENTITY = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')
COMMERCIAL = re.compile(r'商务联系|商务合作|商业合作|业务咨询|报价请联系|欢迎商家联系|装修咨询请联系|保险咨询请联系')
NO_CONTACT = re.compile(r'不要.{0,8}(?:联系|合作)|请?勿.{0,8}(?:联系|合作)|拒绝.{0,8}(?:联系|合作)|谢绝|禁止.{0,8}(?:联系|营销)|勿扰|别联系|不接受.{0,8}(?:合作|推销)|不欢迎.{0,8}(?:商家|营销)|do not contact', re.I)
SENSITIVE = re.compile(r'癌症|糖尿病|抑郁|病历|住院|残疾|艾滋|HIV|身份证|月薪|收入|负债|诊断|疾病|病史', re.I)
DEMAND = re.compile(r'想要|想买|想了解|想做|求推荐|求助|求报价|找.{0,8}(?:公司|师傅|施工|保险|设计|方案)|有没有|哪家|推荐|咨询|询价|准备|计划|需要|打算|预算|谁能|怎么(?:买|选|配置|装修)|如何(?:买|选|配置|装修)')
REPLY_INVITATION = re.compile(r'求推荐|求助|求报价|谁能|有没有.{0,12}(?:推荐|方案|顾问)|(?:请|希望|欢迎).{0,12}(?:推荐|提供方案|给.{0,4}方案|联系|回复|建议)|找.{0,8}(?:保险顾问|保险咨询|设计师|师傅)')
AD = re.compile(r'诚招|加盟|代理招聘|厂家直销|承接.{0,8}(?:装修|工程)|我们提供|点击领取|专业施工|服务热线')
AD_TITLE = re.compile(r'有限公司|商业品牌网|装修网|保险网|报价清单|深度解析|实力解析|实力企业|品牌排名|排行榜|管理中心.{0,8}(?:简介|介绍)|避坑指南|本土老牌|厂家直销')
BUYER_REQUEST = re.compile(r'想买|想投保|准备.{0,6}(?:买保险|投保|装修)|想.{0,6}(?:找|配置).{0,8}(?:保险|保障|装修|施工)|求推荐|求助|求报价|请.{0,6}(?:提供|推荐).{0,8}(?:方案|保险|顾问|施工)|找.{0,6}(?:保险顾问|设计师|师傅)|(?:我|我家|本人|家里|孩子).{0,25}(?:需要|打算|计划|想).{0,15}(?:保险|保障|装修|施工)')
# B端商机：公开发布的采购/承保公告是真实存在的商业机会，语气是公告而非求助。
BIZ_DEMAND = re.compile(r'招标|采购|比选|竞争性磋商|竞争性谈判|询价|遴选|中选|中标|承保|征集|招募|成交公告|结果公告|保险经纪|供应商库|入围')
BIZ_REQUEST = re.compile(r'(?:招标|采购|比选|磋商|谈判|询价|遴选|征集|招募|中选|中标|承保|入围).{0,40}(?:公告|通知|结果|公示|项目)|项目.{0,20}(?:招标|采购|比选|遴选|承保)|(?:保险|年金|福利|保障).{0,20}(?:招标|采购|比选|遴选|承保)')


def demand_evidence(item, task):
    """A request must be in the same short passage as the service topic.

    This is candidate evidence, never proof of identity or permission to market.
    Article menus and generic 'recommendation/consultation' words do not count.
    """
    biz_mode = task.get('lead_mode') == 'business'
    title = str(item.get('title', ''))
    if not biz_mode and AD_TITLE.search(title):
        return ''
    request_re = BIZ_REQUEST if biz_mode else BUYER_REQUEST
    text = title + '\n' + str(item.get('text', ''))
    for passage in re.split(r'[\n。！？!?;；]', text):
        if len(passage) > 300 or not request_re.search(passage):
            continue
        if any(k.casefold() in passage.casefold() for k in task['keywords']):
            return redact(passage.strip())[:300]
    return ''


def redact(text):
    text = IDENTITY.sub('[身份信息已隐藏]', str(text))
    text = PHONE.sub('[联系方式已隐藏]', text)
    text = LANDLINE.sub('[联系方式已隐藏]', text)
    text = EMAIL.sub('[联系方式已隐藏]', text)
    return WECHAT.sub('微信：[联系方式已隐藏]', text)


def permitted_contacts(text, source):
    """Only explicit commercial invitation in the same short sentence is eligible."""
    if not source.get('contact_consent') or source.get('kind') in ('brave_search', 'tavily_search', 'bing_rss', 'volc_search', 'baidu_search') or NO_CONTACT.search(text):
        return ''
    values = []
    for sentence in re.split(r'[\n。！？!?;；]', text):
        if len(sentence) > 240 or not COMMERCIAL.search(sentence):
            continue
        values += PHONE.findall(sentence) + LANDLINE.findall(sentence) + EMAIL.findall(sentence) + WECHAT.findall(sentence)
    return ' / '.join(dict.fromkeys(values))[:500]


def analyze(item, task, source):
    biz_mode = task.get('lead_mode') == 'business'
    original = (str(item.get('title', '')) + '\n' + str(item.get('text', ''))).strip()
    if not original or len(original) > 120000:
        return None
    if NO_CONTACT.search(original):
        return None
    insurance_task = '保险' in task.get('industry', '') or any('保险' in k for k in task['keywords'])
    if insurance_task and SENSITIVE.search(original):
        return None
    # B端公告页常被全站导航的"招聘/加盟"链接污染；公告是否招聘类看标题就足够。
    exclude_scope = str(item.get('title', '')) if biz_mode else original
    if any(k.casefold() in exclude_scope.casefold() for k in task['exclude_keywords']):
        return None
    regions = [r for r in task['regions'] if r.casefold() in original.casefold()]
    keywords = [k for k in task['keywords'] if k.casefold() in original.casefold()]
    demand_re = BIZ_DEMAND if biz_mode else DEMAND
    # 广告词同理：B端公告页导航常带"加盟"等字样，是否广告看标题。
    ad_scope = str(item.get('title', '')) if biz_mode else original
    if not regions or not keywords or not demand_re.search(original) or AD.search(ad_scope):
        return None
    # B端公告本身就是公开的商业机会，不要求"求推荐"式求助语气。
    if not biz_mode and source.get('kind') in ('tavily_search', 'bing_rss', 'volc_search', 'baidu_search') and not REPLY_INVITATION.search(original):
        return None
    evidence = demand_evidence(item, task)
    if not evidence:
        return None
    if not biz_mode and source.get('kind') in ('tavily_search', 'brave_search', 'bing_rss', 'volc_search', 'baidu_search') and not REPLY_INVITATION.search(evidence):
        return None
    # Never use phone/health/income attributes as a scoring feature.
    if biz_mode:
        score = 70 + (5 if BIZ_REQUEST.search(evidence) else 0)
    else:
        score = 70 + (5 if re.search(r'我|本人|我家', evidence) else 0) + (5 if REPLY_INVITATION.search(evidence) else 0)
    if score < task.get('min_score', 60):
        return None
    contact = permitted_contacts(original, source)
    safe_title = redact(item.get('title', ''))[:500]
    safe_text = redact(item.get('text', ''))[:16000]
    url = canonical_url(item['url'])
    normalized = re.sub(r'\s+', '', redact(original)).casefold()
    uid = hashlib.sha256((str(task['id']) + ':' + normalized).encode()).hexdigest()[:32]
    demand = demand_re.search(safe_text or safe_title)
    pos = max(0, demand.start() - 25) if demand else 0
    excerpt = (safe_text or safe_title)[pos:pos + 100].replace('\n', ' ')
    intro = str(task.get('business_intro') or '提供相关服务')[:180]
    if biz_mode:
        # B端话术：联系对象是发布单位而非个人，联系方式仍以公告原文为准，雷达不代为提取。
        draft = f'您好，我在公开信息中看到「{excerpt}」相关公告。我这边{intro}，可按公告要求提供方案与报价。如项目仍在进行，盼告知正式对接渠道；以公告载明的联系方式为准。'
    else:
        draft = f'你好，我在{item.get("platform") or source["platform"]}的公开内容中看到你提到「{excerpt}」。我这边{intro}。如果你仍需要相关服务，是否愿意我先了解具体需求？如不方便联系，我会尊重你的选择。'
    if not biz_mode and source.get('kind') in ('brave_search', 'tavily_search', 'bing_rss', 'volc_search', 'baidu_search'):
        draft = f'你好，我通过公开搜索看到一条可能与你相关的摘要，内容提到「{excerpt}」。如果这确实是你的需求，我这边{intro}，是否愿意我先了解具体情况？如不方便联系，我会尊重你的选择。'
    evidence_label = '商机公告片段：' if biz_mode else '具体求助片段：'
    return dict(id=uid, task_id=task['id'], industry=task['industry'],
                region=' / '.join(regions), title=safe_title, text=safe_text,
                url=url, platform=item.get('platform') or source['platform'],
                source_type=item.get('source_type') or source['source_type'], source_kind=source.get('kind', ''),
                published_at=str(item.get('published_at') or '')[:100],
                collected_at=now(), last_seen_at=now(), score=score,
                reasons=['原文明确包含地区：' + '、'.join(regions),
                         '需求关键词：' + '、'.join(keywords), evidence_label + evidence, '规则筛选候选，分数不代表已确认客户'],
                demand_evidence=evidence, rules_version=2,
                contact=contact, contact_source=url if contact else '',
                draft=draft, review_status='pending', status='new', notes='', followup_permission_confirmed=False,
                record_id='', sync_error='', synced_at='')


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY, task_id INTEGER NOT NULL,
                  collected_at TEXT NOT NULL, score INTEGER NOT NULL, review_status TEXT NOT NULL,
                  status TEXT NOT NULL, payload TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS lead_date ON leads(collected_at);
              CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def all(self, table):
        if table not in ('sources', 'tasks', 'leads', 'runs'):
            raise ValueError('未知数据类型')
        with self.connect() as db:
            limit = ' LIMIT 200' if table == 'runs' else ''
            return [json.loads(r['payload']) for r in db.execute(f'SELECT payload FROM {table} ORDER BY rowid DESC{limit}')]

    def get(self, table, uid):
        if table not in ('sources', 'tasks', 'leads', 'runs'):
            raise ValueError('未知数据类型')
        with self.connect() as db:
            r = db.execute(f'SELECT payload FROM {table} WHERE id=?', (uid,)).fetchone()
            if not r:
                raise KeyError('记录不存在')
            return json.loads(r['payload'])

    def save(self, table, value):
        if table not in ('sources', 'tasks', 'runs'):
            raise ValueError('未知数据类型')
        with self.connect() as db:
            if value.get('id'):
                db.execute(f'UPDATE {table} SET payload=? WHERE id=?', (compact(value), value['id']))
            else:
                uid = db.execute(f'INSERT INTO {table}(payload) VALUES (?)', ('{}',)).lastrowid
                value = dict(value, id=uid)
                db.execute(f'UPDATE {table} SET payload=? WHERE id=?', (compact(value), uid))
        return value

    def insert_lead(self, lead):
        with self.connect() as db:
            r = db.execute('SELECT payload FROM leads WHERE id=?', (lead['id'],)).fetchone()
            if r:
                old = json.loads(r['payload'])
                old['last_seen_at'] = now()
                db.execute('UPDATE leads SET payload=? WHERE id=?', (compact(old), old['id']))
                return False
            db.execute('INSERT INTO leads VALUES(?,?,?,?,?,?,?)',
                       (lead['id'], lead['task_id'], lead['collected_at'], lead['score'],
                        lead['review_status'], lead['status'], compact(lead)))
            return True

    def update_lead(self, uid, updates):
        with self.connect() as db:
            r = db.execute('SELECT payload FROM leads WHERE id=?', (uid,)).fetchone()
            if not r:
                raise KeyError('记录不存在')
            lead = json.loads(r['payload'])
            lead.update(updates)
            db.execute('UPDATE leads SET review_status=?,status=?,score=?,payload=? WHERE id=?',
                       (lead['review_status'], lead['status'], lead['score'], compact(lead), uid))
            return lead

    def delete(self, table, uid):
        if table not in ('sources', 'tasks', 'leads'):
            raise ValueError('未知数据类型')
        with self.connect() as db:
            db.execute(f'DELETE FROM {table} WHERE id=?', (uid,))

    def retain(self, days):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
        with self.connect() as db:
            count = db.execute('DELETE FROM leads WHERE collected_at<?', (cutoff,)).rowcount
            db.execute('DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 1000)')
        return count


def filtered_leads(db, params):
    rows = db.all('leads')
    region = params.get('region', '').strip().casefold()
    q = params.get('q', '').strip().casefold()
    try:
        minimum = int(params.get('min_score') or 0)
    except ValueError:
        raise ValueError('分数筛选必须为数字')
    return [r for r in rows if r['score'] >= minimum and
            (not params.get('industry') or r['industry'] == params['industry']) and
            (not region or region in r['region'].casefold()) and
            (not q or q in (r['title'] + r['text'] + r['platform']).casefold()) and
            (not params.get('status') or r['status'] == params['status']) and
            (not params.get('review_status') or r['review_status'] == params['review_status'])]


def csv_export(leads):
    columns = [('id', '线索编号'), ('industry', '行业'), ('region', '地区'),
               ('platform', '来源平台'), ('source_type', '来源类型'), ('title', '标题'),
               ('text', '原文（联系方式已遮蔽）'), ('url', '原始链接'),
               ('published_at', '发布时间'), ('collected_at', '采集时间'), ('score', '意向分'),
               ('reasons', '判断依据'), ('contact', '审核后公开联系方式'),
               ('contact_source', '联系方式来源'), ('draft', '人工跟进话术'),
               ('review_status', '审核状态'), ('status', '跟进状态'), ('notes', '备注')]
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow([c[1] for c in columns])
    for lead in leads:
        row = []
        for key, _ in columns:
            val = lead.get(key, '')
            if isinstance(val, list):
                val = '；'.join(val)
            search_summary = lead.get('source_kind') in ('brave_search', 'tavily_search') or '搜索摘要' in lead.get('source_type', '')
            if key in ('contact', 'contact_source', 'draft') and (lead['review_status'] != 'approved' or
                   (search_summary and not lead.get('followup_permission_confirmed'))):
                val = '[待人工审核]' if val else ''
            val = str(val)
            if val.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
                val = "'" + val
            row.append(val)
        writer.writerow(row)
    return ('\ufeff' + out.getvalue()).encode('utf-8')
