/* 出院随访与患者协作 Agent · 工作台前端（无框架、无外部依赖） */

const S = {
  token: localStorage.getItem('fde.token') || '',
  user: null,
  date: new Date().toISOString().slice(0, 10),
  tab: 'schedule',
  bootstrap: null,
  sel: null,          /* 当前触达的排程条目 */
  draft: null,        /* 当前草稿 */
  callStarted: false,
  lastResult: null,
};

const TABS = [
  { id: 'schedule',  label: '今日排程', roles: ['nurse', 'engineer', 'doctor', 'manager', 'quality'] },
  { id: 'workbench', label: '触达工作台', roles: ['nurse'] },
  { id: 'tickets',   label: '红旗工单', roles: ['doctor', 'nurse', 'quality', 'manager'] },
  { id: 'templates', label: '问卷模板', roles: ['doctor', 'nurse', 'engineer', 'quality', 'manager'] },
  { id: 'audit',     label: '审计留痕', roles: ['quality', 'doctor', 'engineer', 'manager'] },
  { id: 'ops',       label: '名单与运维', roles: ['engineer', 'quality'] },
];

const STATUS_TEXT = {
  pending: '待随访', in_progress: '进行中', completed: '已完成',
  escalated: '已升级', shelved: '已搁置',
};

const SAMPLES = [
  { id: 'normal', label: '示例：正常陈述', text: '伤口这两天挺干的，没渗血也没渗液，体温正常，药按时吃着，康复训练每天做两次，下周想去门诊复查一下。' },
  { id: 'family', label: '示例：家属代答', text: '我是她女儿，我妈这两天精神还行，就是不太敢下地走，药我盯着吃的，训练也做了。' },
  { id: 'rf-model', label: '示例：红旗（口语变体）', text: '这两天总觉得喘不上气来，胸口闷得慌，晚上躺着更明显。' },
];

/* ------------------------------------------------------------------ 基础 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmt(iso) {
  if (!iso) return '';
  const s = String(iso).replace('T', ' ');
  return s.length >= 16 ? s.slice(5, 16) : s;
}

function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + kind;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, kind === 'err' ? 5200 : 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json', 'X-Session': S.token || '' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let json;
  try { json = await res.json(); } catch (e) { json = { ok: false, error: '响应解析失败' }; }
  if (!res.ok || !json.ok) throw new Error(json.error || `请求失败 HTTP ${res.status}`);
  return json.data;
}

function chip(text, cls) { return `<span class="chip ${cls}">${esc(text)}</span>`; }
function statusChip(s) { return chip(STATUS_TEXT[s] || s, s); }
function slotChip(s) { return s === 'evening' ? chip('晚间 18–20', 'evening') : chip('白天', 'daytime'); }
function authChip(a) {
  return a === '已授权' ? chip('已授权', 'completed')
    : a === '撤回' ? chip('已撤回', 'escalated') : chip('未授权', 'plain');
}

/* ------------------------------------------------------------------ 登录 */

async function loadBootstrap() {
  S.bootstrap = await api('/api/bootstrap');
  const sel = $('#userSelect');
  sel.innerHTML = S.bootstrap.users.map((u) => (
    `<option value="${esc(u.user_id)}">${esc(u.name)} · ${roleText(u.role)}${u.ward ? ' · ' + esc(u.ward) : ''}</option>`
  )).join('');
  S.date = S.bootstrap.today;
}

function roleText(r) {
  return { nurse: '随访护士', doctor: '专科医生', engineer: '信息工程师', quality: '隐私与质控', manager: '患者服务经理' }[r] || r;
}

async function doLogin(userId) {
  const data = await api('/api/login', { method: 'POST', body: { user_id: userId } });
  S.token = data.token;
  S.user = data.user;
  localStorage.setItem('fde.token', S.token);
  localStorage.setItem('fde.userId', data.user.user_id);
  renderNav();
  const allowed = TABS.filter((t) => t.roles.includes(S.user.role)).map((t) => t.id);
  if (!allowed.includes(S.tab)) S.tab = allowed[0];
  await refresh();
  toast(`已进入：${data.user.name}（${roleText(data.user.role)}）`, 'ok');
}

/* ------------------------------------------------------------------ 导航 */

function renderNav() {
  const nav = $('#sidenav');
  if (!S.user) { nav.innerHTML = '<p class="hint" style="padding:8px">请选择操作者后进入工作台。</p>'; return; }
  const allowed = TABS.filter((t) => t.roles.includes(S.user.role));
  nav.innerHTML = allowed.map((t) => (
    `<button class="navitem ${t.id === S.tab ? 'active' : ''}" data-tab="${t.id}">
       <span class="dot"></span><span>${t.label}</span>
       <span class="badge" data-badge="${t.id}" hidden></span>
     </button>`)).join('');
  $$('.navitem', nav).forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + S.tab));
}

function switchTab(tab) {
  S.tab = tab;
  $$('.navitem').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + tab));
  refresh();
}

