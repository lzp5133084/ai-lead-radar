'use strict';

const state = {session:null,page:'dashboard',dashboard:null,profile:null,tasks:[],sources:[],leads:[],runs:[],settings:null,filters:{q:'',region:'',industry:'',min_score:'0',status:'',review_status:''},loading:false,menu:false};
const navItems = [
  ['dashboard','◎','工作概览'],['leads','▤','线索管理'],['tasks','◷','采集任务'],['sources','⌘','真实数据源'],['logs','☷','运行日志'],['guide','?','使用与运行说明'],['geo','✧','GEO 内容工作台'],['settings','⚙','飞书与设置'],['privacy','◇','使用边界']
];
const app = document.getElementById('app');
const modalRoot = document.getElementById('modal-root');
let toastTimer, pollTimer, lastFocus;

function e(value) {return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function safeUrl(value) {try {const url=new URL(String(value));return ['http:','https:'].includes(url.protocol)?url.href:'';}catch{return '';}}
function date(value) {if(!value)return '—';const d=new Date(value);return Number.isNaN(d.valueOf())?String(value):d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
function number(value) {return Number.isFinite(Number(value)) && value !== null && value !== undefined ? Number(value).toLocaleString('zh-CN') : '—';}
function list(value) {return Array.isArray(value)?value:[];}
function terms(value) {return String(value??'').split(/[\n,，;；]+/).map(s=>s.trim()).filter(Boolean);}
function shorten(value,length=100) {const s=String(value??'');return s.length>length?s.slice(0,length)+'…':s;}
function tag(label,kind='') {return `<span class="tag ${e(kind)}">${e(label)}</span>`;}
function button(label,action,attrs='',kind='') {return `<button type="button" class="btn ${kind}" data-action="${e(action)}" ${attrs}>${e(label)}</button>`;}
function idAttrs(id) {return `data-id="${e(id)}"`;}
function sourceKind(value) {return {rss:'公开 RSS',web:'公开网页',json_api:'授权 JSON API',tavily_search:'Tavily 公开搜索 API',brave_search:'Brave 搜索 API（需书面存储许可）',bing_rss:'必应国内公开搜索 RSS',volc_search:'火山方舟联网搜索',baidu_search:'百度AI搜索'}[value]||value||'未设置';}
function isSearchSource(kind){return ['tavily_search','brave_search','bing_rss','volc_search','baidu_search'].includes(kind);}
function hasStoragePermission(source){const note=String(source.storage_authorization_note||'').trim();return !!source.storage_authorized&&note.length>=10&&note.length<=500;}
function sourceSelectable(source){return !!source.permission_confirmed&&(source.kind!=='brave_search'||hasStoragePermission(source));}
function sourceSelectionNote(source){if(!source.permission_confirmed)return '请先在数据源中确认访问许可';if(source.kind==='brave_search'&&!hasStoragePermission(source))return '缺少另行取得的书面存储许可，暂不可选';return '已确认访问许可';}
function reviewTag(value) {return tag({pending:'待人工审核',approved:'审核通过',rejected:'已拒绝'}[value]||'待人工审核',{pending:'amber',approved:'green',rejected:'red'}[value]||'amber');}
function statusTag(value) {return tag({new:'未跟进',contacted:'已人工跟进',closed:'已结束'}[value]||value||'未跟进');}
function runTag(value) {return tag({running:'正在采集',success:'采集完成',partial:'部分失败',failed:'采集失败'}[value]||value||'未知状态',{running:'blue',success:'green',partial:'amber',failed:'red'}[value]||'');}
function sourceTag(source) {if(!source.permission_confirmed)return tag('待确认访问许可','amber');if(source.kind==='brave_search'&&!hasStoragePermission(source))return tag('待书面存储许可','amber');const s=source.status;return tag(({ready:'可采集',connected:'已连接',success:'可采集',ok:'可采集',failed:'连接失败',error:'连接失败',pending:'待首次采集',new:'待首次采集',unconnected:'待连接',disabled:'已暂停'}[s])||s||'待首次采集',['failed','error','采集失败'].includes(s)?'red':['ready','connected','success','ok','可访问'].includes(s)?'green':'');}
function toast(message,error=false) {const t=document.getElementById('toast');t.textContent=String(message);t.className=`toast visible${error?' error':''}`;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{t.className='toast';},error?7000:4200);}
function link(url,label,css='') {const safe=safeUrl(url);return safe?`<a href="${e(safe)}" target="_blank" rel="noopener noreferrer" class="${e(css)}">${e(label||url)} ↗</a>`:`<span class="muted">${e(label||url||'未提供链接')}</span>`;}
function heading(title,description,actions='') {return `<div class="page-heading"><div><div class="eyebrow">LEAD RADAR · 本地客资雷达</div><h1>${e(title)}</h1><p>${e(description)}</p></div><div class="heading-actions">${actions}</div></div>`;}
function empty(title,description,action='',label='') {return `<div class="empty"><div class="empty-icon">◎</div><strong>${e(title)}</strong><p>${e(description)}</p>${action?button(label,action,'','primary small'):''}</div>`;}
function field(label,name,value='',type='text',help='',extra='') {return `<div class="field"><label for="f-${e(name)}">${e(label)}</label><input id="f-${e(name)}" name="${e(name)}" type="${e(type)}" value="${e(value)}" ${extra}>${help?`<p class="help">${e(help)}</p>`:''}</div>`;}
function textarea(label,name,value='',help='',extra='') {return `<div class="field"><label for="f-${e(name)}">${e(label)}</label><textarea id="f-${e(name)}" name="${e(name)}" ${extra}>${e(value)}</textarea>${help?`<p class="help">${e(help)}</p>`:''}</div>`;}
function select(label,name,value,choices,extra='') {return `<div class="field"><label for="f-${e(name)}">${e(label)}</label><select id="f-${e(name)}" name="${e(name)}" ${extra}>${choices.map(([v,label])=>`<option value="${e(v)}" ${String(value)===String(v)?'selected':''}>${e(label)}</option>`).join('')}</select></div>`;}
function checkbox(name,label,checked=false,help='',extra='') {return `<label class="check"><input type="checkbox" name="${e(name)}" ${checked?'checked':''} ${extra}><span>${e(label)}${help?`<small>${e(help)}</small>`:''}</span></label>`;}

async function api(path,method='GET',data) {
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),45000);
  const headers={'Accept':'application/json'};
  if(data!==undefined)headers['Content-Type']='application/json';
  if(method!=='GET' && state.session?.csrf)headers['X-CSRF-Token']=state.session.csrf;
  try {
    const response=await fetch(path,{method,headers,credentials:'same-origin',body:data===undefined?undefined:JSON.stringify(data),signal:controller.signal});
    let payload;try{payload=await response.json();}catch{throw new Error('本地服务返回了无法识别的响应，请查看服务窗口。');}
    if(!response.ok||payload.ok===false){
      if(response.status===401 && path!=='/api/login'){state.session=null;clearInterval(pollTimer);closeModal();renderLogin();}
      const error=new Error(payload.error||`请求失败（${response.status}）`);error.status=response.status;throw error;
    }
    return payload.data;
  }catch(error){if(error.name==='AbortError')throw new Error('服务响应超时，请查看运行日志后再试。');if(error instanceof TypeError)throw new Error('无法连接本地服务，请确认电脑上的启动窗口仍在运行。');throw error;}finally{clearTimeout(timer);}
}

