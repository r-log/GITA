// ── State ──────────────────────────────────────────────────────────
let currentRepoId = null;
let activityChart = null;
let agentChart = null;

// ── API ───────────────────────────────────────────────────────────
async function api(path) {
  const res = await fetch(`/api/dashboard${path}`);
  return res.json();
}

// ── Helpers ───────────────────────────────────────────────────────
function badge(status) {
  const cls = `badge badge-${status}`;
  return `<span class="${cls}">${status}</span>`;
}

function timeAgo(dateStr) {
  if (!dateStr) return 'never';
  const diff = Date.now() - new Date(dateStr).getTime();
  const future = diff < 0;
  const absMs = Math.abs(diff);
  const mins = Math.floor(absMs / 60000);
  if (mins < 1) return 'just now';
  const fmt = (n, unit) => future ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (mins < 60) return fmt(mins, 'm');
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return fmt(hrs, 'h');
  const days = Math.floor(hrs / 24);
  return fmt(days, 'd');
}

function formatDuration(ms) {
  if (!ms) return '-';
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function formatDate(dateStr) {
  if (!dateStr) return '-';
  return new Date(dateStr).toLocaleString();
}

function jsonViewer(obj, maxHeight) {
  const style = maxHeight ? `max-height:${maxHeight}px` : '';
  return `<div class="json-viewer" style="${style}">${JSON.stringify(obj, null, 2)}</div>`;
}

// ── Persistence helpers ───────────────────────────────────────────
const LS_REPO_KEY = 'gita.selectedRepoId';
const LS_TAB_KEY = 'gita.activeTab';

function setActiveTab(tab) {
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const btn = document.querySelector(`nav button[data-tab="${tab}"]`);
  const panel = document.getElementById(`tab-${tab}`);
  if (!btn || !panel) return false;
  btn.classList.add('active');
  panel.classList.add('active');
  localStorage.setItem(LS_TAB_KEY, tab);
  return true;
}

// ── Tab Navigation ────────────────────────────────────────────────
document.querySelectorAll('nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    setActiveTab(btn.dataset.tab);
    loadTabData(btn.dataset.tab);
  });
});

// ── Repo Selector ─────────────────────────────────────────────────
const repoSelector = document.getElementById('repo-selector');
repoSelector.addEventListener('change', () => {
  currentRepoId = repoSelector.value ? parseInt(repoSelector.value) : null;
  if (currentRepoId) {
    localStorage.setItem(LS_REPO_KEY, String(currentRepoId));
    const activeTab = document.querySelector('nav button.active').dataset.tab;
    loadTabData(activeTab);
  } else {
    localStorage.removeItem(LS_REPO_KEY);
  }
});

async function loadRepos() {
  const repos = await api('/repos');
  repos.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.full_name;
    repoSelector.appendChild(opt);
  });

  // Restore previously selected repo if it still exists, otherwise
  // fall back to the only repo if there's just one.
  const savedRepoId = parseInt(localStorage.getItem(LS_REPO_KEY) || '', 10);
  const savedExists = repos.some(r => r.id === savedRepoId);
  if (savedExists) {
    repoSelector.value = String(savedRepoId);
    currentRepoId = savedRepoId;
  } else if (repos.length === 1) {
    repoSelector.value = repos[0].id;
    currentRepoId = repos[0].id;
    localStorage.setItem(LS_REPO_KEY, String(currentRepoId));
  }

  // Restore the tab the user was last on (fall back to overview)
  const savedTab = localStorage.getItem(LS_TAB_KEY) || 'overview';
  const activeTab = setActiveTab(savedTab) ? savedTab : 'overview';
  if (activeTab !== savedTab) setActiveTab('overview');

  if (currentRepoId) loadTabData(activeTab);
}

// ── Tab Data Loading ──────────────────────────────────────────────
function loadTabData(tab) {
  if (!currentRepoId) return;
  switch (tab) {
    case 'overview': loadOverview(); break;
    case 'timeline': loadTimeline(); break;
    case 'outcomes': loadOutcomes(); break;
    case 'runs': loadRuns(); break;
    case 'agents': loadAgents(); break;
    case 'issues': loadIssues(); break;
    case 'security': loadSecurity(); break;
    case 'context': loadContext(); break;
  }
}

