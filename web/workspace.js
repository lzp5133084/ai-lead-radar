'use strict';
function renderGuide(){
  const d=state.guide||{}, p=d.program||{};
  const table=(headers,rows)=>`<div class="table-wrap"><table class="data-table"><thead><tr>${headers.map(h=>`<th>${e(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${headers.length}">尚无记录</td></tr>`}</tbody></table></div>`;
  return heading('使用与运行说明','看懂采集怎么运行、何时运行、实际搜过哪些网站。',button('刷新运行信息','refresh'))+
  `<div class="notice mb-20">程序文件大小（不含业务数据库）：${number((p.bytes||0)/1048576)} MB · 最近程序文件修改：${e(date(p.modified_at))} · 作者：${e(p.author)} · 微信：${e(p.wechat)}<br>本次服务启动：${e(date(d.service_started_at))}。时间显示为当前浏览器本地时间。</div>
  <section class="panel mb-20"><div class="panel-head"><h2>第一次使用，只需这几步</h2></div><div class="panel-body"><ol><li>添加你获准使用的真实来源，在本机填写密钥。</li><li>设置地区、需求词和排除词，选择至少一个来源。</li><li>点击“立即采集”，到运行日志看实际结果。</li><li>打开候选的原始链接，核实需求、日期和联系意愿；人工审核后再跟进或导出。</li><li>需要每天运行，就启用任务定时，并保持电脑和本地服务运行。</li></ol><p>0 条表示没有符合条件的候选；失败会记录原因，不会生成替代数据。关闭电脑、睡眠、断网或退出服务会影响采集。错过的周期不会集中补跑：恢复后到期任务执行一次，再按周期安排下一次。</p><p>手动采集也会将下次计划重排为“此次开始时间＋周期”；正在运行的任务不会重复启动。实际开始可能略晚于计划，后台约每 5 秒检查一次。</p></div></section>
  <section class="panel mb-20"><div class="panel-head"><h2>你的采集时间与周期</h2></div>${table(['任务','周期 / 状态','最近开始','下次计划','每次组数'],list(d.tasks).map(t=>`<tr><td>${e(t.name)}</td><td>${e(intervalLabel(t.interval_minutes))} · ${t.enabled?'启用':'暂停'}</td><td>${e(date(t.last_run_at))}</td><td>${t.enabled?e(date(t.next_run_at)):'已暂停'}</td><td>${number(t.search_budget||6)}</td></tr>`))}<div class="panel-body"><p>每小时 / 每 6 小时 / 每天分别是间隔 60 / 360 / 1440 分钟，不是固定整点。每组 Tavily basic 搜索消耗一次额度；6 组每天一次、一个来源约每月 180 次。多个任务、来源和手动执行会增加用量。</p></div></section>
  <section class="panel mb-20"><div class="panel-head"><h2>搜索与网站历史</h2></div><div class="panel-body"><h3>历史定向搜索过的域名</h3><p>${e(list(d.targeted_sites).join('、')||'暂无定向搜索记录')}</p><p>这是查询范围，不表示抓取全站。国内网页优先也不是新增国内搜索供应商，当前索引搜索仍使用已有授权服务。</p><h3>真正返回并进入筛选的网站（新版开始记录）</h3></div>${table(['域名','返回内容累计','最近一次结束'],list(d.sites).map(s=>`<tr><td>${e(s.domain)}</td><td>${number(s.returned)}</td><td>${e(date(s.last_at))}</td></tr>`))}<div class="panel-body"><h3>曾保存候选内容的来源网站</h3><p>${e(list(d.saved_sites).join('、')||'暂无已保存候选来源')}</p><p>含待审与已拒绝历史，不代表客户或可联系客资。历史旧版未记录完整返回域名，无法追补。统计依据当前保留的最多 ${number(d.history_limit)} 次运行；跨次重复返回会重复计数，采集时间不是原文发布时间。</p></div></section>
  <section class="panel"><div class="panel-head"><h2>运行检查和常见问题</h2></div><div class="panel-body"><p>打不开：先启动程序，再打开 http://127.0.0.1:8765/，不要在地址后加星号。手机须启用局域网启动模式，电脑和手机连接同一可信网络。</p><p>有内容但无候选：可能是广告、资讯、地区不符、缺少具体求助表达、敏感内容或分数不符。返回数量不是客户数量。</p><p>认证、限额、登录或验证码错误：查看运行日志；更正本机凭证或来源授权，不自动充值，不绕过验证。</p><p>小红书、抖音、视频号的全网评论未接通。飞书仅在配置正确、接口成功且记录经审核后同步。自动触达始终关闭。</p><p>GEO：先完善真实身份和服务资料，生成草稿并审核，发布后记录公开链接，再记录 AI 实际引用测试。只放在本机的文件不会自动被外部 AI 搜到。</p></div></section>`;
}

function renderGeo(){
  const d=state.geo||{}, p=d.profile||{}, g=d.generated||{};
  return heading('GEO 内容工作台','让真实服务资料更清晰、更便于搜索和引用。商家与个人、保险与其他行业均可调整。')+
  `<div class="notice warning mb-20">这是资料与内容模板工具，当前没有调用大模型。不会保证 AI 推荐、排名或收录；没有发布到公网，不会自动改变外部 AI 回答。不要伪造资质、客户评价或平台背书。</div>
  <section class="panel mb-20"><div class="panel-head"><h2>① 真实服务资料</h2><span class="tiny muted">上次保存：${e(date(p.updated_at))}</span></div><div class="panel-body"><form data-form="geo-profile"><div class="form-grid">
  ${field('个人或商家名称','name',p.name||'','text','','required')}${select('主体类型','entity_type',p.entity_type||'Person',[['Person','个人'],['Organization','商家 / 机构']])}
  ${field('行业类目','industry',p.industry||'','text','例如保险、装修、摄影。','required')}${field('真实服务地区','regions',p.regions||'','text','省、市、区县；不要把未覆盖地区写成已服务。','required')}
  <div class="full">${textarea('真实服务介绍','service',p.service||'','不要使用保收益、最强、官方指定等无依据表述。','required rows="3"')}</div>
  ${field('自愿公开的咨询方式','contact',p.contact||'','text','这里只填你愿意公开的业务联系渠道。')}${field('真实介绍页网址（可暂留空）','website',p.website||'','url','必须是公开网址，不是本机地址。')}
  <div class="full">${textarea('可核验资质与服务授权','qualification',p.qualification||'','保险内容导出前须填写真实从业资质、授权及核验方式；不要上传身份证或密码。','rows="2"')}</div>
  <div class="full">${field('资质或事实依据的公开链接','evidence_url',p.evidence_url||'','url','可留空；不是填写了就代表程序已验证。')}</div></div>
  ${checkbox('facts_confirmed','我已核实上述资料及公开联系用途',!!p.facts_confirmed,'保存是本地操作；发布前仍须检查资质、事实和内容。')}<button class="btn primary" type="submit">保存资料并生成草稿</button></form></div></section>
  <section class="panel mb-20"><div class="panel-head"><h2>② 介绍与常见问答草稿</h2>${button('导出介绍页与内容包','geo-export')}</div><div class="panel-body"><form data-form="geo-entry"><input type="hidden" name="kind" value="draft"><input type="hidden" name="status" value="draft">${field('草稿标题','title',`${p.regions||''} ${p.industry||''}咨询｜${p.name||''}`,'text','','required')}${textarea('内容可直接修改，保存到本地草稿库','content',(g.intro||'')+'\n\n'+(g.faq||''),'资料变化后会生成新模板；你保存的草稿独立保留，不被覆盖。','rows="13" required')}<button type="submit" class="btn primary">保存这份草稿</button></form><p class="help">介绍页按当前资料生成；你保存的编辑草稿另存为包内文本。内容包包含独立 HTML、文本和实体资料。结构化资料与页面内容需一致，它不是指挥其他 AI 推荐的命令。保险仅作咨询介绍，不生成产品收益或购买建议。</p></div></section>
  <section class="panel mb-20"><div class="panel-head"><h2>③ 发布与人工 AI 检验记录</h2></div><div class="panel-body"><ol>${list(g.plan).map(x=>`<li>${e(x)}</li>`).join('')}</ol><p>建议保持名称、地区、业务介绍和公开联系渠道一致；围绕真实问题提供有依据的内容。${link('https://developers.google.com/search/docs/appearance/ai-features','搜索引擎官方 AI 内容说明')}</p><p>可用于人工检验的问题：${e(list(g.questions).join('；'))}</p><form data-form="geo-entry"><div class="form-grid">${select('记录类型','kind','publication',[['publication','发布记录'],['measurement','AI 人工检验记录']])}${select('状态','status','draft',[['draft','待发布 / 未测试'],['published','已发布（本人记录）'],['observed','已人工测试']])}${field('内容标题或测试问题','title','','text','','required')}${field('发布渠道或 AI 名称','channel','','text','记录实际平台，不填虚构结果。') }<div class="full">${field('实际公开内容或引用链接','url','','url','已发布记录必须填写；记录不会自动验证链接或收录。')}</div><div class="full">${textarea('实际结果、回答摘要或备注','note','','例如未提及、提及但未引用、引用了哪些页面；未测试不能说已推荐。','rows="3"')}</div></div><button type="submit" class="btn">保存记录</button></form></div></section>
  <section class="panel"><div class="panel-head"><h2>本地草稿与发布记录 ${list(d.entries).length} 条</h2></div><div class="panel-body">${list(d.entries).map(r=>`<div class="list-row"><div><strong>${e(r.title)}</strong><div class="sub">${e(r.kind)} · ${e(r.status)} · ${e(r.channel)} · ${e(date(r.updated_at))}</div>${r.url?link(r.url,r.url):''}<div class="sub">${e(shorten(r.note||r.content,180))}</div></div><div>${button('编辑','geo-edit',idAttrs(r.id),'small')}${button('删除','geo-delete',idAttrs(r.id),'small danger')}</div></div>`).join('')||'<p>尚无保存记录，没有虚构发布和测试数据。</p>'}</div></section>`;
}
async function submitGeo(type,form,data){
  const payload=Object.fromEntries(data.entries());
  if(type==='geo-profile'){payload.facts_confirmed=data.has('facts_confirmed');await api('/api/geo/profile','POST',payload);}
  else{const id=form.dataset.id;await api(id?`/api/geo/entries/${encodeURIComponent(id)}`:'/api/geo/entries',id?'PATCH':'POST',payload);closeModal();}
  toast('已保存到本机');await loadPage('geo');
}
document.addEventListener('click',async event=>{
  const target=event.target.closest('[data-action^="geo-"]');if(!target)return;
  try{
    if(target.dataset.action==='geo-export'){
      const response=await fetch('/api/geo/export.zip');
      if(!response.ok){const body=await response.json();throw Error(body.error||'导出失败，请核实资料');}
      const url=URL.createObjectURL(await response.blob()),anchor=document.createElement('a');anchor.href=url;anchor.download='GEO内容包.zip';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已导出到本机，未发布到公网');return;
    }
    const id=target.dataset.id,r=list(state.geo?.entries).find(x=>String(x.id)===String(id));if(!r)throw Error('请刷新记录');
    if(target.dataset.action==='geo-delete'){if(!confirm('删除这条本地 GEO 记录？不会删除外部已发布内容。'))return;await api(`/api/geo/entries/${encodeURIComponent(id)}`,'DELETE');await loadPage('geo');return;}
    openModal('编辑 GEO 记录',`<form data-form="geo-entry" data-id="${e(id)}"><input type="hidden" name="kind" value="${e(r.kind)}">${field('标题','title',r.title,'text','','required')}${field('渠道','channel',r.channel,'text')}${select('状态','status',r.status,[['draft','草稿 / 未测试'],['reviewed','已审核'],['published','已发布（本人记录）'],['observed','已人工测试']])}${field('公开链接','url',r.url,'url')}${textarea('内容','content',r.content,'','rows="8"')}${textarea('备注 / 观察结果','note',r.note,'','rows="3"')}<button class="btn primary" type="submit">保存修改</button></form>`);
  }catch(error){toast(error.message,'error');}
});
