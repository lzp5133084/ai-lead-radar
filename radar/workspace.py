"""Local operating guide and editable GEO drafts; never publishes externally."""
import io
import json
import html
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone
from .core import compact, now

DEFAULT_PROFILE = dict(name='军哥懂保', entity_type='Person', industry='保险', regions='四川 / 泸州',
    service='保险需求梳理与方案咨询', contact='微信：xunijiayuan', qualification='',
    evidence_url='', website='', facts_confirmed=False, updated_at='')


def public_url(value):
    if not value:
        return ''
    from .collectors import validate_public_url, CollectorError
    try:
        return validate_public_url(value, reference_only=True)[0]
    except CollectorError:
        raise ValueError('请填写不含凭证的公开 HTTP/HTTPS 链接，不能使用本机或私网地址') from None


class Workspace:
    def __init__(self, radar):
        self.radar = radar
        self.db = radar.db
        with self.db.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS geo_profile(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);'
                             'CREATE TABLE IF NOT EXISTS geo_entries(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);')

    def geo(self):
        with self.db.connect() as db:
            row = db.execute('SELECT payload FROM geo_profile WHERE id=1').fetchone()
            profile = json.loads(row[0]) if row else dict(DEFAULT_PROFILE)
            entries = [dict(json.loads(r['payload']), id=r['id']) for r in
                       db.execute('SELECT id,payload FROM geo_entries ORDER BY id DESC LIMIT 300')]
        return dict(profile=profile, entries=entries, generated=self.generate(profile))

    def save_profile(self, value):
        profile = {}
        for key in ('name','entity_type','industry','regions','service','contact','qualification','evidence_url','website'):
            val = value.get(key, DEFAULT_PROFILE[key])
            if not isinstance(val, str) or len(val) > 1000:
                raise ValueError('资料必须是文本，每项最多 1000 字')
            profile[key] = val.strip()
        if not all(profile[k] for k in ('name','industry','regions','service')):
            raise ValueError('请填写名称、行业、地区和真实服务')
        if profile['entity_type'] not in ('Person','Organization'):
            raise ValueError('主体类型可选个人或商家')
        for key in ('evidence_url','website'):
            profile[key] = public_url(profile[key])
        profile['facts_confirmed'] = value.get('facts_confirmed') is True
        profile['updated_at'] = now()
        with self.db.connect() as db:
            db.execute('INSERT INTO geo_profile VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (compact(profile),))
        return self.geo()

    def save_entry(self, value, uid=None):
        entry = {k: str(value.get(k, '')).strip()[:20000] for k in ('kind','title','channel','url','status','content','note')}
        if entry['kind'] not in ('draft','publication','measurement') or not entry['title']:
            raise ValueError('请选择记录类型并填写标题或测试问题')
        if entry['status'] not in ('draft','reviewed','published','observed'):
            raise ValueError('记录状态无效')
        entry['url'] = public_url(entry['url'])
        if entry['status'] == 'published' and not entry['url']:
            raise ValueError('已发布记录需要真实公开链接')
        entry['updated_at'] = now()
        with self.db.connect() as db:
            if uid:
                if not db.execute('SELECT id FROM geo_entries WHERE id=?',(uid,)).fetchone():
                    raise ValueError('记录不存在')
                db.execute('UPDATE geo_entries SET payload=? WHERE id=?',(compact(entry),uid))
            else:
                uid = db.execute('INSERT INTO geo_entries(payload) VALUES(?)',(compact(entry),)).lastrowid
        return dict(entry,id=uid)

    def delete_entry(self, uid):
        with self.db.connect() as db:
            db.execute('DELETE FROM geo_entries WHERE id=?',(uid,))

    def generate(self, p):
        location = p['regions']
        intro = f"{p['name']}｜{location} {p['industry']}咨询\n服务：{p['service']}\n联系：{p['contact'] or '待填写公开咨询渠道'}\n资质与服务授权：{p['qualification'] or '待补充并核验；发布前需完善'}"
        qualification = '联系前请核验从业资格、服务授权和实际服务范围。' if p['industry']=='保险' else '联系前请核验经营资质、服务范围与真实案例。'
        questions = [f"在{location}如何选择{p['industry']}咨询服务？", f"咨询{p['industry']}问题前，需要准备什么？", f"怎样联系{p['name']}并核实服务信息？"]
        answers = [f"先明确自己的需求，再核实服务方资质、服务范围和公开说明。{qualification}不要只凭排行榜或宣传词做决定。",
                   '可以先整理希望解决的问题、已有安排和关注点。首次交流不要公开身份证号、病历等敏感信息；具体方案须经本人确认。',
                   f"{p['name']}的服务地区为{location}，公开咨询渠道为{p['contact'] or '待填写'}。服务介绍：{p['service']}。{qualification}此信息来自本人填写，尚不代表第三方背书。"]
        faq = '\n\n'.join(f'问：{q}\n答：{a}' for q,a in zip(questions, answers))
        schema = {'@context':'https://schema.org','@type':p['entity_type'],'name':p['name'],
                  'description':p['service'],'areaServed':location}
        if p['website']: schema['url']=p['website']
        return dict(intro=intro,faq=faq,schema=schema,questions=questions,qa=list(zip(questions,answers)),
                    plan=['补齐并核实资料、资质和公开联系方式','制作独立介绍页和解决真实问题的问答内容','由本人审核后发布到有权经营的网站或账号','记录真实发布链接，检查公开页面是否可访问','定期用相同问题在目标 AI 中人工测试，记录实际答案和引用链接；未测就标记未测'])

    def export(self):
        data = self.geo()
        p, g = data['profile'], data['generated']
        if not p['facts_confirmed']:
            raise ValueError('请先核实资料并勾选确认，生成内容仍须审核后发布')
        if p['industry']=='保险' and not p['qualification']:
            raise ValueError('保险内容导出前请填写可核验的从业资质与服务授权说明')
        esc = html.escape
        schema = json.dumps(g['schema'], ensure_ascii=False).replace('<','\\u003c')
        faq = ''.join(f'<section><h2>{esc(q)}</h2><p>{esc(a)}</p></section>' for q,a in g['qa'])
        page = f'<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(p["name"])} · {esc(p["regions"])}</title><meta name="description" content="{esc(p["service"],quote=True)}"><script type="application/ld+json">{schema}</script><body><main><h1>{esc(p["name"])} · {esc(p["industry"])}</h1><p>{esc(g["intro"]).replace(chr(10),"<br>")}</p>{faq}<p>资料更新：{esc(p["updated_at"])}。本页不是保险公司官方页面，不保证任何 AI 的排名或推荐。</p></main></body></html>'
        out = io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('介绍页.html',page)
            archive.writestr('内容草稿.txt',g['intro']+'\n\n'+g['faq'])
            archive.writestr('实体资料.json',json.dumps(g['schema'],ensure_ascii=False,indent=2))
            for entry in data['entries']:
                if entry['kind']=='draft':
                    archive.writestr(f'保存的草稿/{entry["id"]}.txt',entry['title']+'\n\n'+entry['content'])
            archive.writestr('发布步骤.txt','\n'.join(g['plan'])+'\n这些文件只导出到本机，并未发布到公网。结构化资料不是控制 AI 推荐的指令，不保证收录或推荐。')
        return out.getvalue()

    def guide(self):
        tasks, runs, leads = self.db.all('tasks'), self.db.all('runs'), self.db.all('leads')
        sites = {}
        for run in runs:
            for record in run.get('returned_sites',[]):
                key = record['domain']
                item = sites.setdefault(key,dict(domain=key,returned=0,last_at='',evidence='进入筛选的真实返回摘要'))
                item['returned'] += record['count']
                item['last_at'] = max(item['last_at'],run.get('finished_at') or run['started_at'])
        saved_sites = sorted({urlsplit(l['url']).hostname for l in leads if urlsplit(l['url']).hostname})
        targeted = sorted({c['name'] for r in runs for c in r.get('search_channels',[]) if '.' in c['name']})
        root = Path(__file__).resolve().parents[1]
        files = [f for f in root.rglob('*') if f.is_file() and not any(k in f.relative_to(root).parts for k in ('data','__pycache__','.git'))]
        return dict(tasks=tasks, runs=runs[:40], sites=list(sites.values()),saved_sites=saved_sites,targeted_sites=targeted,
                    history_limit=200,service_started_at=self.radar.service_started_at,
                    program=dict(bytes=sum(f.stat().st_size for f in files), modified_at=datetime.fromtimestamp(max((f.stat().st_mtime for f in files),default=0),timezone.utc).isoformat(),author='军哥懂保',wechat='xunijiayuan'))