// ── Outcomes Tab ──────────────────────────────────────────────────
let outcomesTrendChart = null;

async function loadOutcomes() {
  const data = await api(`/outcomes?repo_id=${currentRepoId}&days=30`);
  const headline = data.headline || {};

  // Headline — one sentence verdict
  const rate = headline.success_rate;
  let rateBadge = '';
  if (rate !== null && rate !== undefined) {
    const cls = rate >= 70 ? 'badge-success' : rate >= 40 ? 'badge-partial' : 'badge-failed';
    rateBadge = `<span class="badge ${cls}" style="margin-left:12px">${rate}% success rate</span>`;
  }
  const etaLine = headline.first_result_eta
    ? `<div class="subtitle" style="margin-top:6px">Next result expected ${timeAgo(headline.first_result_eta)}</div>`
    : '';
  document.getElementById('outcomes-headline').innerHTML = `
    <div class="headline-text">${headline.text || 'No data yet.'} ${rateBadge}</div>
    ${etaLine}
  `;

  // Trend chart — stacked bars: success / partial / failed per week
  const trend = data.trend || [];
  const ctx = document.getElementById('outcomes-trend');
  if (outcomesTrendChart) outcomesTrendChart.destroy();
  outcomesTrendChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: trend.map(t => t.week_start),
      datasets: [
        { label: 'Success',  data: trend.map(t => t.success),  backgroundColor: '#2dc653' },
        { label: 'Partial',  data: trend.map(t => t.partial),  backgroundColor: '#d29922' },
        { label: 'Failed',   data: trend.map(t => t.failed),   backgroundColor: '#f85149' },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: '#8b949e' } } },
      scales: {
        x: { stacked: true, ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
        y: { stacked: true, ticks: { color: '#8b949e' }, grid: { color: '#21262d' }, beginAtZero: true },
      },
    },
  });

  // By-agent breakdown
  const byAgent = data.by_agent || {};
  const agentNames = Object.keys(byAgent).sort();
  if (!agentNames.length) {
    document.getElementById('outcomes-by-agent').innerHTML = '<p class="subtitle">No agent activity in this period.</p>';
  } else {
    let html = '';
    agentNames.forEach(a => {
      const s = byAgent[a];
      const rate = s.success_rate;
      const rateStr = rate === null ? 'no signal yet' : `${rate}%`;
      const rateClass = rate === null ? '' : rate >= 70 ? 'badge-success' : rate >= 40 ? 'badge-partial' : 'badge-failed';
      html += `
        <div style="margin-bottom:10px">
          <div><strong>${a}</strong> <span class="badge ${rateClass}">${rateStr}</span></div>
          <div class="subtitle">${s.success} worked · ${s.partial} partial · ${s.failed} failed · ${s.pending} pending (${s.total} total)</div>
        </div>
      `;
    });
    document.getElementById('outcomes-by-agent').innerHTML = html;
  }

  // Wins list — narrative cards
  const wins = data.wins || [];
  if (!wins.length) {
    document.getElementById('outcomes-wins').innerHTML = '<p class="subtitle">No wins recorded yet. Signal takes 24-72h to accumulate after each intervention.</p>';
  } else {
    let html = '';
    wins.forEach(w => {
      html += `
        <div class="chart-card" style="margin-bottom:10px">
          <div>${w.story}</div>
          <div class="subtitle" style="margin-top:4px">
            ${timeAgo(w.checked_at || w.created_at)} · <code>${w.outcome_type}</code>
          </div>
        </div>
      `;
    });
    document.getElementById('outcomes-wins').innerHTML = html;
  }

  // Struggles list — narrative cards
  const struggles = data.struggles || [];
  if (!struggles.length) {
    document.getElementById('outcomes-struggles').innerHTML = '<p class="subtitle">No failures in the last 30 days.</p>';
  } else {
    let html = '';
    struggles.forEach(s => {
      html += `
        <div class="chart-card" style="margin-bottom:10px">
          <div>${s.story}</div>
          <div class="subtitle" style="margin-top:4px">
            ${timeAgo(s.checked_at || s.created_at)} · <code>${s.outcome_type}</code>
          </div>
        </div>
      `;
    });
    document.getElementById('outcomes-struggles').innerHTML = html;
  }
}

