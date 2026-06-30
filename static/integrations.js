/* ══════════════════════════════════════════════════════════════════════
   integrations.js — Webhooks + Google Sheets + event log + delivery log
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  let MACHINES = [];
  let WEBHOOKS = [];
  let ALARM_CATALOG = [];            // full list from server
  let MODAL_ALARM_CODES = new Set(); // codes currently selected in the open modal

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatTs(iso) {
    try {
      const d = new Date(iso);
      return d.toLocaleString();
    } catch { return iso; }
  }

  // ── Toasts ────────────────────────────────────────────────────────────
  function toast(msg, type = 'success') {
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = msg;
    $('toast-host').appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // ── Tab switching ─────────────────────────────────────────────────────
  function wireTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        $('tab-' + btn.dataset.tab).classList.add('active');
        if (btn.dataset.tab === 'sheets')     loadSheetsConfig();
        if (btn.dataset.tab === 'events')     loadEvents();
        if (btn.dataset.tab === 'deliveries') loadDeliveries();
      });
    });
  }

  // ── Machines ──────────────────────────────────────────────────────────
  async function loadMachines() {
    const r = await fetch('/api/admin/machines');
    if (!r.ok) { toast('Failed to load machines', 'error'); return; }
    MACHINES = await r.json();
  }

  // ── Alarm catalog ─────────────────────────────────────────────────────
  async function loadAlarmCatalog() {
    try {
      const r = await fetch('/api/admin/alarm-catalog');
      if (!r.ok) { ALARM_CATALOG = []; return; }
      ALARM_CATALOG = await r.json();
    } catch {
      ALARM_CATALOG = [];
    }
  }

  // ── Webhooks ──────────────────────────────────────────────────────────
  async function loadWebhooks() {
    const r = await fetch('/api/admin/webhooks');
    if (!r.ok) { toast('Failed to load webhooks', 'error'); return; }
    WEBHOOKS = await r.json();
    renderWebhooks();
  }

  function renderWebhooks() {
    const host = $('webhook-list');
    if (!WEBHOOKS.length) {
      host.innerHTML = '<div class="empty-state">No webhooks configured. Click "+ Add Webhook" to create one.</div>';
      return;
    }
    const rows = WEBHOOKS.map(w => {
      const machineNames = (w.machine_ids && w.machine_ids.length)
        ? w.machine_ids.map(id => {
            const m = MACHINES.find(x => x.id === id);
            return m ? m.name : id;
          }).join(', ')
        : '<span class="muted">all machines</span>';

      const evs = (w.events || []);
      let eventsCell;
      if (evs.length <= 4) {
        eventsCell = evs.map(e => `<code>${escapeHtml(e)}</code>`).join(' ');
      } else {
        const first = evs.slice(0, 3).map(e => `<code>${escapeHtml(e)}</code>`).join(' ');
        eventsCell = `${first} <span class="muted">+${evs.length - 3} more</span>`;
      }

      const statusBadge = w.enabled
        ? '<span class="badge badge-success">Enabled</span>'
        : '<span class="badge badge-muted">Disabled</span>';
      return `
        <tr>
          <td><strong>${escapeHtml(w.name || 'Unnamed')}</strong><br>
              <span class="muted" style="font-size:12px;">${escapeHtml(w.target_url)}</span></td>
          <td>${eventsCell}</td>
          <td>${machineNames}</td>
          <td>${statusBadge}</td>
          <td class="inline-actions">
            <button class="btn btn-sm btn-secondary" data-action="test" data-id="${w.id}">Test</button>
            <button class="btn btn-sm btn-secondary" data-action="edit" data-id="${w.id}">Edit</button>
            <button class="btn btn-sm btn-danger" data-action="delete" data-id="${w.id}">Delete</button>
          </td>
        </tr>
      `;
    }).join('');
    host.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>Name / URL</th><th>Events</th><th>Machines</th><th>Status</th><th>Actions</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;

    host.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.id;
        if (btn.dataset.action === 'test')   testWebhook(id);
        if (btn.dataset.action === 'edit')   editWebhook(id);
        if (btn.dataset.action === 'delete') deleteWebhook(id);
      });
    });
  }

  // ── Webhook modal ─────────────────────────────────────────────────────
  function openModal(webhook) {
    $('modal-title').textContent = webhook ? 'Edit Webhook' : 'Add Webhook';
    $('sub-id').value   = webhook ? webhook.id : '';
    $('sub-name').value = webhook ? (webhook.name || '') : '';
    $('sub-url').value  = webhook ? (webhook.target_url || '') : '';

    const evs = webhook ? (webhook.events || []) : ['cycle.started', 'cycle.completed'];
    $('ev-started').checked   = evs.includes('cycle.started');
    $('ev-completed').checked = evs.includes('cycle.completed');
    $('ev-alarm-any').checked = evs.includes('alarm.any');

    // Selected specific alarm codes = alarm.<CODE> entries (not alarm.any)
    MODAL_ALARM_CODES = new Set(
      evs.filter(e => e.startsWith('alarm.') && e !== 'alarm.any')
         .map(e => e.substring('alarm.'.length))
    );

    $('sub-enabled').checked = webhook ? !!webhook.enabled : true;

    const checkedIds = webhook ? (webhook.machine_ids || []) : [];
    const mc = $('machine-checkboxes');
    if (!MACHINES.length) {
      mc.innerHTML = '<div class="muted">No machines configured yet.</div>';
    } else {
      mc.innerHTML = MACHINES.map(m => `
        <div class="checkbox-row">
          <input type="checkbox" class="machine-cb" value="${m.id}" id="mcb-${m.id}" ${checkedIds.includes(m.id) ? 'checked' : ''}>
          <label for="mcb-${m.id}">${escapeHtml(m.name)}</label>
        </div>
      `).join('');
    }

    renderAlarmChips();
    renderAlarmSelect();

    $('secret-display').classList.add('hidden');
    $('webhook-modal').classList.add('open');
  }

  function renderAlarmChips() {
    const host = $('alarm-chips');
    if (!host) return;
    if (MODAL_ALARM_CODES.size === 0) {
      host.innerHTML = '<span class="muted" style="font-size:12px;">No specific alarm codes selected.</span>';
      return;
    }
    host.innerHTML = Array.from(MODAL_ALARM_CODES).sort().map(code => {
      const entry = ALARM_CATALOG.find(a => a.code === code);
      const label = entry ? `${escapeHtml(code)} — ${escapeHtml(entry.message)}` : escapeHtml(code);
      return `
        <span style="display:inline-flex;align-items:center;gap:6px;padding:4px 8px;background:var(--surface-3,#333);border-radius:12px;font-family:var(--mono);font-size:12px;">
          <span>${label}</span>
          <button type="button" class="alarm-chip-x" data-code="${escapeHtml(code)}"
                  style="background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:14px;padding:0;line-height:1;">&times;</button>
        </span>`;
    }).join('');
    host.querySelectorAll('.alarm-chip-x').forEach(btn => {
      btn.addEventListener('click', () => {
        MODAL_ALARM_CODES.delete(btn.dataset.code);
        renderAlarmChips();
        renderAlarmSelect();
      });
    });
  }

  function renderAlarmSelect() {
    const sel = $('alarm-select');
    if (!sel) return;
    const available = ALARM_CATALOG.filter(a => !MODAL_ALARM_CODES.has(a.code));
    const opts = ['<option value="">— Select an alarm code to add —</option>'];
    if (!ALARM_CATALOG.length) {
      opts.push('<option value="" disabled>(no alarms detected yet — they appear here after machines report them)</option>');
    } else if (!available.length) {
      opts.push('<option value="" disabled>(all known alarm codes already selected)</option>');
    } else {
      available.forEach(a => {
        const msg = (a.message || '').replace(/^\*+/, '');
        opts.push(`<option value="${escapeHtml(a.code)}">${escapeHtml(a.code)} — ${escapeHtml(msg)}</option>`);
      });
    }
    sel.innerHTML = opts.join('');
  }

  function addAlarmFromSelect() {
    const sel = $('alarm-select');
    const code = sel && sel.value;
    if (!code) return;
    MODAL_ALARM_CODES.add(code);
    renderAlarmChips();
    renderAlarmSelect();
  }

  function closeModal() {
    $('webhook-modal').classList.remove('open');
  }

  async function saveWebhook() {
    const id = $('sub-id').value;
    const events = [];
    if ($('ev-started').checked)    events.push('cycle.started');
    if ($('ev-completed').checked)  events.push('cycle.completed');
    if ($('ev-alarm-any').checked)  events.push('alarm.any');
    MODAL_ALARM_CODES.forEach(code => events.push('alarm.' + code));

    const machine_ids = Array.from(document.querySelectorAll('.machine-cb'))
      .filter(cb => cb.checked).map(cb => cb.value);

    const body = {
      name:        $('sub-name').value.trim(),
      target_url:  $('sub-url').value.trim(),
      events:      events,
      machine_ids: machine_ids,
      enabled:     $('sub-enabled').checked,
    };

    if (!body.target_url)    { toast('Target URL is required', 'error'); return; }
    if (!body.events.length) { toast('Select at least one event type', 'error'); return; }

    const url = id ? `/api/admin/webhooks/${id}` : '/api/admin/webhooks';
    const method = id ? 'PUT' : 'POST';
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) { toast(data.error || 'Save failed', 'error'); return; }

    if (!id && data.secret) {
      $('secret-value').textContent = data.secret;
      $('secret-display').classList.remove('hidden');
      $('modal-title').textContent = 'Webhook Created';
      toast('Webhook created. Copy the secret now.', 'success');
    } else {
      toast('Webhook saved', 'success');
      closeModal();
    }
    await loadWebhooks();
  }

  async function editWebhook(id) {
    const w = WEBHOOKS.find(x => x.id === id);
    if (!w) return;
    await loadAlarmCatalog();
    openModal(w);
  }

  async function deleteWebhook(id) {
    if (!confirm('Delete this webhook subscription? This cannot be undone.')) return;
    const r = await fetch(`/api/admin/webhooks/${id}`, { method: 'DELETE' });
    if (r.ok) { toast('Deleted', 'success'); await loadWebhooks(); }
    else       { toast('Delete failed', 'error'); }
  }

  async function testWebhook(id) {
    toast('Firing test…', 'success');
    const r = await fetch(`/api/admin/webhooks/${id}/test`, { method: 'POST' });
    const data = await r.json();
    if (data.ok) toast(`Test delivered (HTTP ${data.status})`, 'success');
    else         toast(`Test failed (HTTP ${data.status}): ${data.response || ''}`, 'error');
  }

  // ══════════════════════════════════════════════════════════════════════
  // Google Sheets
  // ══════════════════════════════════════════════════════════════════════
  function showSheetsMsg(text, type) {
    const el = $('sheets-msg');
    el.textContent = text;
    el.className = 'msg visible ' + (type || 'info');
  }
  function hideSheetsMsg() {
    const el = $('sheets-msg');
    el.className = 'msg';
    el.textContent = '';
  }

  async function loadSheetsConfig() {
    hideSheetsMsg();
    try {
      const r = await fetch('/api/admin/sheets/config');
      if (!r.ok) { toast('Failed to load Sheets config', 'error'); return; }
      const cfg = await r.json();
      $('sheets-enabled').checked = !!cfg.enabled;
      $('sheets-id').value        = cfg.sheet_id || '';
      $('sheets-tab').value       = cfg.tab_name || 'Sheet1';
      $('sheets-creds').value     = cfg.credentials_path || '';

      const hint = $('sheets-creds-hint');
      if (cfg.credentials_present) {
        hint.innerHTML = '<span style="color:var(--green);">✓ File found on server.</span> Path to the Google service account JSON file.';
      } else {
        hint.innerHTML = '<span style="color:var(--amber);">⚠ File not found at this path.</span> Verify the file exists on the server and is readable by the app.';
      }

      $('sheets-queue-depth').textContent = 'Queue depth: ' + (cfg.queue_depth ?? 0);
    } catch (e) {
      toast('Failed to load Sheets config: ' + e.message, 'error');
    }
  }

  async function saveSheetsConfig() {
    hideSheetsMsg();
    const body = {
      enabled:          $('sheets-enabled').checked,
      sheet_id:         $('sheets-id').value.trim(),
      tab_name:         $('sheets-tab').value.trim() || 'Sheet1',
      credentials_path: $('sheets-creds').value.trim(),
    };
    try {
      const r = await fetch('/api/admin/sheets/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        showSheetsMsg(err.error || 'Save failed', 'err');
        return;
      }
      showSheetsMsg('Configuration saved.', 'ok');
      toast('Sheets config saved', 'success');
      setTimeout(hideSheetsMsg, 3000);
      await loadSheetsConfig();
    } catch (e) {
      showSheetsMsg('Save failed: ' + e.message, 'err');
    }
  }

  async function testSheets() {
    hideSheetsMsg();
    showSheetsMsg('Testing connection…', 'info');
    try {
      const r = await fetch('/api/admin/sheets/test', { method: 'POST' });
      const data = await r.json();
      if (data.ok) {
        const details = [
          '✓ ' + data.message,
          data.spreadsheet_title ? 'Spreadsheet: ' + data.spreadsheet_title : '',
          data.worksheet_title   ? 'Worksheet: '  + data.worksheet_title   : '',
        ].filter(Boolean).join('\n');
        showSheetsMsg(details, 'ok');
      } else {
        showSheetsMsg('✗ ' + data.message, 'err');
      }
    } catch (e) {
      showSheetsMsg('Request failed: ' + e, 'err');
    }
  }

  // ══════════════════════════════════════════════════════════════════════
  // Events tab
  // ══════════════════════════════════════════════════════════════════════
  async function loadEvents() {
    const r = await fetch('/api/admin/events?limit=100');
    if (!r.ok) { $('event-list').innerHTML = '<div class="empty-state">Failed to load</div>'; return; }
    const events = await r.json();
    if (!events.length) {
      $('event-list').innerHTML = '<div class="empty-state">No events detected yet.</div>';
      return;
    }
    const rows = events.map(e => {
      const p = e.payload || {};
      const isAlarm = (e.event_type || '').startsWith('alarm.');
      let detailsCell;
      if (isAlarm) {
        const lvl = (p.level != null) ? ` <span class="badge badge-muted">lvl ${p.level}</span>` : '';
        detailsCell = `<code>${escapeHtml(p.code || '')}</code> ${escapeHtml(p.message || '')}${lvl}`;
      } else {
        const pallet = p.pallet ? `Pallet ${p.pallet}` : '';
        const prog = p.program ? `<code>${escapeHtml(p.program)}</code>` : '';
        detailsCell = [pallet, prog].filter(Boolean).join(' · ');
      }
      return `
        <tr>
          <td>${formatTs(e.ts)}</td>
          <td><code>${escapeHtml(e.event_type)}</code></td>
          <td>${escapeHtml(e.machine_name)}</td>
          <td>${detailsCell}</td>
        </tr>
      `;
    }).join('');
    $('event-list').innerHTML = `
      <table class="data-table">
        <thead><tr><th>When</th><th>Event</th><th>Machine</th><th>Details</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  // ══════════════════════════════════════════════════════════════════════
  // Deliveries tab
  // ══════════════════════════════════════════════════════════════════════
  async function loadDeliveries() {
    const r = await fetch('/api/admin/deliveries?limit=100');
    if (!r.ok) { $('delivery-list').innerHTML = '<div class="empty-state">Failed to load</div>'; return; }
    const rows = await r.json();
    if (!rows.length) {
      $('delivery-list').innerHTML = '<div class="empty-state">No webhook deliveries yet.</div>';
      return;
    }
    const body = rows.map(d => {
      let statusBadge;
      if (d.success)                 statusBadge = `<span class="badge badge-success">HTTP ${d.status_code}</span>`;
      else if (d.status_code === 0)  statusBadge = `<span class="badge badge-danger">No response</span>`;
      else                           statusBadge = `<span class="badge badge-danger">HTTP ${d.status_code}</span>`;

      const subName = (() => {
        const s = WEBHOOKS.find(w => w.id === d.subscription_id);
        return s ? s.name : d.subscription_id;
      })();

      const eventLabel = d.event_id === 0 ? '(test fire)' : (d.event_type || '—');

      return `
        <tr>
          <td>${formatTs(d.ts)}</td>
          <td>${escapeHtml(subName)}</td>
          <td><code>${escapeHtml(eventLabel)}</code></td>
          <td>${escapeHtml(d.machine_name || '')}</td>
          <td>#${d.attempt}</td>
          <td>${statusBadge}</td>
          <td><span class="muted" title="${escapeHtml(d.response || '')}">${escapeHtml((d.response || '').slice(0, 80))}</span></td>
        </tr>
      `;
    }).join('');
    $('delivery-list').innerHTML = `
      <table class="data-table">
        <thead><tr><th>When</th><th>Subscription</th><th>Event</th><th>Machine</th><th>Attempt</th><th>Status</th><th>Response</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    `;
  }

  // ══════════════════════════════════════════════════════════════════════
  // Boot
  // ══════════════════════════════════════════════════════════════════════
  async function init() {
    wireTabs();
    $('add-webhook-btn').addEventListener('click', async () => {
      await loadAlarmCatalog();
      openModal(null);
    });
    $('modal-cancel').addEventListener('click', closeModal);
    $('modal-save').addEventListener('click', saveWebhook);
    $('alarm-add-btn').addEventListener('click', addAlarmFromSelect);
    $('webhook-modal').addEventListener('click', (e) => {
      if (e.target === $('webhook-modal')) closeModal();
    });

    $('sheets-save-btn').addEventListener('click', saveSheetsConfig);
    $('sheets-test-btn').addEventListener('click', testSheets);

    await loadMachines();
    await loadAlarmCatalog();
    await loadWebhooks();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
