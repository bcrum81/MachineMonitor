/* ══════════════════════════════════════════════════════════════════════
   reports.js — Admin cycle time reports page logic
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  let machines = [];

  // ── Helpers ───────────────────────────────────────────────────────────
  function fmtDuration(sec) {
    if (sec == null || isNaN(sec)) return '—';
    sec = Number(sec);
    if (sec < 60) return sec.toFixed(1) + 's';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.round(sec % 60);
    if (h > 0) return `${h}h ${m}m ${s}s`;
    return `${m}m ${s}s`;
  }

  function fmtLocal(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    } catch (_) { return iso; }
  }

  function todayISODate() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function isoDateDaysAgo(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function showError(msg) {
    const box = document.getElementById('error-box');
    box.textContent = msg;
    box.classList.add('visible');
  }
  function clearError() {
    document.getElementById('error-box').classList.remove('visible');
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  // ── Init ──────────────────────────────────────────────────────────────
  async function loadMachines() {
    try {
      const r = await fetch('/api/admin/machines');
      if (!r.ok) throw new Error('Not authorized');
      machines = await r.json();
      const sel = document.getElementById('machine-filter');
      machines.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.name;
        sel.appendChild(opt);
      });
    } catch (e) {
      showError('Could not load machine list. Are you logged in as admin?');
    }
  }

  function setDateDefaults() {
    document.getElementById('start-date').value = isoDateDaysAgo(7);
    document.getElementById('end-date').value   = todayISODate();
  }

  // ── Presets ───────────────────────────────────────────────────────────
  function bindPresets() {
    document.querySelectorAll('.preset-btns button').forEach(btn => {
      btn.addEventListener('click', () => {
        const p = btn.dataset.preset;
        const start = document.getElementById('start-date');
        const end   = document.getElementById('end-date');
        if (p === 'today') {
          start.value = todayISODate(); end.value = todayISODate();
        } else if (p === 'yesterday') {
          start.value = isoDateDaysAgo(1); end.value = isoDateDaysAgo(1);
        } else if (p === '7d') {
          start.value = isoDateDaysAgo(7); end.value = todayISODate();
        } else if (p === '30d') {
          start.value = isoDateDaysAgo(30); end.value = todayISODate();
        } else if (p === 'thismonth') {
          const d = new Date();
          start.value = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-01`;
          end.value = todayISODate();
        }
        runReport();
      });
    });
  }

  // ── Query builder ─────────────────────────────────────────────────────
  function buildQuery() {
    const start   = document.getElementById('start-date').value;
    const end     = document.getElementById('end-date').value;
    const machine = document.getElementById('machine-filter').value;
    const pallet  = document.getElementById('pallet-filter').value;
    const program = document.getElementById('program-filter').value.trim();

    const params = new URLSearchParams();
    if (start) params.set('start', start);
    if (end)   params.set('end',   end);
    if (machine && machine !== 'all') params.set('machine_id', machine);
    if (pallet  && pallet  !== 'all') params.set('pallet', pallet);
    if (program) params.set('program', program);
    return params.toString();
  }

  // ── Run report ────────────────────────────────────────────────────────
  async function runReport() {
    clearError();
    const qs = buildQuery();
    document.getElementById('results-body').innerHTML =
      '<div class="loading-inline"><div class="spinner"></div>Loading…</div>';

    try {
      const r = await fetch('/api/admin/cycle-report?' + qs);
      if (r.status === 401) { showError('Unauthorized — session expired?'); return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      renderReport(data);
    } catch (e) {
      showError('Report failed: ' + e.message);
      document.getElementById('results-body').innerHTML =
        '<div class="empty-state">No data.</div>';
    }
  }

  function renderReport(data) {
    // Summary
    const s = data.summary;
    document.getElementById('summary-row').style.display = 'grid';
    document.getElementById('stat-count').textContent = s.count.toLocaleString();
    document.getElementById('stat-avg').textContent   = fmtDuration(s.avg_duration_sec);
    document.getElementById('stat-min').textContent   = fmtDuration(s.min_duration_sec);
    document.getElementById('stat-max').textContent   = fmtDuration(s.max_duration_sec);
    document.getElementById('stat-total').textContent = fmtDuration(s.total_duration_sec);

    // Per-machine chips
    const pmRow = document.getElementById('per-machine-row');
    const pmChips = document.getElementById('per-machine-chips');
    pmChips.innerHTML = '';
    if (data.per_machine && data.per_machine.length > 0) {
      pmRow.style.display = 'block';
      data.per_machine.forEach(pm => {
        const el = document.createElement('span');
        el.className = 'chip';
        el.innerHTML = `<strong>${escapeHtml(pm.machine_name)}</strong> — ${pm.count} cycles, avg ${fmtDuration(pm.avg_sec)}`;
        pmChips.appendChild(el);
      });
    } else {
      pmRow.style.display = 'none';
    }

    // Table
    const body = document.getElementById('results-body');
    if (!data.rows || data.rows.length === 0) {
      body.innerHTML = '<div class="empty-state">No completed cycles found in this range.</div>';
      return;
    }

    let html = `
      <div style="max-height: 600px; overflow-y: auto;">
      <table class="data-table">
        <thead>
          <tr>
            <th>Machine</th>
            <th>Pallet</th>
            <th>Program</th>
            <th>Cycle Start</th>
            <th>Cycle End</th>
            <th class="num">Duration</th>
          </tr>
        </thead>
        <tbody>
    `;
    for (const r of data.rows) {
      html += `
        <tr>
          <td>${escapeHtml(r.machine_name)}</td>
          <td class="mono">P${r.pallet}</td>
          <td class="mono">${escapeHtml(r.program || '—')}</td>
          <td class="mono">${escapeHtml(fmtLocal(r.cycle_start))}</td>
          <td class="mono">${escapeHtml(fmtLocal(r.cycle_end))}</td>
          <td class="num">${fmtDuration(r.duration_sec)}</td>
        </tr>
      `;
    }
    html += '</tbody></table></div>';
    body.innerHTML = html;
  }

  // ── CSV download ──────────────────────────────────────────────────────
  function bindCsvButton() {
    document.getElementById('csv-btn').addEventListener('click', () => {
      clearError();
      const qs = buildQuery();
      window.location.href = '/api/admin/cycle-report.csv?' + qs;
    });
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  async function init() {
    setDateDefaults();
    bindPresets();
    bindCsvButton();

    document.getElementById('run-btn').addEventListener('click', runReport);
    document.getElementById('program-filter').addEventListener('keydown', e => {
      if (e.key === 'Enter') runReport();
    });

    await loadMachines();
    runReport();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