// ── Toast Notification ────────────────────────────────────────────
function showToast(message, type = 'success') {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ── Quick Actions ─────────────────────────────────────────────────
async function triggerAction(action) {
  if (!currentRepoId) return;
  const btn = event.target;
  btn.disabled = true;
  btn.classList.add('running');
  const origText = btn.textContent;
  btn.textContent = 'Running...';

  try {
    const res = await fetch('/api/dashboard/trigger', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({repo_id: currentRepoId, action}),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast(`${action} completed successfully`);
      loadOverview();
    } else {
      showToast(data.message || 'Action failed', 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.classList.remove('running');
    btn.textContent = origText;
  }
}

// ── Overview Tab ──────────────────────────────────────────────────
async function loadOverview() {
  const [stats, activity, agents, alerts, costs] = await Promise.all([
    api(`/stats?repo_id=${currentRepoId}`),
    api(`/activity?repo_id=${currentRepoId}&days=30`),
    api(`/agents?repo_id=${currentRepoId}&limit=20`),
    api(`/alerts?repo_id=${currentRepoId}`),
    api(`/costs?repo_id=${currentRepoId}&days=30`),
  ]);

  // Alerts (compact sidebar)
  renderAlerts(alerts);

  // Cost line is integrated into activity chart
  renderActivityChart(activity, costs);

  // Stats cards
  const issues = stats.issues_in_plan;
  document.getElementById('stats-cards').innerHTML = `
    <div class="stat-card">
      <div class="label">Agent Runs</div>
      <div class="value">${stats.total_agent_runs}</div>
      <div class="sub">${stats.status_counts.success || 0} successful</div>
    </div>
    <div class="stat-card">
      <div class="label">Milestones</div>
      <div class="value">${stats.milestones_count}</div>
      <div class="sub">${issues.total} tasks planned</div>
    </div>
    <div class="stat-card">
      <div class="label">Tasks Done</div>
      <div class="value">${issues.done}</div>
      <div class="sub">${issues.in_progress} in progress</div>
    </div>
    <div class="stat-card">
      <div class="label">Tasks Remaining</div>
      <div class="value">${issues.not_started}</div>
      <div class="sub">${issues.total} total</div>
    </div>
    <div class="stat-card">
      <div class="label">Last Onboarding</div>
      <div class="value" style="font-size:16px">${timeAgo(stats.last_onboarding)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Est. Cost (30d)</div>
      <div class="value" style="font-size:20px;color:#d29922">$${costs.total_cost_usd.toFixed(4)}</div>
      <div class="sub">${costs.total_llm_calls} LLM calls</div>
    </div>
  `;

  // Agent distribution chart
  renderAgentChart(stats.agent_run_counts);

  // Recent activity feed
  document.getElementById('activity-feed').innerHTML = agents.map(a => `
    <div class="feed-item">
      <span class="time">${formatDate(a.started_at)}</span>
      <span class="agent">${a.agent_name}</span>
      ${badge(a.status)}
      <span>${a.event_type || ''}</span>
      <span style="color:#8b949e">${formatDuration(a.duration_ms)}</span>
    </div>
  `).join('');
}

function renderActivityChart(data, costs) {
  const ctx = document.getElementById('activity-chart').getContext('2d');
  if (activityChart) activityChart.destroy();

  // Build cost data aligned to activity dates
  const dailyCosts = costs?.daily || {};
  const costData = data.map(d => dailyCosts[d.date]?.cost_usd || 0);
  const hasCostData = costData.some(c => c > 0);

  const datasets = [
    { label: 'Total Runs', data: data.map(d => d.total), borderColor: '#58a6ff', fill: false, tension: 0.3, yAxisID: 'y' },
    { label: 'Success', data: data.map(d => d.success), borderColor: '#2dc653', fill: false, tension: 0.3, yAxisID: 'y' },
    { label: 'Failed', data: data.map(d => d.failed), borderColor: '#f85149', fill: false, tension: 0.3, yAxisID: 'y' },
  ];

  if (hasCostData) {
    datasets.push({
      label: 'Cost ($)',
      data: costData,
      borderColor: '#d29922',
      backgroundColor: 'rgba(210, 153, 34, 0.1)',
      fill: true,
      tension: 0.3,
      yAxisID: 'cost',
      borderDash: [4, 4],
    });
  }

  const scales = {
    x: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
    y: { position: 'left', ticks: { color: '#8b949e' }, grid: { color: '#21262d' }, beginAtZero: true, title: { display: true, text: 'Runs', color: '#8b949e' } },
  };

  if (hasCostData) {
    scales.cost = {
      position: 'right',
      ticks: { color: '#d29922', callback: v => '$' + v.toFixed(3) },
      grid: { drawOnChartArea: false },
      beginAtZero: true,
      title: { display: true, text: 'Cost (USD)', color: '#d29922' },
    };
  }

  activityChart = new Chart(ctx, {
    type: 'line',
    data: { labels: data.map(d => d.date), datasets },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#8b949e', usePointStyle: true, pointStyle: 'line' } } },
      scales,
    },
  });
}

function renderAgentChart(counts) {
  const ctx = document.getElementById('agent-chart').getContext('2d');
  if (agentChart) agentChart.destroy();
  const labels = Object.keys(counts);
  const colors = ['#58a6ff', '#2dc653', '#d29922', '#f85149', '#bc8cff', '#f0883e'];
  agentChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data: Object.values(counts), backgroundColor: colors.slice(0, labels.length) }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: 'bottom', labels: { color: '#8b949e', padding: 12 } } },
    },
  });
}

