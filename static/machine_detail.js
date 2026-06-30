/* ══════════════════════════════════════════════════════════════════════
   machine_detail.js — 24h stats + timeline + latest poll data
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const machineId = window.location.pathname.split('/').pop();
  let machine = null;

  function formatTimeAgo(isoString) {
    if (!isoString) return '—';
    try {
      const date = new Date(isoString);
      const now  = new Date();
      const diffMins = Math.floor((now - date) / 60000);
      if (diffMins < 1)  return 'Just now';
      if (diffMins < 60) return `${diffMins}m ago`;
      const diffHours = Math.floor(diffMins / 60);
      if (diffHours < 24) return `${diffHours}h ago`;
      const diffDays = Math.floor(diffHours / 24);
      return `${diffDays}d ago`;
    } catch { return '—'; }
  }

  function setStatus(status, label) {
    const dot = document.getElementById('statusDot');
    const txt = document.getElementById('statusLabel');
    dot.className = `status-dot ${status}`;
    txt.textContent = label;
  }

  function show(id) { document.getElementById(id).classList.remove('hidden'); }
  function hide(id) { document.getElementById(id).classList.add('hidden'); }

  function showError(elementId, message = 'Failed to load data') {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = `<div style="color:var(--red);font-size:11px;text-align:center;padding:10px;">${message}</div>`;
  }

  // ── Load machine ──────────────────────────────────────────────────────
  async function loadMachine() {
    try {
      const res = await fetch('/api/machines');
      if (res.ok) {
        const machines = await res.json();
        machine = machines.find(m => m.id === machineId);
        if (machine) {
          document.title = machine.name + ' — CNC Shop Monitor';
          // Update header title via nav API if present
          const titleEl = document.querySelector('.app-title-main');
          if (titleEl) titleEl.textContent = machine.name;
          document.getElementById('liveViewLink').href = `/machine/${machineId}/live`;
        } else {
          setStatus('no_data', 'Not Found');
          return;
        }
      }
    } catch (e) {
      console.error('Failed to load machine:', e);
    }
  }

  // ── Stats ─────────────────────────────────────────────────────────────
  async function loadStats() {
    try {
      const res = await fetch(`/api/machine/${machineId}/stats?hours=24`);
      if (!res.ok) throw new Error('stats failed');
      const stats = await res.json();
      hide('statsLoading');
      show('statsGrid');
      document.getElementById('statsGrid').style.display = 'grid';

      document.getElementById('uptime').textContent     = stats.uptime_percent + '%';
      document.getElementById('pollCount').textContent  = (stats.polls_count || 0).toLocaleString();
      document.getElementById('errorCount').textContent = (stats.errors_count || 0).toLocaleString();
      document.getElementById('efficiency').textContent = stats.avg_efficiency ? stats.avg_efficiency + '%' : '—';

      hide('programsLoading');
      const list = document.getElementById('programsList');
      list.classList.remove('hidden');
      if (stats.programs && stats.programs.length > 0) {
        list.innerHTML = '<div class="programs-list">' +
          stats.programs.map(p => `<div class="program-tag">${p}</div>`).join('') +
          '</div>';
      } else {
        list.innerHTML = '<div class="muted" style="font-size:11px;text-align:center;padding:10px;">No programs detected</div>';
      }
    } catch (e) {
      console.error('Failed to load stats:', e);
      showError('statsLoading', 'Failed to load statistics');
      showError('programsLoading', 'Failed to load programs');
    }
  }

  // ── Timeline ──────────────────────────────────────────────────────────
  async function loadTimeline() {
    try {
      const res = await fetch(`/api/machine/${machineId}/timeline?hours=24`);
      if (!res.ok) throw new Error('timeline failed');
      const data = await res.json();
      hide('timelineLoading');
      const tl = document.getElementById('timeline');
      tl.classList.remove('hidden');
      tl.style.display = 'flex';

      if (data.timeline && data.timeline.length > 0) {
        tl.innerHTML = data.timeline.map(event => `
          <div class="timeline-item">
            <div class="timeline-dot ${event.type}"></div>
            <div class="timeline-content">
              <div class="timeline-time">${formatTimeAgo(event.ts)}</div>
              <div class="timeline-message">${event.message}</div>
            </div>
          </div>
        `).join('');
      } else {
        tl.innerHTML = '<div class="muted" style="font-size:11px;text-align:center;padding:20px;">No recent activity</div>';
      }
    } catch (e) {
      console.error('Failed to load timeline:', e);
      showError('timelineLoading', 'Failed to load activity');
    }
  }

  // ── Current data ──────────────────────────────────────────────────────
  async function loadCurrentData() {
    try {
      const res = await fetch(`/api/latest/${machineId}`);
      if (res.status === 404) {
        hide('currentLoading');
        document.getElementById('currentError').classList.remove('hidden');
        document.getElementById('currentError').style.display = 'flex';
        setStatus('no_data', 'No Data');
        document.getElementById('lastUpdate').textContent = 'No data available';
        return;
      }
      if (!res.ok) throw new Error('current data failed');
      const poll = await res.json();
      hide('currentLoading');
      const grid = document.getElementById('currentData');
      grid.classList.remove('hidden');
      grid.style.display = 'grid';

      setStatus('running', 'Online');
      document.getElementById('lastUpdate').textContent = 'Last update: ' + formatTimeAgo(poll.ts);

      const modelData  = poll.data['machine/model'];
      const statusData = poll.data['machine/status'];
      if (modelData || statusData) {
        const badge = document.getElementById('modelBadge');
        badge.textContent = [modelData?.value, statusData?.value].filter(Boolean).join(' — ');
        badge.classList.remove('hidden');
      }

      const groups = {
        'Machine Info': {},
        'Running Log': {},
        'Work Counter': {},
        'Alarms': {},
        'Tools': {},
        'Other': {},
      };

      for (const [key, value] of Object.entries(poll.data)) {
        if (key.startsWith('machine/'))            groups['Machine Info'][key.replace('machine/', '')] = value.value;
        else if (key.startsWith('running_log/'))   groups['Running Log'][key.replace('running_log/', '')] = value.value;
        else if (key.startsWith('work_counter/'))  groups['Work Counter'][key.replace('work_counter/', '')] = value.value;
        else if (key.startsWith('alarm_log/'))     groups['Alarms'][key.replace('alarm_log/', '')] = value.value;
        else if (key.startsWith('tool/'))          groups['Tools'][key.replace('tool/', '')] = value.value;
        else                                       groups['Other'][key] = value.value;
      }

      grid.innerHTML = '';
      for (const [, items] of Object.entries(groups)) {
        if (Object.keys(items).length === 0) continue;
        for (const [label, value] of Object.entries(items)) {
          const card = document.createElement('div');
          card.className = 'detail-data-card';
          card.innerHTML = `
            <div class="data-label">${label}</div>
            <div class="data-value">${value || '—'}</div>
          `;
          grid.appendChild(card);
        }
      }
    } catch (e) {
      console.error('Failed to load current data:', e);
      hide('currentLoading');
      document.getElementById('currentError').classList.remove('hidden');
      document.getElementById('currentError').style.display = 'flex';
      setStatus('offline', 'Error');
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────
  async function init() {
    await loadMachine();
    await Promise.all([loadStats(), loadTimeline(), loadCurrentData()]);
  }

  setInterval(() => { loadStats(); loadTimeline(); loadCurrentData(); }, 30000);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