function renderLogin(error='') {
  app.innerHTML=`<div class="auth-page"><section class="auth-story"><div class="brand"><span class="radar-mark">◎</span><div><strong>客资雷达</strong><small>LEAD RADAR LOCAL</small></div></div><h1>让每一条线索，<br>都有真实来源。</h1><p>在自己的电脑上管理采集任务、判断需求意向、审核跟进话术。手机也可通过同一局域网访问。</p><div class="auth-proof"><span>本地 SQLite 数据库</span><span>真实来源留存</span><span>人工审核跟进</span></div></section><section class="auth-form-zone"><div class="auth-card"><div class="eyebrow">你的本地工作台</div><h2>管理员登录</h2><p>请输入本机管理员密码。</p><form data-form="login"><div class="auth-error notice error" role="alert">${e(error)}</div>${field('管理员密码','password','','password','','required autocomplete="current-password" autofocus')}<button class="btn primary" type="submit">登录工作台 →</button></form><div class="auth-foot">首次启动密码见电脑 data/首次登录.txt，登录后可修改。<br>请勿把平台账号密码或应用密钥发到聊天中。</div></div></section></div>`;
}
function renderShell() {
  app.innerHTML=`<div class="shell${state.menu?' menu-open':''}"><aside class="sidebar"><div class="brand"><span class="radar-mark">◎</span><div><strong>客资雷达</strong><small>LEAD RADAR LOCAL</small></div></div><div class="nav-label">工作台</div><nav class="nav" aria-label="主导航">${navItems.map(([key,icon,title])=>`<button data-action="navigate" data-page="${e(key)}" class="${key===state.page?'active':''}" ${key===state.page?'aria-current="page"':''}><span class="nav-icon" aria-hidden="true">${icon}</span>${title}</button>`).join('')}</nav><div class="sidebar-foot"><div class="local-badge"><span class="dot"></span>本地服务 · 管理员</div><p>真实来源 / 人工审核<br>自动触达始终关闭</p><button class="logout" data-action="logout">退出登录</button></div></aside><main class="main"><header class="topbar"><button class="menu-toggle" data-action="menu" aria-label="展开或收起导航">☰</button><span class="breadcrumb">工作台 / ${e(navItems.find(x=>x[0]===state.page)?.[2])}</span><div class="topbar-right"><span class="chip">仅本地 / 局域网使用</span><span class="user-name">${e(state.session?.username||'管理员')}</span><span class="avatar" aria-hidden="true">管</span></div></header><div class="content" id="page-content"></div></main></div>`;
  renderContent();
}
function renderContent(error='') {
  const target=document.getElementById('page-content');if(!target)return;
  const renderer={dashboard:renderDashboard,leads:renderLeads,tasks:renderTasks,sources:renderSources,logs:renderLogs,settings:renderSettings,privacy:renderPrivacy,guide:renderGuide,geo:renderGeo}[state.page];
  target.innerHTML=(error?`<div class="notice error page-error" role="alert">${e(error)} ${button('重试','refresh','','small')}</div>`:'')+(state.loading?'<div class="loading"><span class="spinner"></span>正在读取本地数据…</div>':renderer());
}
async function loadPage(page=state.page,quiet=false) {
  if(!state.session)return;
  state.page=page;state.menu=false;
  if(!quiet){state.loading=true;renderShell();}
  try {
    if(page==='dashboard'){const responses=await Promise.all([api('/api/dashboard'),loadProfile()]);state.dashboard=responses[0];}
    else if(page==='leads'){state.leads=list(await api('/api/leads?'+new URLSearchParams(state.filters)));}
    else if(page==='tasks'){const responses=await Promise.all([api('/api/tasks'),api('/api/sources')]);state.tasks=list(responses[0]);state.sources=list(responses[1]);}
    else if(page==='sources'){state.sources=list(await api('/api/sources'));}
    else if(page==='logs'){state.runs=list(await api('/api/runs'));}
    else if(page==='guide'){state.guide=await api('/api/guide');}
    else if(page==='geo'){state.geo=await api('/api/geo');}
    else if(page==='settings'){const responses=await Promise.all([api('/api/settings'),api('/api/dashboard')]);state.settings=responses[0];state.dashboard=responses[1];}
    state.loading=false;if(state.session){if(!quiet)renderShell();else renderContent();}
  }catch(error){state.loading=false;if(state.session)renderContent(error.message);}
}
function startPolling(){clearInterval(pollTimer);pollTimer=setInterval(()=>{if(state.session&&!state.loading&&!modalRoot.childElementCount&&['dashboard','logs'].includes(state.page)&&document.visibilityState==='visible')loadPage(state.page,true);},10000);}
async function loadProfile(){if(state.profile===null){state.profile=await api('/api/profile')||{};}return state.profile;}
function profileNotice(){const p=state.profile;if(!p?.name)return '';return `<div class="notice mb-20"><strong>已预填：${e(p.name)}</strong><br>地区、保险需求关键词和服务介绍已填好。这份预填设置不会自动采集；选择真实来源并保存任务后，点击“立即采集”开始。</div>`;}

