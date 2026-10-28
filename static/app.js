'use strict';

const $ = (id) => document.getElementById(id);
const integer = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });
const decimals = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
const scopes = {
  task: { label: '任务用量', description: '包含主对话 + 子代理 · 自动审批未计入' },
  all: { label: '全部记录', description: '包含主对话 + 子代理 + 自动审批' },
  main: { label: '仅主对话', description: '子代理和自动审批均未计入' },
  agent: { label: '仅子代理', description: '只统计普通子代理 · 按所属主对话归组' },
  review: { label: '仅自动审批', description: '只统计自动审批记录 · 按所属主对话归组' }
};
const providerLabels = { all: '全部代理工具', codex: 'Codex', claude: 'Claude Code', opencode: 'OpenCode' };
const originLabels = { all: '全部 Paseo 关联', paseo: '已关联 Paseo', other: '未关联 Paseo' };
const rangeLabels = { '1': '今天', '7': '最近 7 天', '30': '最近 30 天', '90': '最近 90 天', all: '全部时间', custom: '自选日期' };
const categoryLabels = { main: '主对话', agent: '子代理', review: '自动审批', main_tokens: '主对话', agent_tokens: '子代理', review_tokens: '自动审批' };
const n = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
const exact = (value) => integer.format(n(value));
const decimal = (value) => decimals.format(n(value));
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const text = (id, value) => { $(id).textContent = value; };
const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
const speed = (value) => known(value) ? Number(value).toFixed(1) : '—';
const average = (item) => item.average_turn_tps ?? item.output_tps;
const samples = (item) => Array.isArray(item.speed_samples) ? item.speed_samples.length : n(item.speed_samples);
const state = { data: null, applied: null, days: '7', scope: 'task', model: 'all', provider: 'all', origin: 'all', start: '', end: '', search: '', sort: 'tokens', numberFormat: 'compact', chartMetric: 'total', modelMetric: 'total', autoRefresh: true, expanded: new Set(), expandedTurns: new Set(), requestId: 0, controller: null, loading: false, draftDates: false };
const live = { data: null, requestId: 0, controller: null, loading: false, error: '', nodes: new Map() };
try {
  const saved = JSON.parse(localStorage.getItem('codex-pulse-preferences-v2') || '{}');
  if (saved.days in rangeLabels) state.days = saved.days;
  if (saved.scope in scopes) state.scope = saved.scope;
  if (saved.provider in providerLabels) state.provider = saved.provider;
  if (saved.origin in originLabels) state.origin = saved.origin;
  if (typeof saved.model === 'string') state.model = saved.model;
  if (['tokens', 'output', 'recent', 'speed'].includes(saved.sort)) state.sort = saved.sort;
  if (['compact', 'exact'].includes(saved.numberFormat)) state.numberFormat = saved.numberFormat;
  if (['total', 'output'].includes(saved.chartMetric)) state.chartMetric = saved.chartMetric;
  if (['total', 'output'].includes(saved.modelMetric)) state.modelMetric = saved.modelMetric;
  if (typeof saved.autoRefresh === 'boolean') state.autoRefresh = saved.autoRefresh;
  if (/^\d{4}-\d{2}-\d{2}$/.test(saved.start)) state.start = saved.start;
  if (/^\d{4}-\d{2}-\d{2}$/.test(saved.end)) state.end = saved.end;
} catch { /* Local storage may be disabled; all controls still work. */ }
function savePreferences() {
  const { days, scope, model, provider, origin, start, end, sort, numberFormat, chartMetric, modelMetric, autoRefresh } = state;
  try { localStorage.setItem('codex-pulse-preferences-v2', JSON.stringify({ days, scope, model, provider, origin, start, end, sort, numberFormat, chartMetric, modelMetric, autoRefresh })); } catch { /* Optional preference storage. */ }
}
function compact(value) {
  const number = n(value);
  if (number >= 100000000) return `${(number / 100000000).toFixed(number >= 1000000000 ? 1 : 2).replace(/\.?0+$/, '')} 亿`;
  if (number >= 10000) return `${(number / 10000).toFixed(number >= 1000000 ? 1 : 2).replace(/\.?0+$/, '')} 万`;
  return exact(number);
}
function amount(value) { return state.numberFormat === 'exact' ? exact(value) : compact(value); }
function percent(value) { return `${Math.max(0, Math.min(100, n(value) * 100)).toFixed(1)}%`; }
function dateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try { return new Intl.DateTimeFormat('zh-CN', { timeZone: state.data?.timezone || undefined, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date); }
  catch { return date.toLocaleString('zh-CN'); }
}
function duration(seconds) {
  if (!known(seconds) || Number(seconds) <= 0) return '—';
  if (seconds < 60) return `${decimal(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
}
function ttft(value) { return known(value) ? `${decimal(Number(value) / 1000)} 秒` : '无记录'; }
function setMetric(id, value, raw) {
  text(id, value);
  $(id).title = raw === undefined ? '' : `${exact(raw)} tokens`;
}
function selection() { return { days: state.days, scope: state.scope, model: state.model, provider: state.provider, origin: state.origin, ...(state.days === 'custom' ? { start: state.start, end: state.end } : {}) }; }
function queryParams(filters) { return new URLSearchParams(Object.entries(filters).filter(([, value]) => value !== undefined && value !== null)); }
function sourceFilterLabel(filters = {}) { return `${providerLabels[filters.provider || 'all'] || '未知代理工具'} · ${originLabels[filters.origin || 'all'] || '未知 Paseo 关联'}`; }
function sourceBadges(row) {
  const provider = providerLabels[row.provider || 'codex'] || '未知代理工具';
  const linked = row.origin === 'paseo';
  const mixed = row.origin === 'mixed';
  return `<span class="provider-badge">${esc(provider)}</span><span class="origin-badge${linked || mixed ? ' linked' : ''}" title="按本机 Paseo 元数据关联整个会话；不代表该会话最初由 Paseo 创建">${mixed ? '部分关联 Paseo' : linked ? '已关联 Paseo' : '未关联 Paseo'}</span>`;
}
function reasoningText(row) {
  const coverage = row.reasoning_coverage || (!row.provider || row.provider === 'codex' ? 'full' : 'none');
  if (coverage === 'none') return '未单列（不等于 0）';
  if (coverage === 'partial') return `已记录部分：${exact(row.reasoning_output_tokens)} tokens${known(row.reasoning_unknown_requests) ? `（${exact(row.reasoning_unknown_requests)} 条未单列）` : ''}`;
  return known(row.reasoning_output_tokens) ? `${exact(row.reasoning_output_tokens)} tokens` : '未单列（不等于 0）';
}
function filtersLabel(filters = {}, range) {
  const dateLabel = range?.start && range?.end ? `${range.start} 至 ${range.end}` : rangeLabels[filters.days] || '所选范围';
  return `${dateLabel} · ${scopes[filters.scope]?.label || '任务用量'} · ${filters.model === 'all' ? '全部模型' : filters.model || '全部模型'} · ${sourceFilterLabel(filters)}`;
}
function renderContext(data) {
  const filters = state.applied;
  text('active-filters', filtersLabel(filters, data.range));
  text('scope-description', `${scopes[filters.scope]?.description || '按当前统计对象筛选'} · Paseo 关联按整个会话分类，含恢复或接管的历史记录`);
  text('sessions-scope-note', `${scopes[filters.scope]?.description || ''}。点击名称查看详情。`);
  const available = [...new Set(data.filters?.available_models || (data.models || []).map((m) => m.model))].filter(Boolean);
  if (state.model !== 'all' && !available.includes(state.model)) available.push(state.model);
  $('model-select').innerHTML = '<option value="all">全部模型</option>' + available.sort().map((model) => `<option value="${esc(model)}">${esc(model)}</option>`).join('');
  $('model-select').value = state.model;
  updateExport();
}
function updateExport() {
  const link = $('export-button');
  if (!state.data || !state.applied) { link.removeAttribute('href'); link.setAttribute('aria-disabled', 'true'); return; }
  const params = queryParams(state.applied);
  params.set('type', $('export-type').value);
  link.href = `/api/export.csv?${params}`;
  link.setAttribute('aria-disabled', 'false');
  link.title = `导出当前已展示的数据：${filtersLabel(state.applied, state.data.range)}`;
}
function renderSummary(data) {
  const s = data.summary || {};
  $('metrics').classList.toggle('precise', state.numberFormat === 'exact');
  setMetric('today-value', amount(s.today_tokens), s.today_tokens);
  setMetric('total-value', amount(s.total_tokens), s.total_tokens);
  setMetric('output-total-value', amount(s.output_tokens), s.output_tokens);
  setMetric('sessions-value', exact(s.session_count));
  text('today-description', `今日 · 同对象 / 模型 / 代理 / 关联 · ${data.timezone || '本机时区'}`);
  text('output-description', `推理输出：${reasoningText(s)} · 已记录部分包含在输出中`);
  text('total-description', `输入 + 输出 · ${exact(s.request_count)} 条用量记录`);
  text('composition-total', amount(s.total_tokens));
  $('composition-total').title = `${exact(s.total_tokens)} tokens`;
  const values = [s.uncached_input_tokens ?? Math.max(0, n(s.input_tokens) - n(s.cached_input_tokens)), s.cached_input_tokens, s.output_tokens];
  ['uncached-value', 'cached-value', 'output-value'].forEach((id, i) => setMetric(id, amount(values[i]), values[i]));
  text('cache-rate', percent(n(s.cache_hit_rate) / 100));
  const total = values.reduce((sum, item) => sum + n(item), 0);
  [...$('composition-bar').children].forEach((element, i) => { element.style.width = total ? `${n(values[i]) / total * 100}%` : '0%'; });
  $('composition-bar').setAttribute('aria-label', `非缓存输入 ${exact(values[0])}，缓存输入 ${exact(values[1])}，输出 ${exact(values[2])} tokens`);
  $('metrics').setAttribute('aria-busy', 'false');
  text('average-speed', speed(average(s)));
  text('combined-speed', speed(s.combined_tps));
  text('average-formula', known(average(s)) ? `${exact(s.speed_output_tokens)} tokens ÷ ${decimal(s.active_seconds)} 秒 = ${speed(average(s))} tok/s` : '暂无可计算的完成样本');
  text('combined-formula', known(s.combined_tps) ? `${exact(s.speed_output_tokens)} tokens ÷ ${decimal(s.active_wall_seconds)} 秒 = ${speed(s.combined_tps)} tok/s` : '暂无可计算的完成样本');
  text('speed-samples', `${exact(samples(s))} 个完成轮次`);
  text('speed-excluded', `${exact(s.excluded_turn_count)} 轮缺少完整时间`);
  text('parallelism-value', known(s.parallelism) ? `${Number(s.parallelism).toFixed(2)}×` : '—');
  text('ttft-value', ttft(s.ttft_median_ms));
  const notes = [`速度样本输出 ${exact(s.speed_output_tokens)} tokens；用量总览可能包含尚无完整时间记录的输出。`];
  if (n(s.mixed_model_excluded_turn_count)) notes.push(`另有 ${exact(s.mixed_model_excluded_turn_count)} 个混合模型轮次无法拆分耗时，未参与当前模型速度。`);
  if (n(s.outside_range_turn_count)) notes.push(`另有 ${exact(s.outside_range_turn_count)} 轮的完成日期在所选范围外。`);
  notes.push('缺少完整时间可能来自未完成轮次、日志缺失或当前代理存储格式不支持。首 token 等待仅基于有记录的完成样本。');
  text('speed-coverage', notes.join(''));
}
function niceMax(value) {
  if (value <= 0) return 100;
  const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
  const normalized = value / magnitude;
  return (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 4 ? 4 : normalized <= 5 ? 5 : normalized <= 8 ? 8 : 10) * magnitude;
}
function renderChart(data) {
  const days = data.daily || [], outputOnly = state.chartMetric === 'output';
  const field = outputOnly ? 'output_tokens' : 'total_tokens';
  text('trend-range', data.range?.start && data.range?.end ? `${data.range.start} — ${data.range.end}` : '本地会话日志');
  document.querySelectorAll('[data-chart-metric]').forEach((button) => { const active = button.dataset.chartMetric === state.chartMetric; button.classList.toggle('selected', active); button.setAttribute('aria-pressed', String(active)); });
  $('chart-legend').innerHTML = outputOnly ? '<span><i class="legend-dot output"></i>输出 tokens（含已记录的推理输出）</span>' : '<span><i class="legend-dot uncached"></i>非缓存输入</span><span><i class="legend-dot cached"></i>缓存输入</span><span><i class="legend-dot output"></i>输出</span>';
  if (!days.length || !days.some((day) => n(day[field]) > 0)) {
    $('daily-chart').innerHTML = `<div class="chart-placeholder"><span>这段时间没有${outputOnly ? '输出' : '用量'}记录</span><small>可调整日期、统计对象、模型、代理工具或 Paseo 关联。</small></div>`;
    return;
  }
  const width = Math.max(320, $('daily-chart').clientWidth - 30);
  const height = 225, left = 47, right = 12, top = 15, bottom = 30;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const max = niceMax(Math.max(...days.map((day) => Math.max(0, n(day[field])))));
  const slotWidth = plotWidth / days.length;
  const barWidth = Math.min(38, Math.max(.6, slotWidth * (days.length > 35 ? .76 : .5)));
  const labelStep = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(plotWidth / 65))));
  let markup = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="chart-title chart-description"><title id="chart-title">每日${outputOnly ? '输出' : '总'}用量</title><desc id="chart-description">${esc(days[0].date)} 至 ${esc(days[days.length - 1].date)}，${outputOnly ? '输出 token' : '非缓存输入、缓存输入、输出堆叠'}。可导出每日 CSV 查看详细数值。</desc>`;
  for (let i = 0; i <= 4; i++) {
    const y = top + plotHeight * (1 - i / 4);
    markup += `<line class="chart-gridline" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text class="axis-text" x="${left - 9}" y="${y + 3}" text-anchor="end">${esc(compact(max * i / 4))}</text>`;
  }
  days.forEach((day, i) => {
    const x = left + i * slotWidth + (slotWidth - barWidth) / 2;
    const values = outputOnly ? [0, 0, n(day.output_tokens)] : [Math.max(0, n(day.input_tokens) - n(day.cached_input_tokens)), n(day.cached_input_tokens), n(day.output_tokens)];
    const description = `${day.date}：总计 ${exact(day.total_tokens)}；非缓存输入 ${exact(Math.max(0, n(day.input_tokens) - n(day.cached_input_tokens)))}；缓存输入 ${exact(day.cached_input_tokens)}；输出 ${exact(day.output_tokens)} tokens`;
    let y = top + plotHeight;
    markup += `<g class="bar-group" tabindex="0" role="img" aria-label="${esc(description)}"><title>${esc(description)}</title>`;
    values.forEach((value, j) => {
      const barHeight = value / max * plotHeight;
      y -= barHeight;
      if (barHeight > 0) markup += `<rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" fill="${['#7e907a', '#c4d1bb', '#ce8a60'][j]}" rx="${Math.min(2, barWidth / 4)}"/>`;
    });
    markup += '</g>';
    if ((i % labelStep === 0 && days.length - 1 - i >= Math.max(1, labelStep * .55)) || i === days.length - 1) markup += `<text class="axis-text" x="${left + (i + .5) * slotWidth}" y="${height - 8}" text-anchor="middle">${esc(String(day.date || '').slice(5).replace('-', '/'))}</text>`;
  });
  $('daily-chart').innerHTML = markup + '</svg>';
}
function renderModels(data) {
  const field = state.modelMetric === 'output' ? 'output_tokens' : 'total_tokens';
  const models = [...(data.models || [])].sort((a, b) => n(b[field]) - n(a[field]));
  if (!models.length) { $('models-list').innerHTML = '<span class="muted">暂无模型用量记录</span>'; return; }
  const total = models.reduce((sum, model) => sum + n(model[field]), 0);
  const visible = models.slice(0, 5);
  if (models.length > 5) visible.push({ model: `其他 ${models.length - 5} 个模型`, [field]: models.slice(5).reduce((sum, model) => sum + n(model[field]), 0) });
  $('models-list').innerHTML = visible.map((model) => {
    const share = total ? n(model[field]) / total : 0;
    return `<div class="model-item"><div class="model-item-header"><span class="model-name" title="${esc(model.model || '未知模型')}">${esc(model.model || '未知模型')}</span><span class="model-share">${percent(share)}</span></div><div class="model-bar"><span style="width:${share * 100}%"></span></div><div class="model-tokens" title="${exact(model[field])} tokens">${amount(model[field])} ${state.modelMetric === 'output' ? '输出 ' : ''}tokens</div></div>`;
  }).join('');
}
function detailRow(session, detailId) {
  const turns = Array.isArray(session.turns) ? session.turns : [];
  const models = session.models?.length ? session.models.join('、') : session.model || '未知';
  const turnRows = turns.map((turn) => `<tr><td>${esc(dateTime(turn.completed_at))}</td><td>${esc(categoryLabels[turn.category] || '未知')}<span class="turn-source-badges">${sourceBadges(turn)}</span></td><td>${esc(turn.model || '未知')}</td><td>${exact(turn.output_tokens)}</td><td>${duration(turn.duration_seconds)}</td><td>${speed(turn.output_tps)} tok/s</td><td>${ttft(turn.ttft_ms)}</td></tr>`).join('');
  const notes = [`基于 ${exact(samples(session))} 个完整轮次、${exact(session.speed_output_tokens)} 个输出 tokens；${exact(session.excluded_turn_count)} 轮缺少完整时间。`];
  if (n(session.mixed_model_excluded_turn_count)) notes.push(`${exact(session.mixed_model_excluded_turn_count)} 个混合模型轮次无法拆分耗时。`);
  if (n(session.outside_range_turn_count)) notes.push(`${exact(session.outside_range_turn_count)} 轮的完成日期在范围外。`);
  return `<tr class="session-detail" id="${detailId}"><td colspan="6"><p class="detail-heading">当前筛选下的对话统计 <span class="detail-source-badges">${sourceBadges(session)}</span></p><dl class="detail-grid"><div><dt>平均轮次速度</dt><dd>${speed(average(session))} tok/s<small>输出 ÷ 累计耗时</small></dd></div><div><dt>活跃期间合计吞吐</dt><dd>${speed(session.combined_tps)} tok/s<small>同一批输出 ÷ 区间并集</small></dd></div><div><dt>累计轮次耗时</dt><dd>${duration(session.active_seconds)}<small>${decimal(session.active_seconds)} 秒</small></dd></div><div><dt>活跃时长 · 重叠只计一次</dt><dd>${duration(session.active_wall_seconds)}<small>${decimal(session.active_wall_seconds)} 秒</small></dd></div><div><dt>平均并行系数</dt><dd>${known(session.parallelism) ? `${Number(session.parallelism).toFixed(2)}×` : '—'}</dd></div><div><dt>首 token 等待中位数</dt><dd>${ttft(session.ttft_median_ms)}</dd></div><div><dt>推理输出 · 已含在输出</dt><dd>${esc(reasoningText(session))}</dd></div><div><dt>模型</dt><dd>${esc(models)}</dd></div></dl><p class="detail-note">${esc(notes.join(''))}速度包含思考、工具与等待，按完成日期筛选。</p><div class="source-breakdown"><span>主对话<strong>${exact(session.main_tokens ?? session.total_tokens)}</strong></span><span>子代理<strong>${exact(session.agent_tokens)}</strong></span><span>自动审批<strong>${exact(session.review_tokens)}</strong></span><span>单位：tokens · 仅当前筛选计入部分</span></div><details class="details-box" data-turn-details="${esc(session.id)}"${state.expandedTurns.has(session.id) ? ' open' : ''}><summary>最近已完成轮次 · ${exact(session.turns_total ?? turns.length)} 轮${n(session.turns_total) > turns.length ? `，展示最近 ${exact(turns.length)} 轮` : ''}</summary>${turns.length ? `<div class="turn-scroll"><table class="turn-table"><thead><tr><th>完成时间</th><th>来源</th><th>模型</th><th>输出 tokens</th><th>轮次耗时</th><th>平均有效速度</th><th>首 token 等待</th></tr></thead><tbody>${turnRows}</tbody></table></div><p class="turn-note">此处只展示参与速度计算的轮次；导出「已完成轮次」可获取当前范围完整样本。</p>` : '<p class="turn-note">当前筛选下没有具备完整时间的已完成轮次。</p>'}</details><details class="details-box"><summary>记录标识与时间</summary><p class="detail-id">对话 ID：${esc(session.id)}<br>首次记录 ${esc(dateTime(session.first_seen))} · 最近记录 ${esc(dateTime(session.last_seen))}${session.parent_id ? `<br>父对话 ID：${esc(session.parent_id)}` : ''}</p></details></td></tr>`;
}
function renderSessions() {
  const focusedId = document.activeElement?.dataset?.sessionId;
  const sessions = state.data?.sessions || [];
  const term = state.search.trim().toLocaleLowerCase();
  const visible = sessions.filter((s) => [s.title, s.model, ...(s.models || []), s.id].join(' ').toLocaleLowerCase().includes(term));
  visible.sort((a, b) => {
    if (state.sort === 'recent') return (new Date(b.last_seen).getTime() || 0) - (new Date(a.last_seen).getTime() || 0);
    if (state.sort === 'speed') return (known(average(b)) ? n(average(b)) : -Infinity) - (known(average(a)) ? n(average(a)) : -Infinity) || n(b.total_tokens) - n(a.total_tokens);
    return n(b[state.sort === 'output' ? 'output_tokens' : 'total_tokens']) - n(a[state.sort === 'output' ? 'output_tokens' : 'total_tokens']);
  });
  text('conversation-count', exact(sessions.length));
  text('table-count', term ? `找到 ${exact(visible.length)} / ${exact(sessions.length)} 个对话` : `共 ${exact(sessions.length)} 个主对话分组`);
  if (!visible.length) {
    $('sessions-body').innerHTML = `<tr><td colspan="6" class="table-empty">${term ? '没有找到匹配的对话。试试其他关键词。' : '当前筛选下没有对话记录。<br>可调整日期、统计对象、模型、代理工具或 Paseo 关联。'}</td></tr>`;
    return;
  }
  $('sessions-body').innerHTML = visible.map((session, index) => {
    const open = state.expanded.has(session.id), detailId = `session-detail-${index}`;
    const title = session.title || '未命名对话';
    const model = session.model || session.models?.[0] || '未知模型';
    return `<tr class="session-row${open ? ' expanded' : ''}"><td><button class="session-title-button" type="button" data-session-id="${esc(session.id)}" aria-expanded="${open}"${open ? ` aria-controls="${detailId}"` : ''}><span class="session-symbol" aria-hidden="true">▤</span><span><span class="session-name" title="${esc(title)}">${esc(title)}</span><span class="session-meta">${sourceBadges(session)}<span class="model-label">${esc(model)}</span>${n(session.child_count) ? `<span class="agent-label">${exact(session.child_count)} 个子会话</span>` : ''}</span></span><span class="expand-chevron" aria-hidden="true">›</span></button></td><td class="token-total" title="${exact(session.total_tokens)} tokens">${amount(session.total_tokens)}</td><td><span class="input-output"><span title="${exact(session.input_tokens)} tokens">${amount(session.input_tokens)}<small>输入</small></span><span title="${exact(session.output_tokens)} tokens">${amount(session.output_tokens)}<small>输出</small></span></span></td><td title="${exact(session.cached_input_tokens)} tokens">${amount(session.cached_input_tokens)}</td><td class="speed-cell"><strong>${speed(average(session))}</strong>${known(average(session)) ? '<small>tok/s</small>' : ''}</td><td class="last-seen">${esc(dateTime(session.last_seen))}</td></tr>${open ? detailRow(session, detailId) : ''}`;
  }).join('');
  if (focusedId) [...$('sessions-body').querySelectorAll('button[data-session-id]')].find((item) => item.dataset.sessionId === focusedId)?.focus({ preventScroll: true });
}
function renderSource(data) {
  const source = data.source || {};
  text('source-path', source.path || '—');
  const diagnostics = source.providers || {};
  text('source-providers', Object.entries(providerLabels).filter(([key]) => key !== 'all').map(([key, label]) => `${label}：${exact(diagnostics[key]?.files ?? (key === 'codex' ? source.files : 0))} 个文件 / 会话记录`).join(' · '));
  text('source-count', `${exact(source.files)} 个日志文件 · ${exact(source.sessions)} 个会话线程（全本机读取范围）`);
  text('source-timezone', data.timezone || '本机时区');
  const warnings = Array.isArray(source.warnings) ? source.warnings : [];
  text('source-capabilities', 'Codex、Claude Code：具备可靠起止记录时统计轮次速度。OpenCode 1.x：仅用量；2.x：仅闭合成功轮次可测速，未闭合不推断运行状态。');
  text('source-status', warnings.length ? `${warnings.length} 条读取提示` : '读取正常');
  $('source-warnings').innerHTML = warnings.map((warning) => `<li>${esc(typeof warning === 'string' ? warning : JSON.stringify(warning))}</li>`).join('');
  if (source.speed_note) text('source-speed-note', source.speed_note);
}
function renderAll() { if (!state.data) return; renderContext(state.data); renderSummary(state.data); renderChart(state.data); renderModels(state.data); renderSessions(); renderSource(state.data); }
function liveText(element, value) { const next = String(value ?? ''); if (element.textContent !== next) element.textContent = next; }
function liveTime(value) {
  if (!value) return '尚无记录';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  try { return new Intl.DateTimeFormat('zh-CN', { timeZone: live.data?.timezone || state.data?.timezone || undefined, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date); }
  catch { return date.toLocaleString('zh-CN'); }
}
function renderLiveStatus() {
  const stale = Boolean(live.error);
  $('live-section').classList.toggle('live-data-stale', stale);
  $('live-section').classList.toggle('live-paused', !state.autoRefresh);
  liveText($('live-status'), stale ? '更新失败 · 数据已过期' : !state.autoRefresh ? '自动刷新已暂停' : document.hidden ? '页面隐藏 · 暂停读取' : live.data ? '每 10 秒更新' : '正在连接…');
  $('live-error').hidden = !stale;
  if (stale) {
    const old = live.data ? `保留 ${liveTime(live.data.generated_at)} 的快照（${scopes[live.data.filters?.scope]?.label || '任务用量'} · ${live.data.filters?.model === 'all' ? '全部模型' : live.data.filters?.model || '全部模型'} · ${sourceFilterLabel(live.data.filters)}），以下数值不是当前速度。` : '尚无可显示的实时数据。';
    liveText($('live-error'), `实时更新失败。${old}请确认本地服务正在运行。（${live.error}）`);
  }
  if (live.data) liveText($('live-updated-at'), `读取于 ${liveTime(live.data.generated_at)}${!state.autoRefresh ? ' · 数值已暂停' : ''}`);
}
function makeLiveCard(key) {
  const article = document.createElement('article');
  article.className = 'live-card';
  article.dataset.liveKey = key;
  article.innerHTML = '<div class="live-card-header"><div class="live-identity"><div class="live-title-row"><h3 data-live="title"></h3><span class="live-category" data-live="category"></span><span class="provider-badge" data-live="provider"></span><span class="origin-badge" data-live="origin"></span></div><p class="live-parent" data-live="parent"></p><p class="live-model" data-live="model"></p></div><span class="live-state-pill" data-live="status"></span></div><dl class="live-metrics"><div><dt>本轮已记录输出</dt><dd><strong data-live="output"></strong><span> tokens</span></dd><small data-live="requests"></small><small data-live="reasoning"></small></div><div><dt>本轮已耗时</dt><dd data-live="elapsed"></dd><small data-live="started"></small></div><div class="live-speed-metric"><dt>本轮平均 · 估算</dt><dd><strong data-live="average"></strong><span data-live="average-unit"></span></dd><small data-live="average-note"></small></div><div class="live-speed-metric"><dt data-live="rolling-label">近 60 秒记录速率</dt><dd><strong data-live="rolling"></strong><span data-live="rolling-unit"></span></dd><small data-live="rolling-note"></small></div></dl><div class="live-card-footer"><span data-live="usage"></span><span data-live="activity"></span></div>';
  const fields = Object.fromEntries([...article.querySelectorAll('[data-live]')].map((element) => [element.dataset.live, element]));
  const card = { article, fields };
  live.nodes.set(key, card);
  return card;
}
function updateLiveCard(card, thread) {
  const { article, fields } = card;
  const active = thread.status === 'active';
  const hasUsage = n(thread.request_count) > 0;
  const modelFiltered = live.data?.filters?.model !== 'all';
  const mixed = Boolean(thread.mixed_model) && modelFiltered;
  article.classList.toggle('uncertain', !active);
  liveText(fields.title, thread.title || '未命名对话');
  fields.title.title = `${thread.title || '未命名对话'} · ${thread.thread_id}`;
  liveText(fields.category, categoryLabels[thread.category] || '对话');
  liveText(fields.provider, providerLabels[thread.provider || 'codex'] || '未知代理工具');
  liveText(fields.origin, thread.origin === 'paseo' ? '已关联 Paseo' : '未关联 Paseo');
  fields.origin.classList.toggle('linked', thread.origin === 'paseo');
  fields.origin.title = '按本机 Paseo 元数据关联整个会话；不代表最初由 Paseo 创建';
  liveText(fields.reasoning, `推理输出：${reasoningText(thread)}`);
  fields.parent.hidden = !thread.root_id || thread.root_id === thread.thread_id;
  liveText(fields.parent, `所属主对话：${thread.root_title || thread.root_id || '未知'}`);
  liveText(fields.model, thread.models?.length ? thread.models.join(' / ') : thread.model || '模型尚未记录');
  liveText(fields.status, active ? '最近活跃' : '状态待确认');
  liveText(fields.output, amount(thread.output_tokens));
  fields.output.title = `${exact(thread.output_tokens)} tokens`;
  liveText(fields.requests, hasUsage ? `${exact(thread.request_count)} 条用量记录` : '等待首次用量写入');
  liveText(fields.elapsed, known(thread.elapsed_seconds) ? n(thread.elapsed_seconds) > 0 ? duration(thread.elapsed_seconds) : '0 秒' : '—');
  liveText(fields.started, `开始于 ${liveTime(thread.started_at)}`);
  const avgAvailable = active && hasUsage && known(thread.average_tps) && !mixed;
  const rollingAvailable = active && hasUsage && known(thread.rolling_tps);
  liveText(fields.average, avgAvailable ? speed(thread.average_tps) : active && !hasUsage ? '等待用量' : '—');
  liveText(fields['average-unit'], avgAvailable ? ' tok/s' : '');
  liveText(fields['average-note'], !active ? '长时间无更新，暂不测速' : mixed ? '混合模型，无法拆分耗时' : hasUsage ? known(thread.average_tps) ? '已记录输出 ÷ 已耗时' : '缺少可靠轮次时间，未测速' : '响应结束后才有记录');
  liveText(fields['rolling-label'], `近 ${exact(live.data?.window_seconds || 60)} 秒记录速率`);
  liveText(fields.rolling, rollingAvailable ? speed(thread.rolling_tps) : active && !hasUsage ? '等待用量' : '—');
  liveText(fields['rolling-unit'], rollingAvailable ? ' tok/s' : '');
  liveText(fields['rolling-note'], !active ? '等待新的日志更新' : !hasUsage ? '尚未收到本轮用量' : n(thread.rolling_output_tokens) === 0 ? '窗口内无新增记录 ≠ 已停止' : `${exact(thread.rolling_output_tokens)} tokens / ${decimal(thread.window_seconds)} 秒窗口`);
  liveText(fields.usage, `上次用量：${liveTime(thread.last_usage_at)}`);
  liveText(fields.activity, `上次日志：${liveTime(thread.last_activity_at)}`);
}
function renderLive() {
  if (!live.data) { renderLiveStatus(); return; }
  const data = live.data;
  const filters = data.filters || {};
  liveText($('live-filter-note'), `${scopes[filters.scope]?.label || '任务用量'} · ${filters.model === 'all' ? '全部模型' : filters.model || '全部模型'} · ${sourceFilterLabel(filters)} · 不受日期范围影响`);
  const threads = [...data.threads].sort((a, b) => String(a.started_at || '').localeCompare(String(b.started_at || '')) || String(a.thread_id).localeCompare(String(b.thread_id)));
  const active = threads.filter((thread) => thread.status === 'active');
  const uncertain = threads.filter((thread) => thread.status === 'uncertain');
  liveText($('live-count'), `${exact(active.length)} 最近活跃`);
  liveText($('live-uncertain-count'), exact(uncertain.length));
  $('live-uncertain').hidden = !uncertain.length;
  $('live-empty').hidden = Boolean(active.length);
  liveText($('live-empty'), uncertain.length ? '没有检测到近期更新的运行中对话；可展开下方查看状态待确认的轮次。' : '当前筛选下，没有检测到近期更新且未结束的轮次。OpenCode 存储目前不提供可靠实时状态。');
  const visibleKeys = new Set();
  for (const [container, list] of [[$('live-threads'), active], [$('live-uncertain-threads'), uncertain]]) {
    let previous = null;
    for (const thread of list) {
      const key = `${thread.provider || 'codex'}:${thread.thread_id}:${thread.turn_id}`;
      visibleKeys.add(key);
      const card = live.nodes.get(key) || makeLiveCard(key);
      updateLiveCard(card, thread);
      const expected = previous ? previous.nextElementSibling : container.firstElementChild;
      if (expected !== card.article) container.insertBefore(card.article, expected);
      previous = card.article;
    }
  }
  for (const [key, card] of live.nodes) if (!visibleKeys.has(key)) { card.article.remove(); live.nodes.delete(key); }
  $('live-threads').setAttribute('aria-busy', 'false');
  const staleNote = n(data.stale_count) ? `另有 ${exact(data.stale_count)} 个长期未更新的未结束轮次已隐藏。` : '';
  liveText($('live-state-note'), `“最近活跃”由未见结束事件且最近 ${decimal(n(data.fresh_seconds || 300) / 60)} 分钟内有日志更新推断，不能保证此刻仍在生成。${staleNote}`);
  const warningMarkup = (data.warnings || []).map((warning) => `<li>${esc(typeof warning === 'string' ? warning : JSON.stringify(warning))}</li>`).join('');
  if ($('live-warnings').innerHTML !== warningMarkup) $('live-warnings').innerHTML = warningMarkup;
  renderLiveStatus();
}
async function loadLive() {
  const requested = { scope: state.scope, model: state.model, provider: state.provider, origin: state.origin };
  const requestId = ++live.requestId;
  live.controller?.abort();
  const controller = new AbortController();
  live.controller = controller;
  live.loading = true;
  if (!live.data) { $('live-threads').setAttribute('aria-busy', 'true'); liveText($('live-empty'), '正在读取当前轮次…'); }
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(`/api/live?${queryParams(requested)}`, { cache: 'no-store', signal: controller.signal });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).error || message; } catch { /* Keep HTTP error. */ }
      throw new Error(message);
    }
    const data = await response.json();
    if (!Array.isArray(data?.threads) || !data.generated_at || !data.filters) throw new Error('实时数据格式不完整');
    if (requestId !== live.requestId) return;
    live.data = data;
    live.error = '';
    renderLive();
  } catch (error) {
    if (requestId !== live.requestId) return;
    live.error = error.name === 'AbortError' ? '读取超时' : error.message;
    renderLiveStatus();
    $('live-threads').setAttribute('aria-busy', 'false');
    if (!live.data) liveText($('live-empty'), '实时数据暂不可用，服务恢复后将重新读取。');
  } finally {
    clearTimeout(timeout);
    if (requestId === live.requestId) live.loading = false;
  }
}
function cancelLiveRequest() {
  ++live.requestId;
  live.controller?.abort();
  live.loading = false;
  $('live-threads').setAttribute('aria-busy', 'false');
  if (!live.data && !state.autoRefresh) liveText($('live-empty'), '读取已暂停。点击刷新可获取一次当前快照。');
}
function validDates() {
  if (state.days !== 'custom') return true;
  if (!state.start || !state.end || state.start > state.end) {
    text('error-banner', '请填写开始和结束日期，并确保开始日期不晚于结束日期。当前仍保留上次成功加载的数据。');
    $('error-banner').hidden = false;
    return false;
  }
  return true;
}
async function loadData() {
  if (!validDates()) return;
  const requested = selection();
  const requestId = ++state.requestId;
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  state.loading = true;
  $('pending-status').hidden = false;
  text('pending-status', '正在更新…');
  $('refresh-button').classList.add('loading');
  $('refresh-button').disabled = true;
  $('refresh-button').setAttribute('aria-busy', 'true');
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(`/api/stats?${queryParams(requested)}`, { cache: 'no-store', signal: controller.signal });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { const problem = await response.json(); message = problem.error || message; } catch { /* Non-JSON error. */ }
      throw new Error(message);
    }
    const data = await response.json();
    if (!data?.summary || !Array.isArray(data.sessions) || !Array.isArray(data.daily)) throw new Error('数据格式不完整');
    if (requestId !== state.requestId) return;
    state.data = data;
    state.applied = { ...requested, scope: data.filters?.scope || requested.scope, model: data.filters?.model || requested.model, provider: data.filters?.provider || requested.provider, origin: data.filters?.origin || requested.origin };
    state.draftDates = false;
    $('error-banner').hidden = true;
    text('connection-status', state.autoRefresh ? '已连接 · 自动刷新' : '已连接 · 刷新已暂停');
    $('status-dot').classList.remove('offline');
    renderAll();
    text('updated-at', `最近更新 ${dateTime(data.generated_at || new Date().toISOString())}${state.autoRefresh ? '' : ' · 自动刷新已暂停'}`);
  } catch (error) {
    if (requestId !== state.requestId) return;
    text('connection-status', '连接暂时中断');
    $('status-dot').classList.add('offline');
    const saved = state.data ? `当前仍展示：${filtersLabel(state.applied, state.data.range)}。导出也使用此数据筛选。` : '尚未成功读取数据。';
    text('error-banner', `更新失败。${saved}请确认本地服务正在运行，再点击刷新。${error.name === 'AbortError' ? '读取超时，首次载入大量日志可能需要更长时间。' : `（${error.message}）`}`);
    $('error-banner').hidden = false;
    if (!state.data) {
      text('active-filters', '尚未加载统计数据');
      text('scope-description', '连接恢复后再展示统计范围');
      $('daily-chart').innerHTML = '<div class="chart-placeholder">暂无数据，请恢复本地服务后刷新。</div>';
      $('sessions-body').innerHTML = '<tr><td colspan="6" class="table-empty">尚未成功读取本地会话日志。</td></tr>';
      text('models-list', '等待连接恢复');
      $('metrics').setAttribute('aria-busy', 'false');
    }
  } finally {
    clearTimeout(timeout);
    if (requestId === state.requestId) {
      state.loading = false;
      $('pending-status').hidden = true;
      $('refresh-button').classList.remove('loading');
      $('refresh-button').disabled = false;
      $('refresh-button').setAttribute('aria-busy', 'false');
    }
  }
}
function showDateDraft() {
  if (state.loading) {
    ++state.requestId;
    state.controller?.abort();
    state.loading = false;
    $('refresh-button').classList.remove('loading');
    $('refresh-button').disabled = false;
    $('refresh-button').setAttribute('aria-busy', 'false');
  }
  state.draftDates = true;
  $('pending-status').hidden = false;
  text('pending-status', '选择日期后点击应用');
}
function localDate(date) { return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`; }
const now = new Date();
if (!state.end) state.end = localDate(now);
if (!state.start) { const before = new Date(now); before.setDate(before.getDate() - 6); state.start = localDate(before); }
$('range-select').value = state.days;
$('scope-select').value = state.scope;
$('provider-select').value = state.provider;
$('origin-select').value = state.origin;
$('sort-select').value = state.sort;
$('number-format').value = state.numberFormat;
$('model-metric').value = state.modelMetric;
$('auto-refresh').checked = state.autoRefresh;
$('start-date').value = state.start;
$('end-date').value = state.end;
$('custom-dates').hidden = state.days !== 'custom';
if (state.model !== 'all') { const option = document.createElement('option'); option.value = state.model; option.textContent = state.model; $('model-select').append(option); }
$('model-select').value = state.model;
$('range-select').addEventListener('change', (event) => {
  state.days = event.target.value;
  $('custom-dates').hidden = state.days !== 'custom';
  savePreferences();
  if (state.days === 'custom') { showDateDraft(); return; }
  state.draftDates = false;
  loadData();
});
$('filters-form').addEventListener('submit', (event) => { event.preventDefault(); state.start = $('start-date').value; state.end = $('end-date').value; if (!validDates()) return; state.draftDates = false; savePreferences(); loadData(); });
['start-date', 'end-date'].forEach((id) => $(id).addEventListener('input', showDateDraft));
['scope', 'model', 'provider', 'origin'].forEach((name) => $(`${name}-select`).addEventListener('change', (event) => { state[name] = event.target.value; savePreferences(); loadLive(); if (!state.draftDates) loadData(); }));
$('number-format').addEventListener('change', (event) => { state.numberFormat = event.target.value; savePreferences(); if (state.data) { renderSummary(state.data); renderModels(state.data); renderSessions(); } renderLive(); });
$('model-metric').addEventListener('change', (event) => { state.modelMetric = event.target.value; savePreferences(); if (state.data) renderModels(state.data); });
document.querySelectorAll('[data-chart-metric]').forEach((button) => button.addEventListener('click', () => { state.chartMetric = button.dataset.chartMetric; savePreferences(); if (state.data) renderChart(state.data); }));
$('refresh-button').addEventListener('click', () => { loadLive(); if (state.draftDates) { state.start = $('start-date').value; state.end = $('end-date').value; } if (validDates()) { state.draftDates = false; savePreferences(); loadData(); } });
$('auto-refresh').addEventListener('change', (event) => {
  state.autoRefresh = event.target.checked;
  savePreferences();
  if (state.data && !$('status-dot').classList.contains('offline')) text('connection-status', state.autoRefresh ? '已连接 · 自动刷新' : '已连接 · 刷新已暂停');
  if (state.data) text('updated-at', `最近更新 ${dateTime(state.data.generated_at)}${state.autoRefresh ? '' : ' · 自动刷新已暂停'}`);
  if (state.autoRefresh) {
    if (!document.hidden) { loadLive(); if (!state.loading && !state.draftDates) loadData(); }
  } else {
    cancelLiveRequest();
    if (state.loading) {
      ++state.requestId;
      state.controller?.abort();
      state.loading = false;
      $('pending-status').hidden = true;
      $('refresh-button').classList.remove('loading');
      $('refresh-button').disabled = false;
      $('refresh-button').setAttribute('aria-busy', 'false');
    }
  }
  renderLiveStatus();
});
$('export-type').addEventListener('change', updateExport);
$('export-button').addEventListener('click', (event) => { if (!state.data) event.preventDefault(); });
$('search-input').addEventListener('input', (event) => { state.search = event.target.value; if (state.data) renderSessions(); });
$('sort-select').addEventListener('change', (event) => { state.sort = event.target.value; savePreferences(); if (state.data) renderSessions(); });
$('sessions-body').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-session-id]');
  if (!button) return;
  const id = button.dataset.sessionId;
  if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
  renderSessions();
  [...$('sessions-body').querySelectorAll('button[data-session-id]')].find((item) => item.dataset.sessionId === id)?.focus({ preventScroll: true });
});
$('sessions-body').addEventListener('toggle', (event) => { const detail = event.target; if (!detail.matches?.('[data-turn-details]')) return; if (detail.open) state.expandedTurns.add(detail.dataset.turnDetails); else state.expandedTurns.delete(detail.dataset.turnDetails); }, true);
document.querySelectorAll('.nav-link').forEach((link) => link.addEventListener('click', () => document.querySelectorAll('.nav-link').forEach((item) => item.classList.toggle('active', item === link))));
const navigationSections = ['overview', 'live-section', 'speed-section', 'conversations', 'methodology'];
let navigationFrame = false;
function syncNavigation() {
  let current = navigationSections[0];
  for (const id of navigationSections) {
    if ($(id).getBoundingClientRect().top <= Math.max(96, window.innerHeight * .2)) current = id;
  }
  document.querySelectorAll('.nav-link').forEach((link) => link.classList.toggle('active', link.getAttribute('href') === `#${current}`));
  navigationFrame = false;
}
function scheduleNavigation() {
  if (!navigationFrame) { navigationFrame = true; requestAnimationFrame(syncNavigation); }
}
window.addEventListener('scroll', scheduleNavigation, { passive: true });
window.addEventListener('resize', scheduleNavigation);
scheduleNavigation();
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.data) renderChart(state.data); }, 120); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) cancelLiveRequest();
  else if (state.autoRefresh) { loadLive(); if (!state.loading && !state.draftDates) loadData(); }
  renderLiveStatus();
});
setInterval(() => { if (!document.hidden && state.autoRefresh && !state.loading && !state.draftDates) loadData(); }, 15000);
setInterval(() => { if (!document.hidden && state.autoRefresh && !live.loading) loadLive(); }, 10000);
loadData();
loadLive();