// ── Alerts Panel (compact sidebar) ────────────────────────────────
function renderAlerts(alerts) {
  const content = document.getElementById('alerts-content');
  if (alerts.total === 0) {
    content.innerHTML = '<div class="alerts-empty">No alerts — all clear</div>';
    return;
  }

  const items = [];
  alerts.critical.forEach(a => {
    items.push(`<div class="alert-item">
      <span class="alert-dot critical"></span>
      <span class="alert-msg">${a.message.substring(0, 120)}</span>
      ${a.recommendation ? `<div class="alert-rec">${a.recommendation.substring(0, 100)}</div>` : ''}
    </div>`);
  });
  alerts.warnings.slice(0, 5).forEach(a => {
    items.push(`<div class="alert-item">
      <span class="alert-dot warning"></span>
      <span class="alert-msg">${a.message.substring(0, 120)}</span>
    </div>`);
  });

  const more = alerts.warnings.length > 5 ? `<div class="alerts-empty">+${alerts.warnings.length - 5} more warnings</div>` : '';
  content.innerHTML = items.join('') + more;
}

// ── Onboarding Runs Tab ───────────────────────────────────────────
async function loadRuns() {
  const status = document.getElementById('runs-status-filter').value;
  const params = `repo_id=${currentRepoId}${status ? `&status=${status}` : ''}`;
  const runs = await api(`/runs?${params}`);

  document.getElementById('runs-table').innerHTML = `
    <table>
      <thead><tr>
        <th>ID</th><th>Status</th><th>Issues Created</th><th>Confidence</th><th>Actions</th><th>Completed</th>
      </tr></thead>
      <tbody>
        ${runs.map(r => `
          <tr class="clickable" onclick="toggleRunDetail(${r.id}, this)">
            <td>#${r.id}</td>
            <td>${badge(r.status)}</td>
            <td>${r.issues_created}</td>
            <td>${r.confidence ? (r.confidence * 100).toFixed(0) + '%' : '-'}</td>
            <td>${r.actions_count}</td>
            <td>${timeAgo(r.completed_at)}</td>
          </tr>
          <tr class="detail-row" id="run-detail-${r.id}">
            <td colspan="6"><div class="detail-content loading">Loading...</div></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

document.getElementById('runs-status-filter').addEventListener('change', () => {
  if (currentRepoId) loadRuns();
});

async function toggleRunDetail(runId, row) {
  const detail = document.getElementById(`run-detail-${runId}`);
  if (detail.classList.contains('open')) {
    detail.classList.remove('open');
    return;
  }
  detail.classList.add('open');

  const run = await api(`/run/${runId}`);
  detail.querySelector('.detail-content').innerHTML = `
    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Repo Snapshot</div>
    <div class="collapsible-content">${jsonViewer(run.repo_snapshot, 400)}</div>

    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Suggested Plan</div>
    <div class="collapsible-content">${jsonViewer(run.suggested_plan, 400)}</div>

    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Actions Taken (${Array.isArray(run.actions_taken) ? run.actions_taken.length : 0})</div>
    <div class="collapsible-content">${jsonViewer(run.actions_taken, 400)}</div>

    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Existing State</div>
    <div class="collapsible-content">${jsonViewer(run.existing_state, 300)}</div>
  `;
}

// ── Agent Runs Tab ────────────────────────────────────────────────
async function loadAgents() {
  const name = document.getElementById('agents-name-filter').value;
  const status = document.getElementById('agents-status-filter').value;
  let params = `repo_id=${currentRepoId}`;
  if (name) params += `&agent_name=${name}`;
  if (status) params += `&status=${status}`;

  const runs = await api(`/agents?${params}`);

  document.getElementById('agents-table').innerHTML = `
    <table>
      <thead><tr>
        <th>ID</th><th>Agent</th><th>Event</th><th>Status</th><th>Duration</th><th>Tools</th><th>Confidence</th><th>Time</th>
      </tr></thead>
      <tbody>
        ${runs.map(r => `
          <tr class="clickable" onclick="toggleAgentDetail(${r.id}, this)">
            <td>#${r.id}</td>
            <td>${r.agent_name}</td>
            <td>${r.event_type || '-'}</td>
            <td>${badge(r.status)}</td>
            <td>${formatDuration(r.duration_ms)}</td>
            <td>${r.tools_count}</td>
            <td>${r.confidence ? (r.confidence * 100).toFixed(0) + '%' : '-'}</td>
            <td>${timeAgo(r.started_at)}</td>
          </tr>
          <tr class="detail-row" id="agent-detail-${r.id}">
            <td colspan="8"><div class="detail-content loading">Loading...</div></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

document.getElementById('agents-name-filter').addEventListener('change', () => { if (currentRepoId) loadAgents(); });
document.getElementById('agents-status-filter').addEventListener('change', () => { if (currentRepoId) loadAgents(); });

async function toggleAgentDetail(runId, row) {
  const detail = document.getElementById(`agent-detail-${runId}`);
  if (detail.classList.contains('open')) {
    detail.classList.remove('open');
    return;
  }
  detail.classList.add('open');

  const run = await api(`/agent/${runId}`);
  const toolsHtml = Array.isArray(run.tools_called) && run.tools_called.length > 0
    ? `<table style="margin-top:8px">
        <thead><tr><th>Tool</th><th>Args</th><th>Success</th><th>Result</th></tr></thead>
        <tbody>${run.tools_called.map(tc => `
          <tr>
            <td><strong>${tc.tool || '-'}</strong></td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${JSON.stringify(tc.args || {}).substring(0, 200)}</td>
            <td>${tc.result?.success ? badge('success') : badge('failed')}</td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${(tc.result?.data || tc.result?.error || '-').toString().substring(0, 200)}</td>
          </tr>
        `).join('')}</tbody>
      </table>`
    : '<p style="color:#8b949e">No tool calls recorded</p>';

  detail.querySelector('.detail-content').innerHTML = `
    ${run.error_message ? `<div class="finding critical"><div class="type">Error</div><div class="desc">${run.error_message}</div></div>` : ''}

    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Tool Calls (${Array.isArray(run.tools_called) ? run.tools_called.length : 0})</div>
    <div class="collapsible-content">${toolsHtml}</div>

    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Full Result</div>
    <div class="collapsible-content">${jsonViewer(run.result, 400)}</div>

    <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')">Context</div>
    <div class="collapsible-content">${jsonViewer(run.context, 300)}</div>
  `;
}

// ── Issues & Milestones Tab ───────────────────────────────────────
async function loadIssues() {
  const data = await api(`/issues?repo_id=${currentRepoId}`);
  const milestones = data.milestones || [];

  document.getElementById('milestones-list').innerHTML = milestones.map(m => {
    const progressColor = m.progress_pct === 100 ? 'fill-green' : m.progress_pct > 0 ? 'fill-yellow' : 'fill-gray';
    return `
      <div class="milestone-card">
        <h3>${m.title}</h3>
        <div class="meta">${m.done_tasks}/${m.total_tasks} tasks done | Confidence: ${(m.confidence * 100).toFixed(0)}%</div>
        <div class="progress-bar"><div class="fill ${progressColor}" style="width:${m.progress_pct}%"></div></div>
        <ul class="task-list">
          ${m.tasks.map(t => `
            <li>
              ${badge(t.status)}
              <span>${t.title}</span>
              ${t.effort ? `<span style="color:#8b949e;font-size:11px">[${t.effort}]</span>` : ''}
            </li>
          `).join('')}
        </ul>
      </div>
    `;
  }).join('') || '<p class="loading">No milestones found</p>';
}

// ── Security Tab ──────────────────────────────────────────────────
async function loadSecurity() {
  const analyses = await api(`/analyses?repo_id=${currentRepoId}&analysis_type=risk`);

  if (analyses.length === 0) {
    document.getElementById('security-list').innerHTML = '<p class="loading">No security analyses found</p>';
    return;
  }

  document.getElementById('security-list').innerHTML = analyses.map(a => {
    const findings = a.result?.findings || {};
    const critical = findings.critical || [];
    const warnings = findings.warning || [];
    const info = findings.info || [];

    return `
      <div class="milestone-card">
        <h3>Risk Analysis ${badge(a.risk_level)} <span style="color:#8b949e;font-size:12px">Score: ${a.score}/100</span></h3>
        <div class="meta">${formatDate(a.created_at)} | ${a.result?.overall_assessment || ''}</div>

        ${critical.length ? `<div class="section-title" style="margin-top:12px;color:#f85149">Critical (${critical.length})</div>` : ''}
        ${critical.map(f => `
          <div class="finding critical">
            <div class="type">${f.type}</div>
            <div class="desc">${f.description}</div>
            ${f.recommendation ? `<div class="rec">${f.recommendation}</div>` : ''}
          </div>
        `).join('')}

        ${warnings.length ? `<div class="section-title" style="margin-top:12px;color:#d29922">Warnings (${warnings.length})</div>` : ''}
        ${warnings.map(f => `
          <div class="finding warning">
            <div class="type">${f.type}</div>
            <div class="desc">${f.description}</div>
            ${f.recommendation ? `<div class="rec">${f.recommendation}</div>` : ''}
          </div>
        `).join('')}

        ${info.length ? `<div class="section-title" style="margin-top:12px;color:#58a6ff">Good Practices (${info.length})</div>` : ''}
        ${info.map(f => `
          <div class="finding info">
            <div class="desc">${f.description || f.type}</div>
          </div>
        `).join('')}
      </div>
    `;
  }).join('');
}

// ── Context Updates Tab ───────────────────────────────────────────
async function loadContext() {
  const runs = await api(`/runs?repo_id=${currentRepoId}&status=context_update`);

  if (runs.length === 0) {
    document.getElementById('context-list').innerHTML = '<p class="loading">No context updates yet. Push code to trigger one.</p>';
    return;
  }

  // Load full details for each
  const details = await Promise.all(runs.slice(0, 20).map(r => api(`/run/${r.id}`)));

  document.getElementById('context-list').innerHTML = details.map(run => {
    const actions = Array.isArray(run.actions_taken) ? run.actions_taken : [];
    const ctxAction = actions.find(a => a.type === 'context_update') || {};
    const changed = ctxAction.files_changed || [];
    const removed = ctxAction.files_removed || [];

    return `
      <div class="milestone-card">
        <h3>Context Update ${badge('context_update')}</h3>
        <div class="meta">${formatDate(run.completed_at)} | Confidence: ${run.confidence ? (run.confidence * 100).toFixed(0) + '%' : '-'}</div>

        ${changed.length ? `
          <div class="section-title" style="margin-top:12px">Files Changed (${changed.length})</div>
          <ul class="task-list">
            ${changed.map(f => `<li><span style="color:#58a6ff">${f}</span></li>`).join('')}
          </ul>
        ` : ''}

        ${removed.length ? `
          <div class="section-title" style="margin-top:12px;color:#f85149">Files Removed (${removed.length})</div>
          <ul class="task-list">
            ${removed.map(f => `<li><span style="color:#f85149;text-decoration:line-through">${f}</span></li>`).join('')}
          </ul>
        ` : ''}

        <div class="collapsible" onclick="this.nextElementSibling.classList.toggle('open')" style="margin-top:12px">View Snapshot</div>
        <div class="collapsible-content">${jsonViewer(run.repo_snapshot, 400)}</div>
      </div>
    `;
  }).join('');
}

// ── Timeline ─────────────────────────────────────────────────────

function eventIcon(eventType) {
  const icons = {
    'push': '&#x1F4E6;',         // package
    'issues': '&#x1F4CB;',       // clipboard
    'pull_request': '&#x1F500;', // merge
    'issue_comment': '&#x1F4AC;',// speech
    'installation': '&#x1F527;', // wrench
    'installation_repositories': '&#x1F527;',
    'check_suite': '&#x2705;',   // checkmark
  };
  return icons[eventType] || '&#x26A1;';
}

function statusDot(status) {
  const colors = {
    'success': '#3fb950',
    'failed': '#f85149',
    'partial': '#d29922',
    'no_action': '#484f58',
  };
  const color = colors[status] || '#484f58';
  return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color};margin-right:6px"></span>`;
}

async function loadTimeline() {
  const data = await api(`/timeline?repo_id=${currentRepoId}&limit=40`);
  const container = document.getElementById('timeline-container');

  if (!data.timeline || data.timeline.length === 0) {
    container.innerHTML = '<p style="color:#8b949e;text-align:center;padding:40px">No events yet. Install GITA on a repo to see activity.</p>';
    return;
  }

  container.innerHTML = data.timeline.map(entry => {
    const agentsHtml = entry.agents.map(agent => {
      const actionsHtml = agent.actions.length > 0
        ? agent.actions.map(a =>
            `<div class="tl-action ${a.success ? '' : 'tl-action-failed'}">${a.success ? '&#x2713;' : '&#x2717;'} ${a.action}</div>`
          ).join('')
        : '<div class="tl-action" style="color:#484f58">No write actions (read-only analysis)</div>';

      return `
        <div class="tl-agent">
          <div class="tl-agent-header">
            ${statusDot(agent.status)}
            <strong>${agent.agent}</strong>
            <span class="tl-meta">${agent.tools_used} tools, ${formatDuration(agent.duration_ms)}</span>
            ${agent.confidence ? `<span class="tl-meta">${Math.round(agent.confidence * 100)}% confidence</span>` : ''}
            ${agent.error ? `<span class="tl-error">${agent.error}</span>` : ''}
          </div>
          <div class="tl-actions">${actionsHtml}</div>
          ${agent.summary ? `<div class="tl-summary">${agent.summary.substring(0, 200)}${agent.summary.length > 200 ? '...' : ''}</div>` : ''}
        </div>
      `;
    }).join('');

    const noAgents = entry.agents_dispatched === 0
      ? '<div class="tl-no-agents">No agents dispatched (event type not in routing table)</div>'
      : '';

    return `
      <div class="tl-entry">
        <div class="tl-header" onclick="this.parentElement.classList.toggle('tl-expanded')">
          <div class="tl-icon">${eventIcon(entry.event_type)}</div>
          <div class="tl-content">
            <div class="tl-title">${entry.description}</div>
            <div class="tl-meta-row">
              <span class="tl-time">${timeAgo(entry.timestamp)}</span>
              <span class="tl-event-type">${entry.event_key}</span>
              ${entry.agents_dispatched > 0
                ? `<span class="tl-agents-count">${statusDot(entry.overall_status)}${entry.agents_dispatched} agent${entry.agents_dispatched > 1 ? 's' : ''}, ${formatDuration(entry.total_duration_ms)}</span>`
                : '<span class="tl-agents-count" style="color:#484f58">no agents</span>'
              }
            </div>
          </div>
        </div>
        <div class="tl-detail">
          ${agentsHtml}
          ${noAgents}
        </div>
      </div>
    `;
  }).join('');
}


// ── Init ──────────────────────────────────────────────────────────
loadRepos();