function setBadge(tab, n) {
  const el = $(`[data-badge="${tab}"]`);
  if (!el) return;
  el.hidden = !n;
  el.textContent = n || '';
}

/* ------------------------------------------------------------------ 刷新 */

async function refresh() {
  if (!S.user) return;
  await refreshKpi();
  if (S.tab === 'schedule') await renderSchedule();
  if (S.tab === 'workbench') await renderWorkbench();
  if (S.tab === 'tickets') await renderTickets();
  if (S.tab === 'templates') await renderTemplates();
  if (S.tab === 'audit') await renderAudit();
  if (S.tab === 'ops') await renderOps();
}

async function refreshKpi() {
  try {
    const s = await api('/api/summary?date=' + S.date);
    const st = s.stats;
    $('#kpiStrip').innerHTML = `
      <div class="kpi"><div class="k-label">当日排程</div><div class="k-value">${st.scheduled}</div>
        <div class="k-note">名单 ${esc(s.date)} · 晚间时段优先</div></div>
      <div class="kpi accent"><div class="k-label">问卷完成率</div><div class="k-value">${st.completion_rate}%</div>
        <div class="k-note">短信 ${st.sms_attempts_not_counted} 次 · 不计入完成率</div></div>
      <div class="kpi ${s.pending_tickets ? 'danger' : ''}"><div class="k-label">待处置红旗工单</div>
        <div class="k-value">${s.pending_tickets}</div><div class="k-note">已升级 ${st.escalated} · 已搁置 ${st.shelved}</div></div>
      <div class="kpi"><div class="k-label">人工跟进清单</div><div class="k-value">${s.manual_list}</div>
        <div class="k-note">模板未签发 / 术式缺失 / 名单待补</div></div>`;
    setBadge('tickets', s.pending_tickets);
  } catch (e) { /* KPI 失败不阻断主流程 */ }
}

/* ------------------------------------------------------------------ 排程 */

