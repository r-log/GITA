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
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
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

// ── Tab Navigation ────────────────────────────────────────────────
document.querySelectorAll('nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
    loadTabData(btn.dataset.tab);
  });
});

// ── Repo Selector ─────────────────────────────────────────────────
const repoSelector = document.getElementById('repo-selector');
repoSelector.addEventListener('change', () => {
  currentRepoId = repoSelector.value ? parseInt(repoSelector.value) : null;
  if (currentRepoId) {
    const activeTab = document.querySelector('nav button.active').dataset.tab;
    loadTabData(activeTab);
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
  if (repos.length === 1) {
    repoSelector.value = repos[0].id;
    currentRepoId = repos[0].id;
    loadTabData('overview');
  }
}

// ── Tab Data Loading ──────────────────────────────────────────────
function loadTabData(tab) {
  if (!currentRepoId) return;
  switch (tab) {
    case 'overview': loadOverview(); break;
    case 'runs': loadRuns(); break;
    case 'agents': loadAgents(); break;
    case 'issues': loadIssues(); break;
    case 'security': loadSecurity(); break;
    case 'context': loadContext(); break;
  }
}

// ── Overview Tab ──────────────────────────────────────────────────
async function loadOverview() {
  const [stats, activity, agents] = await Promise.all([
    api(`/stats?repo_id=${currentRepoId}`),
    api(`/activity?repo_id=${currentRepoId}&days=30`),
    api(`/agents?repo_id=${currentRepoId}&limit=20`),
  ]);

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
      <div class="label">Last Context Update</div>
      <div class="value" style="font-size:16px">${timeAgo(stats.last_context_update)}</div>
    </div>
  `;

  // Activity chart
  renderActivityChart(activity);

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

function renderActivityChart(data) {
  const ctx = document.getElementById('activity-chart').getContext('2d');
  if (activityChart) activityChart.destroy();
  activityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.date),
      datasets: [
        { label: 'Total', data: data.map(d => d.total), borderColor: '#58a6ff', fill: false, tension: 0.3 },
        { label: 'Success', data: data.map(d => d.success), borderColor: '#2dc653', fill: false, tension: 0.3 },
        { label: 'Failed', data: data.map(d => d.failed), borderColor: '#f85149', fill: false, tension: 0.3 },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: '#8b949e' } } },
      scales: {
        x: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' }, beginAtZero: true },
      },
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

// ── Init ──────────────────────────────────────────────────────────
loadRepos();