function renderDashboard() {
  const d=state.dashboard||{},s=d.stats||{},tasks=list(d.tasks),sources=list(d.sources),runs=list(d.runs);
  return heading('每条线索，来源可追溯','从允许访问的真实来源发现需求。系统筛选候选内容，最终意向与联系许可由你审核。',button('刷新数据','refresh')+button('＋ 新建任务','new-task','','primary'))+
    profileNotice()+
    `<div class="hero-strip"><div><strong>你的真实采集工作台已就绪</strong><p>当前判断引擎：${e(d.engine||'透明规则 v1')} · 自动触达关闭 · 未采集到内容时会如实显示 0 条</p></div><span class="hero-tag">本地存储，人工跟进</span></div><div class="metrics">${[
      ['已保存候选线索',s.total,'▤','来自你的真实采集任务'],['高意向候选',s.high_intent,'↗','透明规则评分 80 分及以上'],['人工审核通过',s.approved,'✓','人工确认内容与使用范围'],['待同步飞书',s.pending_sync,'⇄','审核通过后可写入多维表格']
    ].map(([label,value,icon,note])=>`<div class="metric"><div class="metric-top">${label}<span class="metric-icon">${icon}</span></div><div class="metric-value">${number(value)}</div><p class="metric-note">${note}</p></div>`).join('')}</div><div class="grid-2"><div><section class="panel"><div class="panel-head"><div><h2>采集任务</h2><p>定时采集只在电脑服务运行期间执行</p></div>${button('全部任务','go-tasks','','small ghost')}</div>${tasks.length?`<div class="panel-body">${tasks.slice(0,5).map(t=>`<div class="list-row"><div><div class="row-title">${e(t.name)}</div><div class="row-sub">${e(t.industry||'自定义行业')} · ${e(list(t.regions).join('、')||'不限地区')} · ${e(intervalLabel(t.interval_minutes))}</div></div><div class="button-row">${tag(t.enabled?'定时启用':'定时暂停',t.enabled?'green':'')}${button('立即采集','run-task',idAttrs(t.id),'small')}</div></div>`).join('')}</div>`:empty('还没有采集任务','先添加一个允许访问的真实数据源，再设置地区与需求关键词。','new-task','创建第一条任务')}</section><section class="panel"><div class="panel-head"><div><h2>最近运行</h2><p>失败、无数据与去重结果均会留下记录</p></div>${button('查看日志','go-logs','','small ghost')}</div>${runs.length?`<div class="panel-body">${runs.slice(0,4).map(r=>`<div class="list-row"><div><div class="row-title">${e(r.task_name||'采集任务')}</div><div class="row-sub">${e(date(r.started_at))} · 新增 ${number(r.accepted)} 条 · 重复 ${number(r.duplicates)} 条</div></div>${runTag(r.status)}</div>`).join('')}</div>`:empty('尚未运行采集','创建任务后点击“立即采集”，这里会显示实际运行结果。')}</section></div><div><section class="panel"><div class="panel-head"><div><h2>真实数据源</h2><p>每个来源独立配置和确认访问许可</p></div>${button('管理来源','go-sources','','small ghost')}</div>${sources.length?`<div class="panel-body">${sources.slice(0,5).map(src=>`<div class="list-row"><div><div class="row-title">${e(src.name)}</div><div class="row-sub">${e(sourceKind(src.kind))} · ${e(src.platform||'公开来源')}</div></div>${sourceTag(src)}</div>`).join('')}</div>`:empty('等待连接真实来源','支持公开 RSS、允许采集的网页和获得使用授权的 JSON API。','new-source','添加数据源')}</section><section class="panel"><div class="panel-head"><h2>从发现到人工跟进</h2><span class="tag">4 个步骤</span></div><div class="panel-body"><div class="steps">${[
      ['连接许可来源','填写真实 URL，确认访问许可与采集范围。'],['设置需求条件','选择地区、行业、关键词和执行频率。'],['核对原文与来源','查看规则评分、原始链接，审核跟进话术。'],['人工跟进或同步','导出审核结果，或同步已配置的飞书表格。']
    ].map(([title,body],i)=>`<div class="step"><span class="step-num">${i+1}</span><div><h3>${title}</h3><p>${body}</p></div></div>`).join('')}</div><div class="notice mt-21">未配置合法评论 API 的小红书、抖音、视频号处于待连接状态。系统不会绕过登录、验证码或访问控制。</div></div></section></div></div><p class="footer-note">没有演示客资，也不会把测试内容写入你的业务数据库。</p>`;
}
function intervalLabel(minutes){return {60:'每小时',360:'每 6 小时',1440:'每天'}[minutes]||`每 ${minutes||60} 分钟`;}
function renderLeads() {
  const f=state.filters;
  const exportUrl='/api/export.csv?'+new URLSearchParams(f);
  return heading('线索管理','每条候选内容保留原始链接、原文、采集时间与判断理由。审核通过后，才可作为已审核客资使用。',`<a class="btn" href="${e(exportUrl)}">↓ 导出 CSV</a>`+button('刷新','refresh'))+
    `<form class="toolbar" data-form="filters">${field('搜索原文 / 标题','q',f.q,'search','','placeholder="需求关键词、来源内容…"')}${field('地区','region',f.region,'text','','placeholder="如：上海 / 浦东"')}${field('行业','industry',f.industry,'text','','placeholder="装修 / 保险"')}${select('最低评分','min_score',f.min_score,[['0','全部评分'],['50','50 分及以上'],['70','70 分及以上'],['85','85 分及以上']])}${select('人工审核','review_status',f.review_status,[['','全部审核状态'],['pending','待人工审核'],['approved','审核通过'],['rejected','已拒绝']])}${select('跟进状态','status',f.status,[['','全部跟进状态'],['new','未跟进'],['contacted','已人工跟进'],['closed','已结束']])}<button class="btn primary" type="submit">筛选</button>${button('重置','reset-filters','','small')}</form><div class="notice mb-18">评分仅用于排序，不代表需求已核实。请先打开来源核对原文、公开目的和联系许可；导出中的联系方式与话术仅供审核通过的记录使用。</div><section class="panel"><div class="panel-head"><h2>候选内容 <span class="muted tiny">${state.leads.length} 条</span></h2><span class="tiny muted">按当前筛选条件展示</span></div>${state.leads.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>真实内容</th><th>地区 / 来源</th><th>意向评分</th><th>人工审核</th><th>采集时间</th><th>操作</th></tr></thead><tbody>${state.leads.map(l=>`<tr><td class="wrap"><div class="title">${e(shorten(l.title||'未提供标题',65))}</div><div class="sub">${e(shorten(l.text,110))}</div></td><td><div>${e(l.region||'未识别地区')}</div><div class="sub">${e(l.platform||'公开来源')}</div><div class="sub">${e(l.source_type||'')}</div></td><td><div class="score">${number(l.score)}<span> / 100</span></div><div class="sub">${e(l.industry||'')}</div></td><td>${reviewTag(l.review_status)}<div class="sub">${statusTag(l.status)}</div></td><td><span class="tiny">${e(date(l.collected_at))}</span>${l.record_id?'<div class="sub">已同步飞书</div>':''}</td><td>${button('查看与审核','lead-detail',idAttrs(l.id),'small')}</td></tr>`).join('')}</tbody></table></div>`:empty('没有符合条件的真实线索','可调整筛选条件，或到采集任务执行一次采集。没有匹配需求的内容时，系统不会生成替代线索。')}</section>`;
}
function renderTasks() {
  return heading('采集任务','指定地区和需求词，在选定的许可来源中定时寻找匹配内容。电脑需保持服务运行。',button('＋ 新建任务','new-task','','primary'))+
    `<div class="notice mb-20">新增任务必须至少选择一个真实数据源。抓取为空、关键词未匹配、地区未命中或访问失败时，可在运行日志中查看结果。</div>${state.tasks.length?`<div class="card-list">${state.tasks.map(t=>`<section class="panel task-card"><div class="task-card-top"><div><h3>${e(t.name)}</h3><div class="tags">${tag(t.industry||'自定义行业')}${tag(t.enabled?'定时启用':'定时暂停',t.enabled?'green':'')}${tag(intervalLabel(t.interval_minutes))}</div></div><div class="button-row">${button('编辑','edit-task',idAttrs(t.id),'small')}${button(t.enabled?'暂停定时':'启用定时','toggle-task',idAttrs(t.id),'small')}${button('立即采集','run-task',idAttrs(t.id),'primary small')}</div></div><div class="task-card-meta"><span>地区：${e(list(t.regions).join('、')||'不限地区')}</span><span>最低保存分：${number(t.min_score)}</span><span>数据源：${list(t.source_ids).length} 个</span></div><div class="tags">${list(t.keywords).map(x=>tag(x,'green')).join('')||tag('未设置关键词','amber')}</div><p class="tiny muted">排除词：${e(list(t.exclude_keywords).join('、')||'无')} · 最近运行：${e(date(t.last_run_at||t.last_run))} · 下次计划：${e(date(t.next_run_at||t.next_run))}</p></section>`).join('')}</div>`:`<section class="panel">${empty('开始你的第一条真实采集任务','先配置来源，再填写地区、需求关键词和扫描频率。每次运行会保存实际结果与失败原因。','new-task','创建任务')}</section>`}`;
}
function renderSources() {
  return heading('真实数据源','每个来源由你提供真实 URL 并确认使用许可。系统遵守访问限制，不跨越登录与验证码。',button('＋ 添加数据源','new-source','','primary'))+
    `<section class="panel"><div class="panel-head"><h2>已配置来源 <span class="muted tiny">${state.sources.length} 个</span></h2><span class="tiny muted">RSS / 网页 / 授权 API / 公开搜索</span></div>${state.sources.length?`<div class="table-wrap"><table class="data-table sources-table"><thead><tr><th>来源名称与地址</th><th>类型 / 平台</th><th>连接状态</th><th>联系方式候选</th><th>操作</th></tr></thead><tbody>${state.sources.map(s=>`<tr><td class="wrap"><div class="title">${e(s.name)}</div><div class="sub">${link(s.url,shorten(s.url,95))}</div>${s.last_error?`<div class="run-error">${e(shorten(s.last_error,200))}</div>`:''}</td><td>${tag(sourceKind(s.kind))}<div class="sub">${e(s.platform||'公开来源')}</div></td><td>${sourceTag(s)}</td><td><span class="tiny">${s.contact_consent?'仅标记候选，需逐条复核':'不提取联系方式'}</span></td><td><div class="button-row">${button('编辑','edit-source',idAttrs(s.id),'small')}${button('删除','delete-source',idAttrs(s.id),'small danger')}</div></td></tr>`).join('')}</tbody></table></div>`:empty('还没有接入数据源','复制允许访问的 RSS 或网页 URL，或注册合法公开搜索 API。这里不会预置虚构来源。','new-source','添加第一条来源')}</section><div class="platforms">${['小红书','抖音','视频号'].map(p=>`<div class="platform"><strong>${p}</strong><div class="mt-7">${tag('未配置合法评论 API','amber')}</div><p>获得适用的 API 使用授权后，可配置 JSON API 数据源。当前不支持未经授权的全网评论抓取。</p></div>`).join('')}</div><div class="notice warning mt-20">公开可访问不等于允许商业采集。请核对网站条款、robots 规则和接口授权范围。来源的联系方式声明仅允许标记候选，不代表每条内容都允许联系。</div>`;
}
function renderLogs() {
  return heading('运行日志','展示实际采集、匹配、去重和失败结果。服务运行时每 10 秒更新。',button('刷新日志','refresh'))+renderSearchChannels(state.runs.find(r=>list(r.search_channels).length))+
    `<section class="panel"><div class="panel-head"><h2>最近采集记录</h2><span class="tiny muted">失败不会填入替代数据</span></div>${state.runs.length?state.runs.map(r=>`<article class="run-card"><div class="run-card-top"><div><h3>${e(r.task_name||`任务 ${r.task_id}`)}</h3><p class="run-time">开始：${e(date(r.started_at))} · 结束：${e(date(r.finished_at))} · 运行 #${e(r.id)}</p></div>${runTag(r.status)}</div><div class="run-numbers"><span>读取 <b>${number(r.fetched)}</b></span><span>新增 <b>${number(r.accepted)}</b></span><span>去重 <b>${number(r.duplicates)}</b></span><span>错误 <b>${list(r.errors).length}</b></span></div>${r.message?`<p class="tiny muted mt-10">${e(r.message)}</p>`:''}${list(r.errors).length?`<div class="run-error">${list(r.errors).map(x=>e(typeof x==='string'?x:JSON.stringify(x))).join('\n')}</div>`:''}</article>`).join(''):empty('暂无运行日志','首次执行真实采集后，运行状态与错误信息会保存在这里。')}</section>`;
}
function renderSearchChannels(run){
  if(!run)return '';
  return `<section class="panel mb-20"><div class="panel-head"><h2>最近一次公开互联网搜索渠道</h2><span class="tiny muted">授权搜索摘要，不是平台全量抓取</span></div><div class="panel-body">${list(run.search_channels).map(c=>`<div class="list-row"><div><strong>${e(c.name)}</strong><div class="sub">${e(c.query)}</div></div><span>${c.status==='success'?`返回 ${number(c.returned)} 条`:'失败'}</span></div>`).join('')}<p class="tiny muted">返回内容不等于客户。程序继续过滤广告、资讯和无具体需求内容；只有通过筛选才进入线索管理。</p></div></section>`;
}
function renderSettings() {
  const s=state.settings||{},f=s.feishu||{},connected=f.configured;
  return heading('飞书与本地设置','通过真实飞书 API 写入审核通过的记录。应用密钥只在本地填写，保存后不会回显。',button('刷新设置','refresh'))+
    `<div class="settings-grid"><section class="panel"><div class="panel-head"><div><h2>飞书多维表格</h2><p>需具有应用与目标表格的合法访问权限</p></div>${tag(connected?'已填写连接配置':'待连接',connected?'green':'amber')}</div><div class="panel-body"><form data-form="settings"><div class="section-label">① 应用授权</div><div class="form-grid">${field('应用 App ID','app_id',f.app_id||'','text','使用飞书开放平台自建应用。','autocomplete="off"')}${field('应用 App Secret','app_secret','','password',f.has_secret?'已保存；留空保留原密钥。':'请在本地填写；不会显示已保存值。','autocomplete="new-password"')}${field('或 Tenant Access Token','tenant_access_token','','password',f.has_token?'已保存；留空保留原 token。token 过期后需在本地更新。':'可用有效的租户访问 token；或填写上方 App ID 与 Secret。','autocomplete="new-password"')}</div><div class="section-label">② 目标表格</div><div class="form-grid">${field('多维表格 app_token','app_token',f.app_token||'','text','表格 URL 中的应用标识。','autocomplete="off"')}${field('数据表 table_id','table_id',f.table_id||'','text','表格 URL 中 table 参数的值。','autocomplete="off"')}</div><div class="notice">请给应用授予多维表格记录读写权限，并让应用有权访问该表格。表格字段、字段类型和注册清单见使用指南。保存配置后，再测试连接。</div>${checkbox('auto_sync','采集完成后自动同步审核通过的记录',!!f.auto_sync,'默认关闭。待审核、拒绝的记录不会作为已审核客资同步。')}<div class="section-label mt-23">③ 本地数据</div>${field('原始内容保留天数','retention_days',s.retention_days||90,'number','到期清理会移除旧线索；定期导出你需要保留的记录。','min="1" max="3650" required')}<div class="form-actions"><button class="btn primary" type="submit">保存配置</button></div></form><div class="privacy-divider"></div><div class="button-row">${button('测试飞书连接','feishu-check','','small')}${button('同步已审核记录','feishu-sync','','primary small')}</div><div id="feishu-result" class="status-text mt-14" role="status"></div></div></section><div><section class="panel"><div class="panel-head"><h2>手机访问</h2>${tag(s.lan_enabled?'局域网模式':'本机模式',s.lan_enabled?'green':'')}</div><div class="panel-body"><p class="status-text">电脑和手机连接同一局域网，电脑保持服务运行。手机打开电脑的局域网地址，使用同一管理员密码登录。</p><div class="link-list mt-14">${list(state.dashboard?.lan_urls).length?list(state.dashboard.lan_urls).map(url=>link(url,url)).join(''):'<p class="tiny muted">当前没有可用的局域网访问地址。按使用指南启用局域网启动模式。</p>'}</div><div class="notice warning mt-17">局域网采用 HTTP，请只在你信任的私人网络使用。请勿端口转发或把服务直接开放到公网。</div></div></section><section class="panel"><div class="panel-head"><h2>管理员密码</h2><span class="tag">单管理员</span></div><div class="panel-body"><form data-form="password">${field('当前密码','old_password','','password','','required autocomplete="current-password"')}${field('新密码','new_password','','password','至少 12 个字符；请勿使用平台账号密码。','required minlength="12" autocomplete="new-password"')}${field('再次输入新密码','confirm_password','','password','','required minlength="12" autocomplete="new-password"')}<button class="btn" type="submit">修改密码</button></form></div></section></div></div>`;
}
function renderPrivacy() {
  return heading('真实数据，明确边界','每一步都保留来源；自动触达关闭，人工核对后再决定跟进。')+
    `<div class="grid-2"><section class="panel"><div class="panel-head"><h2>采集与联系方式</h2>${tag('人工判断')}</div><div class="panel-body"><div class="privacy-list"><div><h3>只连接允许访问的真实来源</h3><p>支持公开 RSS、允许采集的网页以及合法授权的 API。网站的访问条款、robots 规则和接口授权范围都需要遵守。任务不会绕过验证码、登录和访问控制。</p></div><div><h3>不把公开信息都当成可联系客资</h3><p>只有内容主体主动为商业联系公开的联系方式，才可标记为候选。应结合原始内容核对目的、时效和联系许可；来源设置中的声明不能代替逐条审核。普通个人资料、隐藏信息与非自愿联系方式不应采集。</p></div><div><h3>保留依据，人工审核</h3><p>每条记录保存来源 URL、平台 / 内容类型、原文、采集时间和透明规则评分理由。地区未匹配、没有需求关键词或排除词命中时，任务不会保存为匹配线索。评分不保证对方是实际客户。</p></div><div><h3>话术由人确认，跟进由人执行</h3><p>系统根据内容生成可编辑草稿。请删除不准确的描述，核对服务能力和联系许可，再将记录设为“审核通过”。系统没有自动发送消息或批量触达功能。</p></div></div></div></section><section class="panel"><div class="panel-head"><h2>本地权限与数据管理</h2>${tag('单管理员')}</div><div class="panel-body"><div class="privacy-list"><div><h3>电脑与手机共享一份本地数据库</h3><p>数据库保存在电脑的 data 目录。手机浏览器通过局域网连接同一个服务；关闭电脑服务会停止访问和定时采集。当前提供单管理员登录，适合个人或小范围受信任使用。</p></div><div><h3>飞书凭证在本地填写</h3><p>不需要向聊天发送密码或密钥。应用 Secret 和访问 token 保存后不会回显；Windows 环境使用本机数据保护机制加密存储。只有配置权限正确、API 调用实际成功，才会记录同步结果。</p></div><div><h3>你可以删除和限定保存期限</h3><p>在记录详情中删除不应保留的内容，并在设置中限定原始内容保留天数。导出文件也含业务信息，请按同样标准存储。删除本地记录不会删除此前已经写入飞书的记录。</p></div><div><h3>不直接对公网开放</h3><p>请仅使用本机或可信局域网。局域网版本未提供公网所需的传输加密与多人角色管理。当前产品没有部署到公网。</p></div></div></div></section></div>`;
}

function openModal(title,body,subtitle='',wide=false) {
  lastFocus=document.activeElement;document.body.classList.add('modal-open');
  modalRoot.innerHTML=`<div class="modal-overlay"><section class="modal${wide?' wide':''}" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div class="modal-header"><div><h2 id="modal-title">${e(title)}</h2>${subtitle?`<p>${e(subtitle)}</p>`:''}</div><button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">✕</button></div><div class="modal-body"><div class="modal-error notice error" role="alert"></div>${body}</div></section></div>`;
  modalRoot.querySelector('input:not([type="checkbox"]),select,textarea,button')?.focus();
}
function closeModal() {modalRoot.replaceChildren();document.body.classList.remove('modal-open');if(lastFocus?.isConnected)lastFocus.focus();}
function modalError(message) {const target=modalRoot.querySelector('.modal-error');if(target){target.textContent=message;target.scrollIntoView({block:'nearest'});}else toast(message,true);}
async function taskForm(id) {
  if(state.page!=='tasks'){const results=await Promise.all([api('/api/tasks'),api('/api/sources')]);state.tasks=list(results[0]);state.sources=list(results[1]);}
  const existing=id?state.tasks.find(x=>String(x.id)===String(id)):null;
  if(id&&!existing)throw new Error('此任务已不在当前列表，请刷新后再试。');
  const t=existing||await loadProfile();
  const sourceIds=list(t.source_ids).map(String);
  const hasAllowedSource=state.sources.some(sourceSelectable);
  const sourceNotice=hasAllowedSource?'':`<div class="notice warning mb-20">还需要接入一个你获准使用的真实数据源，才能创建并运行任务。${button('先添加真实来源','new-source','','small mt-10')}</div>`;
  openModal(id?'编辑采集任务':'新建采集任务',`<form data-form="task" data-id="${e(id||'')}">${sourceNotice}<div class="form-grid">
    ${field('任务名称','name',t.name||'','text','','required maxlength="100" placeholder="例如：泸州保险需求"')}
    ${field('行业','industry',t.industry||'保险','text','','required maxlength="60" list="industry-options"')}
    <datalist id="industry-options"><option value="保险"><option value="装修"><option value="自定义行业"></datalist>
    ${select('线索类型','lead_mode',t.lead_mode||'consumer',[['consumer','个人求助（求推荐、求报价）'],['business','企业商机（招标、采购、承保公告）']])}
    <div class="full notice">个人求助：要求原文有"求推荐/求报价"等求助语气。企业商机：匹配公开发布的采购、承保、中标公告，公告里的联系方式以原文页面为准，雷达不代为提取。</div>
    <div class="full">${textarea('地区（多个地区用换行或逗号分隔）','regions',list(t.regions).join('\n'),'必填，按原文匹配，可填城市或区县。','required rows="2" placeholder="泸州\n泸州市"')}</div>
    <div class="full">${textarea('意向关键词','keywords',list(t.keywords).join('\n'),'填写与保险配置需求相关的词。多个关键词用换行或逗号分隔。','required rows="3" placeholder="保险\n投保\n保障方案"')}</div>
    <div class="full">${textarea('排除关键词','exclude_keywords',list(t.exclude_keywords).join('\n'),'命中任一排除词的内容不会保存。','rows="2" placeholder="招聘\n招募\n加盟"')}</div>
    ${select('定时频率','interval_minutes',t.interval_minutes||1440,[[60,'每小时'],[360,'每 6 小时'],[1440,'每天']])}
    ${select('每次公开搜索组数','search_budget',t.search_budget||6,[[1,'1 组（节省额度）'],[3,'3 组'],[6,'6 组（推荐）'],[12,'12 组（扩大检索）']])}
    <div class="full">${checkbox('domestic_search','国内网页优先＋论坛、问答定向搜索',t.domestic_search??true,'交替搜索广泛网页和指定网站的索引摘要；使用你已配置并授权的搜索服务，不代表网站接口已授权或全部帖子已覆盖。')}${textarea('定向搜索网站（每行一个域名，可增删）','search_domains',list(t.search_domains??['tieba.baidu.com','zhidao.baidu.com','xiaomayi.net']).join('\n'),'预填百度贴吧、百度知道、泸州小蚂蚁。仅查搜索索引，不直接抓站点正文。小红书、抖音、视频号仍待适用授权。','rows="3"')}</div>
    <div class="full notice">每组分别搜索具体求助表达，分配覆盖已填写地区。Tavily 每组使用一次 basic 搜索额度（每天 6 组约每月 180 次）；必应国内 RSS 免密钥不计额度。扩大到其他城市，可在“地区”中添加。仅保留具体求助候选；没有就显示没有。</div>
    ${field('最低保存评分','min_score',t.min_score??60,'number','评分为透明规则判断，可在记录中查看理由。','required min="0" max="100" step="1"')}
    <div class="full"><label class="field-label">选择真实数据源（至少 1 个）</label><div class="source-picker">${state.sources.length?state.sources.map(s=>`<label class="check"><input type="checkbox" name="source_ids" value="${e(s.id)}" ${sourceIds.includes(String(s.id))?'checked':''} ${sourceSelectable(s)?'':'disabled'}><span>${e(s.name)}<small>${e(sourceKind(s.kind))} · ${e(sourceSelectionNote(s))}</small></span></label>`).join(''):'<p class="help">尚未配置来源。上方“先添加真实来源”可进入连接设置。</p>'}</div></div>
    <div class="full mt-17">${textarea('你的服务简介（用于话术草稿）','business_intro',t.business_intro||'','请填写真实服务范围，草稿仍需人工审核。','rows="2" placeholder="在四川泸州提供保险解决方案。"')}</div>
    </div>${checkbox('enabled','启用定时采集',t.enabled??false,'只在电脑本地服务运行时执行；可随时暂停。')}
    <div class="form-actions">${button('取消','close-modal')}<button class="btn primary" type="submit" ${hasAllowedSource?'':'disabled'}>${id?'保存任务':'创建任务'}</button></div></form>`,'只采集选中的真实来源，执行结果会留在运行日志中。');
}
function sourceForm(id) {
  const s=state.sources.find(x=>String(x.id)===String(id))||{};
  const kind=s.kind||'bing_rss';
  const defaultName=kind==='tavily_search'?'泸州保险公开需求搜索':kind==='bing_rss'?'必应国内公开需求搜索':kind==='volc_search'?'火山方舟联网搜索':kind==='baidu_search'?'百度AI搜索':'';
  const defaultPlatform=kind==='tavily_search'?'Tavily Search':kind==='brave_search'?'Brave Search':kind==='bing_rss'?'必应国内版':kind==='volc_search'?'火山方舟':kind==='baidu_search'?'百度智能云':'';
  const fixedUrl={tavily_search:'https://api.tavily.com/search',brave_search:'https://api.search.brave.com/res/v1/web/search',bing_rss:'https://cn.bing.com/search',volc_search:'https://ark.cn-beijing.volces.com/api/v3/responses',baidu_search:'https://qianfan.baidubce.com/v2/ai_search/web_search'}[kind];
  openModal(id?'编辑数据源':'添加真实数据源',`<form data-form="source" data-id="${e(id||'')}" data-original-kind="${e(s.kind||kind)}" data-has-token="${s.has_token?'true':'false'}"><div class="form-grid">
    ${field('来源名称','name',s.name||defaultName,'text','','required maxlength="100" placeholder="便于你辨认的来源名称"')}
    ${select('来源类型','kind',kind,[['volc_search','火山方舟联网搜索（字节系中文内容·推荐）'],['baidu_search','百度AI搜索（百度官方·中文网页索引全）'],['bing_rss','必应国内公开搜索 RSS（免密钥）'],['tavily_search','Tavily 公开搜索 API'],['rss','公开 RSS'],['web','允许采集的公开网页'],['json_api','合法授权的 JSON API'],['brave_search','高级：Brave（另需书面存储许可）']])}
    <div class="full">${field('真实来源 URL','url',fixedUrl||s.url||'','url','搜索接口地址由程序固定；其他来源只接受 HTTP / HTTPS。','required placeholder="https://…"')}</div>
    ${field('平台名称','platform',s.platform||defaultPlatform,'text','如论坛名称、网站名称或授权平台。','required maxlength="80"')}
    ${field('内容类型','source_type',isSearchSource(kind)?'搜索摘要（需人工核实原文）':s.source_type||'','text','搜索结果只是摘要，请打开原文确认需求。','maxlength="80"')}
    <div class="full" data-source-part="web">${field('正文 CSS 选择器（可选）','body_selector',s.body_selector||'','text','用于限定正文范围；留空读取页面可见文本。','placeholder="例如：article 或 .post-content"')}</div>
    <div class="full" data-source-part="json_api tavily_search brave_search volc_search baidu_search">
      ${field('API 密钥 / Bearer Token','api_token','','password',s.has_token?'已保存；留空保留原密钥。不会回显已保存值。':'将密钥仅填在本机，勿发到聊天。Windows 下加密保存。','autocomplete="new-password"')}
      ${field('或密钥环境变量名（可选）','api_token_env',s.api_token_env||'','text','也可在电脑环境变量中配置真实密钥，这里只填变量名。不要把密钥填进 URL。','placeholder="例如：TAVILY_API_KEY" autocomplete="off"')}
    </div>
    <div class="full" data-source-part="volc_search">
      ${field('方舟接入点 ID（ep- 开头）','model_endpoint',s.model_endpoint||'','text','火山方舟控制台 → 模型推理 → 接入点，复制 ep- 开头的 ID。联网搜索走该接入点。','required placeholder="ep-20260622001208-xxxxx" autocomplete="off"')}
      <div class="notice">使用你已开通联网搜索插件的火山方舟账号。密钥只填在本机；调用消耗方舟 token 与联网插件额度（有免费额度）。仅查找普通公开内容；不得搜索病史、健康险保单，或用于未经邀请的营销。程序只采纳搜索引擎真实抓取的引用链接，不会把模型自编文字当作来源。</div>
    </div>
    <div class="full" data-source-part="json_api tavily_search brave_search bing_rss volc_search baidu_search">
      ${checkbox('api_authorized','我已获得该 API 的合法使用授权',!!s.api_authorized,'确认调用权限、数据使用目的与接口限额。必应 RSS 为微软官方公开输出，勾选即承诺遵守其服务条款。')}
    </div>
    <div class="full" data-source-part="baidu_search"><div class="notice">使用你已开通 AI 搜索的百度智能云账号，粘贴 bce-v3 开头的 API Key。密钥只填在本机；按调用量后付费、不用不花钱（有免费额度）。仅查找普通公开内容；不得搜索病史、健康险保单，或用于未经邀请的营销。程序只采纳百度真实抓取的引用链接，不会把模型自编文字当作来源。</div></div>
    <div class="full" data-source-part="bing_rss"><div class="notice">必应国内版官方 RSS 输出（cn.bing.com），无需注册和密钥，中文网页索引更全。仅查找普通公开的咨询需求；不得搜索病史、健康险保单，或用于未经邀请的营销。请遵守微软服务条款；保存和使用摘要前仍需核实原文许可；摘要不代表已确认客户，也不提取联系方式。</div></div>
    <div class="full" data-source-part="tavily_search"><div class="notice">先到 ${link('https://app.tavily.com/','Tavily 官方网站')} 注册，选择免费额度，体验无需绑卡或选付费。密钥只填在本机。仅查找普通公开的保险咨询需求；不得搜索病史、健康险保单，或用于未经邀请的营销。保存和使用摘要前，仍需核实原文许可；摘要不代表已确认客户，也不提取联系方式。</div></div>
    <div class="full" data-source-part="brave_search"><div class="notice warning">高级来源：普通 Brave API 注册或付费不能替代保存搜索结果的许可。需要另行取得允许将结果保存到本地数据库的书面授权；未获该许可时请使用 Tavily 或其他获准来源。这里仅保存候选摘要，不提取联系方式。</div>
      ${checkbox('storage_authorized','我已另外取得 Brave 搜索结果存储的书面许可',!!s.storage_authorized,'仅有 API 密钥、普通账号或一般调用权限不满足此条件。')}
      ${textarea('书面存储许可说明','storage_authorization_note',s.storage_authorization_note||'','填写授权文件名称、授权主体、日期及许可保存范围（10–500 字）；不要填写密码或密钥。','rows="3" minlength="10" maxlength="500"')}
    </div>
    </div><div class="notice warning mt-12">请先确认该来源允许你访问和按此目的采集。网页的 robots 规则与访问限制会影响采集结果；公开页面并不自动代表允许商业使用。</div>
    ${checkbox('permission_confirmed','我已确认允许访问及本次采集用途',!!s.permission_confirmed,'不绕过登录、验证码或访问控制；遵守来源条款、robots 规则与授权范围。','required')}
    ${checkbox('contact_consent','该来源明确提供内容主体自愿公开的商业联系方式',!!s.contact_consent,'可选；公开搜索来源禁用。其他来源开启后仅标记候选，每条记录仍需复核。')}
    <div class="form-actions">${button('取消','close-modal')}<button class="btn primary" type="submit">保存来源</button></div></form>`,'请输入你有权使用的来源；密钥只在本地填写且不会回显。');
  toggleSourceParts();
}
function toggleSourceParts() {
  const kind=modalRoot.querySelector('[name="kind"]')?.value;
  modalRoot.querySelectorAll('[data-source-part]').forEach(part=>{part.hidden=!part.dataset.sourcePart.split(' ').includes(kind);part.querySelectorAll('input,textarea').forEach(input=>{input.disabled=part.hidden;});});
  const authorization=modalRoot.querySelector('[name="api_authorized"]');if(authorization)authorization.required=['json_api','tavily_search','brave_search','bing_rss','volc_search','baidu_search'].includes(kind);
  const storage=modalRoot.querySelector('[name="storage_authorized"]'),storageNote=modalRoot.querySelector('[name="storage_authorization_note"]');
  if(storage)storage.required=kind==='brave_search';if(storageNote)storageNote.required=kind==='brave_search';
  const search=isSearchSource(kind),fixedUrls={tavily_search:'https://api.tavily.com/search',brave_search:'https://api.search.brave.com/res/v1/web/search',bing_rss:'https://cn.bing.com/search',volc_search:'https://ark.cn-beijing.volces.com/api/v3/responses',baidu_search:'https://qianfan.baidubce.com/v2/ai_search/web_search'};
  const url=modalRoot.querySelector('[name="url"]'),contact=modalRoot.querySelector('[name="contact_consent"]'),type=modalRoot.querySelector('[name="source_type"]'),platform=modalRoot.querySelector('[name="platform"]'),name=modalRoot.querySelector('[name="name"]');
  if(url){url.readOnly=search;if(search)url.value=fixedUrls[kind]||'';else if(Object.values(fixedUrls).includes(url.value))url.value='';}
  if(contact){contact.disabled=search;if(search)contact.checked=false;}
  if(type){type.readOnly=search;if(search)type.value='搜索摘要（需人工核实原文）';else if(type.value==='搜索摘要（需人工核实原文）')type.value='';}
  if(platform){const defaultPlatforms={tavily_search:'Tavily Search',brave_search:'Brave Search',bing_rss:'必应国内版',volc_search:'火山方舟',baidu_search:'百度智能云'};if(search&&(!platform.value||Object.values(defaultPlatforms).includes(platform.value)))platform.value=defaultPlatforms[kind]||'';else if(!search&&Object.values(defaultPlatforms).includes(platform.value))platform.value='';}
  if(name){const defNames={tavily_search:'泸州保险公开需求搜索',bing_rss:'必应国内公开需求搜索',volc_search:'火山方舟联网搜索',baidu_search:'百度AI搜索'},defName=defNames[kind]||'';if(defName&&!name.value)name.value=defName;else if(!defName&&Object.values(defNames).includes(name.value))name.value='';}
}
function leadForm(id) {
  const l=state.leads.find(x=>String(x.id)===String(id));if(!l)throw new Error('此记录已不在当前列表，请刷新后再试。');
  const searchContent=isSearchSource(l.source_kind)||String(l.source_type||'').includes('搜索摘要');
  const contact=searchContent?'':typeof l.contact==='string'?l.contact:l.contact?JSON.stringify(l.contact):'';
  const followupPermission=searchContent?checkbox('followup_permission_confirmed','我已核实原文明确邀请咨询回复或商业联系，并有权使用这条内容',!!l.followup_permission_confirmed,'只有明确邀请的内容才能审核通过；审核通过不会自动联系对方。'):'';
  openModal('线索来源核对与人工审核',`<div class="detail-grid"><section>
    <div class="tags">${tag(l.platform||'公开来源')}${tag(l.source_type||'真实内容')}${tag(`${l.score??0} / 100`,'green')}</div>
    <h3 class="detail-title">${e(l.title||'原始内容')}</h3><div class="detail-label">原文 / 搜索摘要（摘要需打开来源核实）</div><div class="original-text">${e(l.text||'未提供原文')}</div>
    <div class="detail-meta"><div>地区：${e(l.region||'未识别地区')} · 行业：${e(l.industry||'—')}</div><div>来源发布时间：${e(date(l.published_at))}</div><div>采集时间：${e(date(l.collected_at))}</div><div>最近出现：${e(date(l.last_seen_at))}</div><div>${link(l.url,'打开原始来源')}</div>${l.record_id?`<div>飞书记录 ID：${e(l.record_id)}</div>`:''}</div>
    <div class="detail-label">规则评分理由</div>${list(l.reasons).length?`<ul class="reasons">${list(l.reasons).map(x=>`<li>${e(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ul>`:'<p class="help">此记录没有评分理由。</p>'}
    <div class="detail-label">联系方式待核对（未审核前不导出）</div><div class="detail-contact">${searchContent?'搜索摘要不提取联系方式。':contact?e(contact):'未记录符合条件的公开商业联系方式。'}</div>${!searchContent&&l.contact_source?`<p class="help">联系方式依据：${safeUrl(l.contact_source)?link(l.contact_source,'查看联系方式来源'):e(l.contact_source)}</p>`:''}<p class="help">请打开来源确认由本人主动用于商业联系公开；没有许可时不要使用。</p>
    </section><section><form data-form="lead" data-id="${e(l.id)}" data-search-content="${searchContent?'true':'false'}">
    ${textarea('个性化跟进草稿','draft',l.draft||'','草稿不会自动发送。核对内容、服务能力与联系许可后再通过审核。','class="draft-text" rows="9"')}
    ${followupPermission}
    ${select('人工审核状态','review_status',l.review_status||'pending',[['pending','待人工审核'],['approved','审核通过'],['rejected','拒绝使用']])}
    ${select('人工跟进状态','status',l.status||'new',[['new','未跟进'],['contacted','已人工跟进'],['closed','已结束']])}
    ${textarea('内部备注','notes',l.notes||'','备注仅用于你的业务判断。','rows="3"')}
    <div class="notice">“审核通过”表示你已核对真实内容与使用范围；不会自动联系对方。搜索摘要必须另行确认原文明示邀请及使用许可，才可导出话术或同步飞书。</div>
    <div class="form-actions">${button('取消','close-modal')}<button class="btn primary" type="submit">保存审核结果</button></div><div class="privacy-divider"></div>${button('删除此本地记录','delete-lead',idAttrs(l.id),'small danger')}<p class="help">已同步飞书的记录不会随本地删除自动移除。</p>
    </form></section></div>`,'判断结果只用于辅助，请以原文与实际需求为准。',true);
}

async function handleAction(action,target) {
  const id=target.dataset.id;
  if(action==='navigate'){closeModal();return loadPage(target.dataset.page);}
  if(action==='menu'){state.menu=!state.menu;document.querySelector('.shell')?.classList.toggle('menu-open',state.menu);return;}
  if(action==='refresh')return loadPage();
  if(action.startsWith('go-'))return loadPage(action.slice(3));
  if(action==='close-modal'){closeModal();return;}
  if(action==='logout'){await api('/api/logout','POST',{});state.session=null;clearInterval(pollTimer);closeModal();renderLogin();return;}
  if(action==='new-task'||action==='edit-task')return taskForm(id);
  if(action==='new-source'){if(state.page!=='sources')state.sources=list(await api('/api/sources'));return sourceForm();}
  if(action==='edit-source')return sourceForm(id);
  if(action==='lead-detail')return leadForm(id);
  if(action==='reset-filters'){state.filters={q:'',region:'',industry:'',min_score:'0',status:'',review_status:''};return loadPage('leads');}
  if(action==='run-task'){target.disabled=true;try{const result=await api(`/api/tasks/${encodeURIComponent(id)}/run`,'POST',{});toast(`已启动真实采集${result?.run_id?'，运行 #'+result.run_id:''}。可在运行日志查看结果。`);await loadPage(state.page,true);}finally{if(target.isConnected)target.disabled=false;}return;}
  if(action==='toggle-task'){const task=state.tasks.find(x=>String(x.id)===String(id));await api(`/api/tasks/${encodeURIComponent(id)}`,'PATCH',{enabled:!task.enabled});toast(task.enabled?'已暂停定时任务。':'已启用定时任务。');return loadPage('tasks',true);}
  if(action==='delete-source'){if(!confirm('删除此数据源？引用它的任务可能无法继续采集，请先核对相关任务。'))return;await api(`/api/sources/${encodeURIComponent(id)}`,'DELETE');toast('数据源已删除。');return loadPage('sources',true);}
  if(action==='delete-lead'){if(!confirm('删除这条本地线索及原文？已经同步到飞书的记录需在飞书单独删除。'))return;await api(`/api/leads/${encodeURIComponent(id)}`,'DELETE');closeModal();toast('本地记录已删除。');return loadPage('leads',true);}
  if(action==='feishu-check'||action==='feishu-sync'){
    target.disabled=true;const output=document.getElementById('feishu-result');if(output)output.textContent=action==='feishu-check'?'正在通过飞书 API 测试连接…':'正在同步已审核记录…';
    try{
      const result=await api(action==='feishu-check'?'/api/feishu/check':'/api/feishu/sync','POST',{});
      let message,isError=false;
      if(action==='feishu-check'){
        const missing=list(result?.missing_fields),wrong=list(result?.type_errors);isError=result?.ok===false||missing.length>0||wrong.length>0;
        message=isError?'飞书 API 可访问，但表格字段检查未通过。':'飞书 API 连接与表格字段检查通过。';
        if(missing.length)message+='\n缺少字段：'+missing.join('、');
        if(wrong.length)message+='\n类型不符：'+wrong.map(x=>`${x.field_name}（需要类型 ${x.expected}，当前 ${x.actual}）`).join('；');
      }else{
        isError=Number(result?.failed)>0||list(result?.errors).length>0;
        message=`同步完成：成功 ${number(result?.synced??0)} 条，失败 ${number(result?.failed??0)} 条，跳过 ${number(result?.skipped??0)} 条。`;
        if(list(result?.errors).length)message+='\n'+list(result.errors).map(x=>typeof x==='string'?x:JSON.stringify(x)).join('\n');
      }
      if(output){output.textContent=message;output.className='status-text'+(isError?' form-error':'');}toast(message,isError);
    }catch(error){if(output){output.textContent=error.message;output.className='status-text form-error';}throw error;}finally{if(target.isConnected)target.disabled=false;}return;
  }
}

async function handleSubmit(form,event) {
  event.preventDefault();const type=form.dataset.form;const data=new FormData(form);const get=name=>String(data.get(name)??'').trim();
  const submit=form.querySelector('button[type="submit"]');if(submit)submit.disabled=true;
  try {
    if(type.startsWith('geo-')){await submitGeo(type,form,data);return;}
    if(type==='login'){const session=await api('/api/login','POST',{password:String(data.get('password')??'')});state.session=session;startPolling();await loadPage('dashboard');return;}
    if(type==='filters'){state.filters={q:get('q'),region:get('region'),industry:get('industry'),min_score:get('min_score'),status:get('status'),review_status:get('review_status')};await loadPage('leads');return;}
    if(type==='task'){
      const sourceIds=data.getAll('source_ids').map(x=>Number(x));if(!sourceIds.length)throw new Error('请至少选择一个已确认访问许可的真实数据源。');
      const keywords=terms(get('keywords'));if(!keywords.length)throw new Error('请至少填写一个需求关键词。');
      const payload={name:get('name'),industry:get('industry'),lead_mode:get('lead_mode')||'consumer',regions:terms(get('regions')),keywords,exclude_keywords:terms(get('exclude_keywords')),interval_minutes:Number(get('interval_minutes')),enabled:data.has('enabled'),min_score:Number(get('min_score')),search_budget:Number(get('search_budget')||6),domestic_search:data.has('domestic_search'),search_domains:terms(get('search_domains')),source_ids:sourceIds,business_intro:get('business_intro')};
      await api('/api/tasks'+(form.dataset.id?'/'+encodeURIComponent(form.dataset.id):''),form.dataset.id?'PATCH':'POST',payload);closeModal();toast('任务已保存。现在可以执行真实采集。');await loadPage('tasks');return;
    }
    if(type==='source'){
      const url=safeUrl(get('url'));if(!url)throw new Error('请填写有效的 HTTP 或 HTTPS 来源地址。');
      const search=isSearchSource(get('kind'));
      const hasMatchingToken=form.dataset.hasToken==='true'&&form.dataset.originalKind===get('kind');
      if(search&&get('kind')!=='bing_rss'&&!get('api_token')&&!get('api_token_env')&&!hasMatchingToken)throw new Error('公开搜索需要对应平台的合法 API 密钥；切换平台后请重新填写密钥或环境变量名。');
      const storageNote=get('storage_authorization_note');
      if(get('kind')==='brave_search'&&(!data.has('storage_authorized')||storageNote.length<10||storageNote.length>500))throw new Error('Brave 需要另行取得搜索结果存储的书面许可，并填写 10–500 字许可说明。普通注册或 API 密钥不能替代此许可。');
      const payload={name:get('name'),kind:get('kind'),url,platform:get('platform'),source_type:search?'搜索摘要（需人工核实原文）':get('source_type')||sourceKind(get('kind')),permission_confirmed:data.has('permission_confirmed'),contact_consent:search?false:data.has('contact_consent'),body_selector:get('body_selector'),api_token_env:get('api_token_env'),api_authorized:data.has('api_authorized'),storage_authorized:get('kind')==='brave_search'&&data.has('storage_authorized'),storage_authorization_note:get('kind')==='brave_search'?storageNote:''};
      if(get('api_token'))payload.api_token=get('api_token');
      await api('/api/sources'+(form.dataset.id?'/'+encodeURIComponent(form.dataset.id):''),form.dataset.id?'PATCH':'POST',payload);closeModal();toast('真实数据源已保存。请在采集任务中选择它。');await loadPage('sources');return;
    }
    if(type==='lead'){
      const payload={draft:get('draft'),review_status:get('review_status'),status:get('status'),notes:get('notes')};
      if(form.dataset.searchContent==='true'){
        payload.followup_permission_confirmed=data.has('followup_permission_confirmed');
        if(payload.review_status==='approved'&&!payload.followup_permission_confirmed)throw new Error('请先核实原文明确邀请咨询回复或商业联系，并确认有权使用这条内容，再勾选确认。');
      }
      await api(`/api/leads/${encodeURIComponent(form.dataset.id)}`,'PATCH',payload);closeModal();toast('审核结果已保存。系统不会自动触达。');await loadPage('leads',true);return;
    }
    if(type==='settings'){
      const feishu={app_id:get('app_id'),app_token:get('app_token'),table_id:get('table_id'),auto_sync:data.has('auto_sync')};
      if(get('app_secret'))feishu.app_secret=get('app_secret');if(get('tenant_access_token'))feishu.tenant_access_token=get('tenant_access_token');
      await api('/api/settings','POST',{feishu,retention_days:Number(get('retention_days'))});toast('配置已保存在本地。请测试飞书连接。');await loadPage('settings');return;
    }
    if(type==='password'){const newPassword=String(data.get('new_password')??'');if(newPassword!==String(data.get('confirm_password')??''))throw new Error('两次输入的新密码不一致。');await api('/api/password','POST',{old_password:String(data.get('old_password')??''),new_password:newPassword});state.session=null;clearInterval(pollTimer);closeModal();renderLogin();toast('密码已修改，请使用新密码重新登录。');return;}
  }catch(error){
    if(type==='login'){const target=form.querySelector('.auth-error');target.textContent=error.message;}
    else if(modalRoot.contains(form))modalError(error.message);
    else toast(error.message,true);
  }finally{if(submit?.isConnected)submit.disabled=false;}
}

document.addEventListener('click',event=>{
  const target=event.target.closest('[data-action]');
  if(target){event.preventDefault();handleAction(target.dataset.action,target).catch(error=>toast(error.message,true));}
  else if(event.target.classList.contains('modal-overlay'))closeModal();
  else if(state.menu && !event.target.closest('.sidebar') && !event.target.closest('.menu-toggle')){state.menu=false;document.querySelector('.shell')?.classList.remove('menu-open');}
});
document.addEventListener('submit',event=>{const form=event.target.closest('form[data-form]');if(form)handleSubmit(form,event);});
document.addEventListener('change',event=>{
  if(event.target.name==='kind'&&modalRoot.contains(event.target)){
    const token=modalRoot.querySelector('[name="api_token"]'),tokenEnv=modalRoot.querySelector('[name="api_token_env"]');
    if(token)token.value='';if(tokenEnv)tokenEnv.value='';
    ['api_authorized','permission_confirmed','storage_authorized'].forEach(name=>{const input=modalRoot.querySelector(`[name="${name}"]`);if(input)input.checked=false;});
    toggleSourceParts();
  }
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){closeModal();state.menu=false;document.querySelector('.shell')?.classList.remove('menu-open');}
  if(event.key==='Tab'&&modalRoot.childElementCount){const nodes=[...modalRoot.querySelectorAll('button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),a[href]')].filter(n=>n.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}
});
(async()=>{try{state.session=await api('/api/session');startPolling();await loadPage('dashboard');}catch(error){if(!state.session)renderLogin(error.status===401?'':error.message);}})();