async function renderSchedule() {
  const canRun = ['nurse', 'engineer'].includes(S.user.role);
  const panel = $('#panel-schedule');
  let data;
  try { data = await api('/api/schedule?date=' + S.date); }
  catch (e) { panel.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; return; }

  const items = data.items;
  const rows = items.map((it, i) => `
    <tr data-sid="${it.id}" class="${S.sel && S.sel.id === it.id ? 'is-sel' : ''}">
      <td class="num mono">${it.seq}</td>
      <td>${esc(it.inpatient_no)}<div class="pill-note">${esc(fmt(it.discharge_time))}</div></td>
      <td>${esc(it.department)}<div class="pill-note">${esc(it.procedure_code || '术式缺失')}</div></td>
      <td>${slotChip(it.slot)}</td>
      <td>${esc(it.nurse_name)}</td>
      <td>${authChip(it.auth_status)}</td>
      <td>${statusChip(it.status)}</td>
      <td class="row" style="justify-content:flex-end">
        <button class="btn btn-sm" data-act="up" data-sid="${it.id}" title="上移">↑</button>
        <button class="btn btn-sm" data-act="down" data-sid="${it.id}" title="下移">↓</button>
        <button class="btn btn-sm btn-primary" data-act="open" data-sid="${it.id}"
          ${it.status === 'completed' ? 'disabled' : ''}>触达</button>
      </td>
    </tr>`).join('');

  panel.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>今日排程</h3><div class="sub">只纳入有签发版本模板的术式；家属联系人优先晚间时段；三次未接自动搁置转人工</div></div>
        <div class="row">
          <input type="date" id="dateInput" value="${esc(S.date)}" style="width:150px">
          ${canRun ? '<button class="btn btn-primary" id="runSchedule">生成排程</button>' : ''}
          <button class="btn" id="reloadSchedule">刷新</button>
        </div>
      </div>
      <div class="card-body tight">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th class="num">序</th><th>住院号 / 出院</th><th>科室 / 术式</th><th>时段</th>
              <th>责任护士</th><th>授权</th><th>状态</th><th></th>
            </tr></thead>
            <tbody>${rows || '<tr><td colspan="8" class="empty">当日暂无排程。' + (canRun ? '点击「生成排程」。' : '') + '</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div><h3>人工跟进清单</h3>
        <div class="sub">模板未签发 / 术式缺失 / 名单超 24 小时未补齐 —— 不降级为通用问卷</div></div>
        <span class="chip plain">${data.manual_list.length} 条</span></div>
      <div class="card-body tight">
        <table><thead><tr><th>住院号</th><th>出院</th><th>科室</th><th>术式</th><th>转人工原因</th></tr></thead>
        <tbody>${data.manual_list.map((m) => `<tr>
          <td>${esc(m.inpatient_no)}</td><td>${esc(fmt(m.discharge_time))}</td>
          <td>${esc(m.department)}</td><td>${esc(m.procedure_code || '—')}</td>
          <td class="hint">${esc(m.manual_reason)}</td></tr>`).join('')
          || '<tr><td colspan="5" class="empty">暂无</td></tr>'}</tbody></table>
      </div>
    </div>`;

  $('#dateInput').addEventListener('change', (e) => { S.date = e.target.value; refresh(); });
  $('#reloadSchedule').addEventListener('click', refresh);
  if (canRun) {
    $('#runSchedule').addEventListener('click', async () => {
      try {
        const st = await api('/api/schedule/run', { method: 'POST', body: { date: S.date } });
        toast(`排程完成：新建 ${st.created} 条，幂等跳过 ${st.skipped_duplicate} 条，转人工 ${st.manual} 条`, 'ok');
        refresh();
      } catch (e) { toast(e.message, 'err'); }
    });
  }
  $$('[data-act]', panel).forEach((b) => b.addEventListener('click', async () => {
    if (b.dataset.act === 'open') {
      S.sel = items.find((x) => String(x.id) === b.dataset.sid);
      S.draft = null; S.callStarted = false;
      if (S.user.role !== 'nurse') { toast('触达为随访护士岗位操作', 'err'); return; }
      switchTab('workbench');
      return;
    }
    try {
      await api(`/api/schedule/${b.dataset.sid}/reorder`, { method: 'POST', body: { direction: b.dataset.act } });
      refresh();
    } catch (e) { toast(e.message, 'err'); }
  }));
}

/* ------------------------------------------------------------------ 触达 */

async function renderWorkbench() {
  const panel = $('#panel-workbench');
  if (!S.user) return;

  if (!S.sel) {
    let board;
    try { board = await api('/api/schedule?date=' + S.date); }
    catch (e) { panel.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; return; }
    const open = board.items.filter((i) => i.status === 'pending' || i.status === 'in_progress');
    panel.innerHTML = `
      <div class="card">
        <div class="card-head"><div><h3>选择待触达患者</h3>
          <div class="sub">硬约束：授权状态非「已授权」不启动外呼；未告知授权即不录音；短信不计入完成率</div></div></div>
        <div class="card-body tight">
          <table><thead><tr><th>序</th><th>住院号</th><th>时段</th><th>授权</th><th>状态</th><th></th></tr></thead>
          <tbody>${open.map((i) => `<tr>
            <td class="num mono">${i.seq}</td><td>${esc(i.inpatient_no)}<div class="pill-note">${esc(i.department)}</div></td>
            <td>${slotChip(i.slot)}</td><td>${authChip(i.auth_status)}</td><td>${statusChip(i.status)}</td>
            <td class="row" style="justify-content:flex-end"><button class="btn btn-sm btn-primary" data-open="${i.id}">开始触达</button></td>
          </tr>`).join('') || '<tr><td colspan="6" class="empty">暂无待触达条目</td></tr>'}</tbody></table>
        </div>
      </div>`;
    $$('[data-open]', panel).forEach((b) => b.addEventListener('click', () => {
      S.sel = open.find((x) => String(x.id) === b.dataset.open);
      S.draft = null; S.callStarted = false;
      renderWorkbench();
    }));
    return;
  }

  const it = S.sel;
  let tpl = null;
  try {
    const t = await api('/api/templates');
    tpl = t.items.find((x) => x.procedure_code === it.procedure_code) || null;
  } catch (e) { /* ignore */ }

  panel.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>触达工作台 · ${esc(it.inpatient_no)}</h3>
          <div class="sub">${esc(it.department)} · ${esc(it.procedure_code || '术式缺失')} · 模板版本 ${esc(it.template_version)} · ${esc(fmt(it.discharge_time))} 出院</div></div>
        <div class="row">
          <button class="btn btn-sm" id="backList">返回列表</button>
          ${statusChip(it.status)}
        </div>
      </div>
      <div class="card-body">
        <div class="patient-card">
          <div class="stepbar">
            <span class="st ${S.callStarted ? 'on' : ''}"><b>1</b>外呼（授权校验）</span><span class="sep">›</span>
            <span class="st ${S.lastResult ? 'on' : ''}"><b>2</b>问卷与红旗检查</span><span class="sep">›</span>
            <span class="st ${S.draft ? 'on' : ''}"><b>3</b>草稿与护士确认</span>
          </div>
          <div class="pc-grid">
            <div class="pc-item"><div class="pc-label">授权状态</div><div class="pc-value">${authChip(it.auth_status)}</div></div>
            <div class="pc-item"><div class="pc-label">联系人类型</div><div class="pc-value">${esc(it.contact_type)}</div></div>
            <div class="pc-item"><div class="pc-label">联系电话</div><div class="pc-value mono">${esc(it.contact_phone)}</div></div>
            <div class="pc-item"><div class="pc-label">已尝试次数</div><div class="pc-value">${it.attempts} / 3</div></div>
          </div>
          ${tpl ? `<ul class="qlist" style="margin-top:10px">${tpl.questions.map((q) => `<li>${esc(q.text)}</li>`).join('')}</ul>` : '<div class="alert warn" style="margin-top:10px">该术式无签发模板，按规则应转人工，不应继续脚本化问卷。</div>'}
        </div>

        <div class="sep-line"></div>
        <div class="row" style="justify-content:space-between">
          <div class="row">
            <label class="row" style="gap:6px"><input type="checkbox" id="consent" ${it.auth_status === '已授权' ? '' : 'disabled'}>
              <span class="hint">通话前已播报录音告知语，患者/家属同意录音</span></label>
          </div>
          <div class="row">
            <button class="btn btn-primary" id="startCall" ${S.callStarted ? 'disabled' : ''}>一键外呼</button>
            <button class="btn" id="noAnswer">未接</button>
            <button class="btn" id="invalidNumber">空号</button>
            <button class="btn" id="sms">发短信</button>
            <button class="btn btn-danger" id="revoke">撤回授权</button>
          </div>
        </div>

        <div class="sep-line"></div>
        <label class="field">患者/家属陈述（原文，留痕不脱敏）</label>
        <textarea id="transcript" placeholder="在此粘贴或记录通话内容…"></textarea>
        <div class="row" style="margin-top:8px">
          ${SAMPLES.map((s) => `<button class="btn btn-sm" data-sample="${s.id}">${esc(s.label)}</button>`).join('')}
          <button class="btn btn-primary" id="analyze" style="margin-left:auto" ${S.callStarted ? '' : 'disabled title="需先完成外呼授权校验"'}>提交分析（红旗检查 + 草稿）</button>
        </div>
        <div class="pill-note" style="margin-top:6px">短信发送仅提醒，不计入完成率；三次未接自动搁置并转入人工跟进清单。</div>
      </div>
    </div>
    <div id="resultArea"></div>`;

  $('#backList').addEventListener('click', () => {
    S.sel = null; S.draft = null; S.callStarted = false; S.lastResult = null; renderWorkbench();
  });
  $('#startCall').addEventListener('click', async () => {
    const consent = $('#consent').checked;
    try {
      const r = await api(`/api/contact/${it.id}/call`, { method: 'POST', body: { consent } });
      S.callStarted = true;
      toast(r.message + (consent ? '，录音已开启' : '，本通话不录音'));
      renderWorkbench();
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#noAnswer').addEventListener('click', () => attempt('call', 'no_answer'));
  $('#invalidNumber').addEventListener('click', () => attempt('call', 'invalid_number'));
  $('#sms').addEventListener('click', () => attempt('sms', 'sent'));

  async function attempt(channel, result) {
    try {
      const r = await api(`/api/contact/${it.id}/attempt`, { method: 'POST', body: { channel, result } });
      toast(r.message + (channel === 'sms' ? '（短信不计入完成率）' : ''));
      S.sel = null; S.callStarted = false; S.draft = null;
      renderWorkbench();
    } catch (e) { toast(e.message, 'err'); }
  }

  $('#revoke').addEventListener('click', async () => {
    if (!confirm('确认患者撤回授权？系统将停止外呼与录音，历史录音标记「不得用于训练」。')) return;
    try {
      const su = await api('/api/stepup', { method: 'POST', body: { purpose: 'auth_revoke', action_id: String(it.id) } });
      await api(`/api/contact/${it.id}/revoke`, { method: 'POST', body: { token: su.token, reason: '患者电话撤回授权' } });
      toast('已撤回授权：24 小时内停用，历史录音标记不得用于训练', 'ok');
      S.sel = null; renderWorkbench();
    } catch (e) { toast(e.message, 'err'); }
  });

  $$('[data-sample]', panel).forEach((b) => b.addEventListener('click', () => {
    const s = SAMPLES.find((x) => x.id === b.dataset.sample);
    $('#transcript').value = s.text;
  }));

  $('#analyze').addEventListener('click', async () => {
    const transcript = $('#transcript').value.trim();
    if (!transcript) { toast('请先记录患者陈述', 'err'); return; }
    try {
      const r = await api('/api/questionnaire/check', {
        method: 'POST', body: { schedule_id: it.id, transcript },
      });
      S.lastResult = r;
      if (r.interrupted) { S.sel = { ...it, status: 'escalated' }; }
      else { S.draft = r.draft; }
      renderResult(it, r);
      refreshKpi();
      if (r.interrupted) toast('红旗命中：问卷已中断，已生成升级工单', 'err');
    } catch (e) { toast(e.message, 'err'); }
  });

  if (S.lastResult) renderResult(it, S.lastResult);
  else $('#resultArea').innerHTML = `<div class="alert info"><strong>说明</strong>红旗优先于草稿：一旦命中立即中断问卷、生成升级工单并转人工，不生成草稿、不写登记表。</div>`;
}

function renderResult(it, r) {
  const area = $('#resultArea');
  if (r.interrupted) {
    area.innerHTML = `
      <div class="alert danger">
        <strong>红旗命中 · 问卷已中断</strong>
        ${esc(r.message)}
      </div>
      <div class="card"><div class="card-head"><h3>命中明细</h3>
        <span class="chip plain">命中 ${r.hits.length} 条 · 新建工单 ${r.tickets_created} 张</span></div>
        <div class="card-body">
          <div class="hitlist">${r.hits.map((h) => `
            <div class="hit">
              <div>
                <div class="h-title">${esc(h.rule_id)} · ${esc(h.title)}</div>
                <div class="h-meta">通道：${h.channel === 'lexicon' ? '固定词表' : '本地模型替身'} ·
                  触发：${esc(h.evidence)} · 升级：${esc(h.escalation)}</div>
              </div>
              <div class="conf">${(h.confidence * 100).toFixed(0)}%</div>
            </div>`).join('')}</div>
          <div class="pill-note" style="margin-top:10px">宁误报不漏报：低置信度也按红旗处理；误报由专科在工单中标注「排除」。</div>
        </div></div>`;
    return;
  }

  const fields = r.draft.fields;
  area.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>结构化草稿 · 待护士确认</h3>
          <div class="sub">${esc(r.draft.generator)} · 草稿不是登记表，确认后才归档</div></div>
        <span class="chip tag">草稿 ${r.draft.draft_id}</span>
      </div>
      <div class="card-body">
        <div class="grid-2">
          ${Object.keys(fields).map((k) => `
            <div class="draft-field" style="grid-column:${k === '患者原话' ? '1 / -1' : 'auto'}">
              <div class="df-head"><span class="df-name">${esc(k)}</span></div>
              <textarea data-field="${esc(k)}" rows="${k === '患者原话' ? 3 : 2}">${esc(fields[k])}</textarea>
            </div>`).join('')}
        </div>
        <div class="row" style="margin-top:6px">
          <span class="hint">修改过的字段会留痕（草稿原值 / 确认值 / 修改人 / 时间）。签署人恒为护士。</span>
          <div class="row" style="margin-left:auto">
            <button class="btn" id="discardDraft">不确认，仅存草稿</button>
            <button class="btn btn-primary" id="confirmDraft">护士确认并写入登记表</button>
          </div>
        </div>
      </div>
    </div>`;

  $$('[data-field]', area).forEach((t) => t.addEventListener('input', () => {
    t.closest('.draft-field').classList.toggle('changed', t.value !== fields[t.dataset.field]);
  }));

  $('#discardDraft').addEventListener('click', async () => {
    try {
      await api('/api/drafts/discard', { method: 'POST', body: { draft_id: S.draft.draft_id } });
      toast('已作废草稿，未写入登记表');
      S.draft = null; S.lastResult = null; $('#resultArea').innerHTML = '';
    } catch (e) { toast(e.message, 'err'); }
  });

  $('#confirmDraft').addEventListener('click', async () => {
    const body = {};
    $$('[data-field]', area).forEach((t) => { body[t.dataset.field] = t.value; });
    const payload = JSON.parse(JSON.stringify(body));
    try {
      const su = await api('/api/stepup', {
        method: 'POST',
        body: { purpose: 'record_confirm', action_id: String(S.draft.draft_id), fields: payload },
      });
      const rec = await api('/api/records/confirm', {
        method: 'POST', body: { draft_id: S.draft.draft_id, fields: payload, token: su.token },
      });
      toast(`已写入登记表，签署人 ${S.user.name}；差异字段 ${rec.diff.length} 个`, 'ok');
      S.draft = null; S.lastResult = null; S.sel = null; S.callStarted = false;
      renderWorkbench(); refreshKpi();
    } catch (e) { toast(e.message, 'err'); }
  });
}

/* ------------------------------------------------------------------ 工单 */

async function renderTickets() {
  const panel = $('#panel-tickets');
  let data;
  try { data = await api('/api/tickets'); }
  catch (e) { panel.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; return; }
  const pending = data.items.filter((t) => t.status === 'pending');
  const done = data.items.filter((t) => t.status !== 'pending');
  const isDoctor = S.user.role === 'doctor';

  panel.innerHTML = `
    <div class="card">
      <div class="card-head"><div><h3>待处置红旗工单</h3>
        <div class="sub">触发到建单 ≤1 分钟 · 工单含触发时间、原文、命中通道与置信度</div></div>
        <span class="chip ${pending.length ? 'escalated' : 'plain'}">${pending.length} 张待处置</span></div>
      <div class="card-body">
        ${pending.map((t) => `
          <div class="hit" data-tid="${t.id}" style="grid-template-columns:minmax(0,1fr) auto; margin-bottom:10px">
            <div>
              <div class="h-title">${esc(t.rule_id)} · ${esc(t.inpatient_no)} · ${esc(t.department)}</div>
              <div class="h-meta">${esc(fmt(t.created_at))} · 通道 ${t.channel === 'lexicon' ? '固定词表' : '本地模型替身'}
                · 置信度 ${(t.confidence * 100).toFixed(0)}% · 排程 ${esc(t.schedule_date)} · ${esc(t.slot === 'evening' ? '晚间' : '白天')}</div>
              <div class="mono" style="margin-top:6px; color:#7d1a15">“${esc(t.trigger_text)}”</div>
              ${isDoctor ? `
                <div class="row" style="margin-top:8px">
                  <select data-decision="${t.id}" style="width:150px">
                    <option value="approved">确认红旗，转人工处置</option>
                    <option value="rejected">排除（误报）</option>
                    <option value="continued">继续观察</option>
                  </select>
                  <input type="text" data-note="${t.id}" placeholder="处置说明（可选）" style="width:240px">
                  <button class="btn btn-primary btn-sm" data-resolve="${t.id}">提交处置</button>
                </div>` : '<div class="pill-note" style="margin-top:6px">处置权限：专科医生</div>'}
            </div>
            <div class="conf">待处置</div>
          </div>`).join('') || '<div class="empty">当前没有待处置工单</div>'}
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>已处置工单</h3></div>
      <div class="card-body tight">
        <table><thead><tr><th>规则</th><th>住院号</th><th>通道</th><th>结论</th><th>处置人</th><th>时间</th><th>说明</th></tr></thead>
        <tbody>${done.map((t) => `<tr>
          <td>${esc(t.rule_id)}</td><td>${esc(t.inpatient_no)}</td>
          <td>${esc(t.channel)}</td><td>${chip(t.status, t.status === 'approved' ? 'escalated' : t.status === 'rejected' ? 'plain' : 'shelved')}</td>
          <td>${esc(t.resolved_name || t.resolved_by)}</td><td>${esc(fmt(t.resolved_at))}</td>
          <td class="hint">${esc(t.resolution || '—')}</td></tr>`).join('')
          || '<tr><td colspan="7" class="empty">暂无</td></tr>'}</tbody></table>
      </div>
    </div>`;

  $$('[data-resolve]', panel).forEach((b) => b.addEventListener('click', async () => {
    const id = b.dataset.resolve;
    const decision = $(`[data-decision="${id}"]`, panel).value;
    const note = $(`[data-note="${id}"]`, panel).value;
    try {
      const su = await api('/api/stepup', {
        method: 'POST', body: { purpose: 'ticket_resolve', action_id: id, decision, note },
      });
      await api(`/api/tickets/${id}/resolve`, {
        method: 'POST', body: { decision, note, token: su.token },
      });
      toast('工单已处置（需 step-up 凭证，处置人取自会话）', 'ok');
      renderTickets(); refreshKpi();
    } catch (e) { toast(e.message, 'err'); }
  }));
}

/* ------------------------------------------------------------------ 模板 */

async function renderTemplates() {
  const panel = $('#panel-templates');
  let data, rules;
  try {
    data = await api('/api/templates');
    rules = await api('/api/rules');
  } catch (e) { panel.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; return; }
  const isDoctor = S.user.role === 'doctor';

  panel.innerHTML = `
    <div class="split">
      <div class="card">
        <div class="card-head"><div><h3>术式问卷模板</h3>
          <div class="sub">每个术式须有签发版本号与生效日期；未签发不下发（REQ-03）</div></div></div>
        <div class="card-body tight">
          <table><thead><tr><th>术式</th><th>模板</th><th>版本</th><th>生效</th><th>状态</th><th></th></tr></thead>
          <tbody>${data.items.map((t) => `<tr>
            <td class="mono">${esc(t.procedure_code)}</td><td>${esc(t.title)}</td>
            <td class="mono">${esc(t.version)}</td><td>${esc(t.effective_date)}</td>
            <td>${chip(t.status === 'signed' ? '已签发' : '未签发', t.status)}</td>
            <td class="row" style="justify-content:flex-end">
              ${isDoctor && t.status !== 'signed'
                ? `<button class="btn btn-sm btn-primary" data-sign="${esc(t.procedure_code)}">签发</button>`
                : `<span class="pill-note">${t.signed_by ? esc(t.signed_by) : '—'}</span>`}
            </td></tr>`).join('')}</tbody></table>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div><h3>红旗规则包</h3>
          <div class="sub">${esc(rules.rule_set)} v${esc(rules.version)} · 生效 ${esc(rules.effective_date)}</div></div></div>
        <div class="card-body">
          <dl class="dl">
            <dt>动作</dt><dd class="mono">${esc(rules.action)}</dd>
            <dt>模型通道</dt><dd>${esc(rules.model_channel.name)}（${esc(rules.model_channel.note)}）</dd>
            <dt>策略</dt><dd>${esc(rules.model_channel.policy)}</dd>
            <dt>置信度阈值</dt><dd>${rules.model_channel.min_confidence}</dd>
          </dl>
          <div class="sep-line"></div>
          <div class="hitlist">${rules.rules.map((r) => `
            <div class="hit" style="grid-template-columns:minmax(0,1fr)">
              <div><div class="h-title">${esc(r.rule_id)} · ${esc(r.title)}</div>
              <div class="h-meta">词表 ${r.keywords.length} 条 / 变体 ${r.variants.length} 条 · 升级 ${esc(r.escalation)}</div></div>
            </div>`).join('')}</div>
        </div>
      </div>
    </div>`;

  $$('[data-sign]', panel).forEach((b) => b.addEventListener('click', async () => {
    const code = b.dataset.sign;
    try {
      const su = await api('/api/stepup', { method: 'POST', body: { purpose: 'template_sign', action_id: code } });
      const r = await api(`/api/templates/${code}/sign`, { method: 'POST', body: { token: su.token } });
      toast(`已签发 ${code} v${r.version}，生效 ${r.effective_date}`, 'ok');
      renderTemplates();
    } catch (e) { toast(e.message, 'err'); }
  }));
}

/* ------------------------------------------------------------------ 审计 */

async function renderAudit() {
  const panel = $('#panel-audit');
  let items, cov;
  try {
    items = await api('/api/audit?limit=120');
    cov = await api('/api/audit/coverage');
  } catch (e) { panel.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; return; }

  panel.innerHTML = `
    <div class="split">
      <div class="card">
        <div class="card-head"><div><h3>留痕事件</h3>
          <div class="sub">三类事件：操作 / 升级 / 授权 · 只写不改（DB 触发器拒绝改删）</div></div>
          <button class="btn btn-sm" id="verifyChain">校验哈希链</button></div>
        <div class="card-body tight">
          <div class="table-wrap" style="max-height:520px">
          <table><thead><tr><th class="num">seq</th><th>类型</th><th>动作</th><th>主体</th><th>操作者</th><th>时间</th><th>结果</th></tr></thead>
          <tbody>${items.items.map((e) => `<tr>
            <td class="num mono">${e.seq}</td>
            <td>${chip({ operation: '操作', escalation: '升级', authorization: '授权' }[e.event_type], e.event_type === 'escalation' ? 'escalated' : e.event_type === 'authorization' ? 'in_progress' : 'plain')}</td>
            <td class="mono">${esc(e.action)}</td>
            <td>${esc(e.subject || '—')}<div class="pill-note">${esc(e.detail && e.detail.rule_id ? e.detail.rule_id : '')}</div></td>
            <td>${esc(e.actor_id)}</td><td>${esc(fmt(e.at))}</td>
            <td>${esc(e.outcome)}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">暂无事件</td></tr>'}</tbody></table>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>审计覆盖率</h3></div>
        <div class="card-body">
          <dl class="dl">
            <dt>操作事件</dt><dd>${cov.by_type.operation || 0}</dd>
            <dt>升级事件</dt><dd>${cov.by_type.escalation || 0}</dd>
            <dt>授权事件</dt><dd>${cov.by_type.authorization || 0}</dd>
            <dt>登记表</dt><dd>${cov.records} 条 / 留痕 ${cov.records_audited}</dd>
            <dt>工单</dt><dd>${cov.tickets} 张 / 留痕 ${cov.tickets_audited}</dd>
            <dt>覆盖率</dt><dd>${cov.coverage_ok ? chip('关键动作均已留痕', 'completed') : chip('存在未留痕动作', 'escalated')}</dd>
          </dl>
          <div class="sep-line"></div>
          <div class="hint">校验说明：日结脚本按序重放 prev_hash/entry_hash；任意一行被篡改即链断。</div>
        </div>
      </div>
    </div>`;

  $('#verifyChain').addEventListener('click', async () => {
    try {
      const r = await api('/api/audit/verify', { method: 'POST', body: {} });
      toast(r.ok ? `哈希链完整：${r.count} 条事件校验通过` : `链断裂于 seq ${r.broken_at_seq}`, r.ok ? 'ok' : 'err');
      renderAudit();
    } catch (e) { toast(e.message, 'err'); }
  });
}

/* ------------------------------------------------------------------ 运维 */

async function renderOps() {
  const panel = $('#panel-ops');
  let boot, board;
  try {
    boot = await api('/api/bootstrap');
    board = await api('/api/schedule?date=' + S.date);
  } catch (e) { panel.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; return; }

  panel.innerHTML = `
    <div class="split">
      <div class="card">
        <div class="card-head"><div><h3>HIS 名单导入（接口替身）</h3>
          <div class="sub">只读、每日全量、8 字段白名单；超出字段一律拒收</div></div></div>
        <div class="card-body">
          <div class="row">
            <select id="fixtureSelect" style="width:260px">
              ${boot.fixtures.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join('')}
            </select>
            <button class="btn btn-primary" id="importBtn">导入批次</button>
            <button class="btn btn-danger" id="timeoutBtn">模拟接口超时</button>
          </div>
          <div class="sep-line"></div>
          <table><thead><tr><th>批次</th><th>导出时间</th><th>状态</th><th class="num">接收</th><th class="num">拒收</th></tr></thead>
          <tbody>${boot.batches.map((b) => `<tr>
            <td class="mono">${esc(b.batch_version)}</td><td>${esc(fmt(b.exported_at))}</td>
            <td>${chip(b.status, b.status === 'active' ? 'completed' : b.status === 'pending_backfill' ? 'shelved' : 'plain')}</td>
            <td class="num">${b.accepted}</td><td class="num">${b.rejected}</td></tr>`).join('')
            || '<tr><td colspan="5" class="empty">尚未导入名单</td></tr>'}</tbody></table>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>系统边界与合规</h3></div>
        <div class="card-body">
          <dl class="dl">
            <dt>部署</dt><dd>${esc(boot.egress.mode)} · 仅本机回环</dd>
            <dt>外网出口</dt><dd>${boot.egress.external_network_exit ? chip('存在出口', 'escalated') : chip('无外网出口', 'completed')}</dd>
            <dt>外呼依赖</dt><dd>${esc(boot.egress.outbound_calls)}</dd>
            <dt>最小必要</dt><dd>8 字段白名单，超出拒收</dd>
            <dt>留痕</dt><dd>操作/升级/授权 三类，append-only + 哈希链</dd>
            <dt>凭证</dt><dd>step-up 300 s，一次一用，DB nonce</dd>
          </dl>
          <div class="sep-line"></div>
          <label class="field">撤回授权（会对该患者立即停止外呼与录音）</label>
          <div class="row">
            <select id="revokeSelect" style="width:100%">
              ${board.items.map((i) => `<option value="${i.id}">${esc(i.inpatient_no)} · ${esc(i.department)} · ${esc(i.auth_status)}</option>`).join('')
                || '<option value="">当日无排程</option>'}
            </select>
            <button class="btn btn-danger" id="revokeOps">撤回授权</button>
          </div>
        </div>
      </div>
    </div>`;

  $('#importBtn').addEventListener('click', async () => {
    try {
      const r = await api('/api/his/import', {
        method: 'POST', body: { fixture: $('#fixtureSelect').value },
      });
      toast(r.idempotent
        ? `批次已存在：${r.message}`
        : `导入完成：接收 ${r.accepted}，拒收 ${r.rejected}，合并 ${r.merged}，丢弃超额字段 ${r.dropped_fields.join('、') || '无'}`,
        r.idempotent ? '' : 'ok');
      renderOps(); refreshKpi();
    } catch (e) { toast(e.message, 'err'); }
  });

  $('#timeoutBtn').addEventListener('click', async () => {
    try {
      const r = await api('/api/his/import', { method: 'POST', body: { simulate: 'timeout' } });
      toast(`已降级：沿用批次 ${r.fallback_batch} 并标记「待补」，超 24 小时转人工`, 'ok');
      renderOps();
    } catch (e) { toast(e.message, 'err'); }
  });

  $('#revokeOps').addEventListener('click', async () => {
    const sid = $('#revokeSelect').value;
    if (!sid) { toast('当日无可撤回条目', 'err'); return; }
    if (!confirm('确认撤回该患者授权？停止外呼与录音，历史录音标记不得用于训练。')) return;
    try {
      const su = await api('/api/stepup', { method: 'POST', body: { purpose: 'auth_revoke', action_id: String(sid) } });
      await api(`/api/contact/${sid}/revoke`, { method: 'POST', body: { token: su.token } });
      toast('已撤回授权并停止外呼/录音', 'ok');
      renderOps(); refreshKpi();
    } catch (e) { toast(e.message, 'err'); }
  });
}

/* ------------------------------------------------------------------ 启动 */

(async function boot() {
  try {
    await loadBootstrap();
  } catch (e) {
    $('#sidenav').innerHTML = `<div class="alert danger">${esc(e.message)}</div>`;
    return;
  }
  $('#loginBtn').addEventListener('click', () => doLogin($('#userSelect').value).catch((e) => toast(e.message, 'err')));

  const savedId = localStorage.getItem('fde.userId') || 'u-nurse-01';
  const hasOpt = Array.from($('#userSelect').options).some((o) => o.value === savedId);
  $('#userSelect').value = hasOpt ? savedId : $('#userSelect').options[0].value;

  if (S.token) {
    try {
      const s = await api('/api/summary?date=' + S.date);
      const me = S.bootstrap.users.find((u) => u.user_id === (localStorage.getItem('fde.userId') || ''));
      if (me) {
        S.user = me;
        renderNav();
        const allowed = TABS.filter((t) => t.roles.includes(me.role)).map((t) => t.id);
        if (!allowed.includes(S.tab)) S.tab = allowed[0];
        renderNav(); await refresh();
        return;
      }
    } catch (e) { /* 会话失效，走重新登录 */ }
  }
  await doLogin($('#userSelect').value).catch((e) => toast(e.message, 'err'));
})();
