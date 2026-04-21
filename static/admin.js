/* ══════════════════════════════════════════════════════════════════════
   admin.js — Admin panel: machine CRUD + change password
   Protocol-aware: pulls available protocols from /api/protocols and
   renders protocol-specific config fields dynamically.
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  let machines  = [];
  let PROTOCOLS = [];     // metadata from /api/protocols, keyed by array; also indexed below
  let PROTO_BY_ID = {};
  let editMode  = false;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ══════════════════════════════════════════════════════════════════════
  // Protocol registry — fetched once, used everywhere
  // ══════════════════════════════════════════════════════════════════════
  async function loadProtocols() {
    try {
      const res = await fetch('/api/protocols');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      PROTOCOLS = await res.json();
    } catch (e) {
      console.error('Failed to load protocols:', e);
      PROTOCOLS = [];
    }
    PROTO_BY_ID = {};
    PROTOCOLS.forEach(p => { PROTO_BY_ID[p.id] = p; });
    renderProtocolDropdown();
  }

  function renderProtocolDropdown() {
    const sel = document.getElementById('mProtocol');
    if (!PROTOCOLS.length) {
      sel.innerHTML = '<option value="">(no protocols registered)</option>';
      return;
    }
    sel.innerHTML = PROTOCOLS.map(p =>
      `<option value="${esc(p.id)}">${esc(p.display_name)}</option>`
    ).join('');
  }

  // ══════════════════════════════════════════════════════════════════════
  // Machines table
  // ══════════════════════════════════════════════════════════════════════
  async function loadMachines() {
    const res = await fetch('/api/admin/machines');
    if (res.ok) {
      machines = await res.json();
      renderTable();
    }
  }

  function palletLabel(m) {
    const proto = PROTO_BY_ID[m.protocol];
    const defaultPc = proto ? proto.default_pallet_count : (m.protocol === 'http_brother' ? 2 : 0);
    const pc = (m.pallet_count !== undefined && m.pallet_count !== null) ? m.pallet_count : defaultPc;
    if (pc === 0) return '—';
    if (pc === 1) return '1';
    return String(pc);
  }

  function protocolBadge(protocolId) {
    const proto = PROTO_BY_ID[protocolId];
    const cls = protocolId === 'http_brother' ? 'http' : '';
    const stub = (proto && !proto.implemented) ? ' <span class="muted" style="font-size:10px;">(stub)</span>' : '';
    return `<span class="proto-badge ${cls}">${esc(protocolId)}</span>${stub}`;
  }

  function renderTable() {
    const tbody = document.getElementById('machineTableBody');
    if (machines.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No machines configured yet. Click + Add Machine to get started.</td></tr>';
      return;
    }
    tbody.innerHTML = machines.map(m => `
      <tr>
        <td style="font-weight:500;">${esc(m.name)}</td>
        <td>${protocolBadge(m.protocol)}</td>
        <td class="mono">${esc(m.ip)}</td>
        <td class="mono muted">${m.port || 'auto'}</td>
        <td class="mono muted">${palletLabel(m)}</td>
        <td class="mono muted">${m.poll_interval}s</td>
        <td class="mono muted">${(m.utc_offset !== undefined && m.utc_offset !== null) ? (m.utc_offset >= 0 ? 'UTC+' : 'UTC') + m.utc_offset : 'UTC+0'}</td>
        <td>
          <div class="actions">
            <a href="/machine/${esc(m.id)}" class="btn btn-sm btn-success" target="_blank">View</a>
            <button class="btn btn-sm btn-secondary" data-action="edit" data-id="${esc(m.id)}">Edit</button>
            <button class="btn btn-sm btn-danger" data-action="delete" data-id="${esc(m.id)}" data-name="${esc(m.name)}">Delete</button>
          </div>
        </td>
      </tr>
    `).join('');

    tbody.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const id   = btn.dataset.id;
        const name = btn.dataset.name;
        if (btn.dataset.action === 'edit')   editMachine(id);
        if (btn.dataset.action === 'delete') deleteMachine(id, name);
      });
    });
  }

  // ══════════════════════════════════════════════════════════════════════
  // Protocol-specific field rendering
  // ══════════════════════════════════════════════════════════════════════
  function onProtocolChange() {
    const proto = PROTO_BY_ID[document.getElementById('mProtocol').value];
    if (!proto) return;

    // Hint / warning under the protocol dropdown
    const hint = document.getElementById('mProtocolHint');
    if (!proto.implemented) {
      hint.textContent = '⚠ This protocol is a scaffold stub. It can be saved but will not poll data yet.';
      hint.style.color = '#b45309';
    } else {
      hint.textContent = '';
      hint.style.color = '';
    }

    // Toggle pallet count visibility
    document.getElementById('palletCountField').style.display =
      proto.supports_pallets ? '' : 'none';
    if (!proto.supports_pallets) {
      document.getElementById('mPalletCount').value = String(proto.default_pallet_count || 0);
    }

    // Toggle auth row visibility
    document.getElementById('authFieldsRow').style.display =
      proto.requires_auth ? '' : 'none';
    if (!proto.requires_auth) {
      document.getElementById('mUsername').value = '';
      document.getElementById('mPassword').value = '';
    }

    // Update port placeholder
    const portInput = document.getElementById('mPort');
    if (!portInput.value) {
      portInput.placeholder = proto.default_port != null ? String(proto.default_port) : 'auto';
    }

    // Render CONFIG_FIELDS
    renderProtocolConfigFields(proto, {});
  }

  function renderProtocolConfigFields(proto, values) {
    const container = document.getElementById('protocolConfigFields');
    if (!proto || !proto.config_fields || !proto.config_fields.length) {
      container.innerHTML = '';
      return;
    }

    let html = `<div class="panel-label" style="margin-top:10px;">${esc(proto.display_name)} — Configuration</div>`;
    html += '<div style="display:flex;flex-direction:column;gap:9px;">';

    for (const f of proto.config_fields) {
      const defaultVal = (f.default !== undefined && f.default !== null) ? f.default : '';
      const val = (values && values[f.name] !== undefined && values[f.name] !== null) ? values[f.name] : defaultVal;
      const type = f.type === 'password' ? 'password' : (f.type === 'number' ? 'number' : 'text');
      const placeholder = f.placeholder || '';
      const hint = f.hint || '';
      const inputId = 'mPCF_' + f.name;

      html += `
        <div class="field">
          <label>${esc(f.label || f.name)}</label>
          <input type="${type}" id="${esc(inputId)}" data-pcf-name="${esc(f.name)}"
                 placeholder="${esc(placeholder)}" value="${esc(val)}" />
          ${hint ? `<span class="field-hint">${esc(hint)}</span>` : ''}
        </div>
      `;
    }
    html += '</div>';
    container.innerHTML = html;
  }

  function collectProtocolConfigFields() {
    const out = {};
    document.querySelectorAll('#protocolConfigFields input[data-pcf-name]').forEach(inp => {
      const name = inp.dataset.pcfName;
      let val = inp.value;
      if (inp.type === 'number') {
        if (val === '' || val === null) {
          out[name] = null;
        } else {
          const n = Number(val);
          out[name] = Number.isFinite(n) ? n : null;
        }
      } else {
        out[name] = val === '' ? null : val;
      }
    });
    return out;
  }

  // ══════════════════════════════════════════════════════════════════════
  // Modal
  // ══════════════════════════════════════════════════════════════════════
  function openModal() {
    editMode = false;
    document.getElementById('modalTitle').textContent = 'Add Machine';
    document.getElementById('editId').value = '';
    document.getElementById('mName').value = '';
    // Default to the first implemented protocol, else first registered
    const defaultProto = PROTOCOLS.find(p => p.implemented) || PROTOCOLS[0];
    document.getElementById('mProtocol').value = defaultProto ? defaultProto.id : '';
    document.getElementById('mIp').value = '';
    document.getElementById('mPort').value = '';
    document.getElementById('mUsername').value = '';
    document.getElementById('mPassword').value = '';
    document.getElementById('mPoll').value = '2';
    document.getElementById('mUtcOffset').value = '0';
    document.getElementById('mPalletCount').value =
      defaultProto ? String(defaultProto.default_pallet_count) : '2';
    hideMsg('testResult');
    onProtocolChange();   // render initial CONFIG_FIELDS + toggles
    document.getElementById('modalOverlay').classList.add('open');
    document.getElementById('mName').focus();
  }

  function editMachine(id) {
    const m = machines.find(x => x.id === id);
    if (!m) return;
    editMode = true;
    document.getElementById('modalTitle').textContent = 'Edit Machine';
    document.getElementById('editId').value = m.id;
    document.getElementById('mName').value = m.name;
    document.getElementById('mProtocol').value = m.protocol;
    document.getElementById('mIp').value = m.ip;
    document.getElementById('mPort').value = m.port || '';
    document.getElementById('mUsername').value = m.username || '';
    document.getElementById('mPassword').value = m.password || '';
    document.getElementById('mPoll').value = m.poll_interval;
    document.getElementById('mUtcOffset').value = (m.utc_offset !== undefined && m.utc_offset !== null) ? m.utc_offset : 0;

    const proto = PROTO_BY_ID[m.protocol];
    const defaultPc = proto ? proto.default_pallet_count : (m.protocol === 'http_brother' ? 2 : 0);
    const pc = (m.pallet_count !== undefined && m.pallet_count !== null) ? m.pallet_count : defaultPc;
    document.getElementById('mPalletCount').value = String(pc);

    hideMsg('testResult');

    // Render the current protocol's CONFIG_FIELDS with existing values from the machine
    if (proto) {
      // Hint / visibility toggles
      const hint = document.getElementById('mProtocolHint');
      if (!proto.implemented) {
        hint.textContent = '⚠ This protocol is a scaffold stub. It can be saved but will not poll data yet.';
        hint.style.color = '#b45309';
      } else {
        hint.textContent = '';
        hint.style.color = '';
      }
      document.getElementById('palletCountField').style.display = proto.supports_pallets ? '' : 'none';
      document.getElementById('authFieldsRow').style.display    = proto.requires_auth ? '' : 'none';
      renderProtocolConfigFields(proto, m);
    } else {
      onProtocolChange();
    }

    document.getElementById('modalOverlay').classList.add('open');
  }

  function closeModal() {
    document.getElementById('modalOverlay').classList.remove('open');
  }

  function getFormData() {
    const base = {
      name:          document.getElementById('mName').value.trim(),
      protocol:      document.getElementById('mProtocol').value,
      ip:            document.getElementById('mIp').value.trim(),
      port:          parseInt(document.getElementById('mPort').value) || null,
      username:      document.getElementById('mUsername').value.trim() || null,
      password:      document.getElementById('mPassword').value || null,
      poll_interval: parseFloat(document.getElementById('mPoll').value) || 2.0,
      utc_offset:    parseFloat(document.getElementById('mUtcOffset').value) || 0,
      pallet_count:  parseInt(document.getElementById('mPalletCount').value) || 0,
    };
    return Object.assign(base, collectProtocolConfigFields());
  }

  // ══════════════════════════════════════════════════════════════════════
  // Messages
  // ══════════════════════════════════════════════════════════════════════
  function showMsg(id, text, type) {
    const el = document.getElementById(id);
    el.textContent = text;
    el.className = 'msg visible ' + (type || 'info');
  }
  function hideMsg(id) {
    const el = document.getElementById(id);
    el.className = 'msg';
    el.textContent = '';
  }

  // ══════════════════════════════════════════════════════════════════════
  // Test connection
  // ══════════════════════════════════════════════════════════════════════
  async function testConnection() {
    const data = getFormData();
    if (!data.ip) { showMsg('testResult', 'Enter an IP address first.', 'err'); return; }
    showMsg('testResult', 'Testing connection…', 'info');
    try {
      const res = await fetch('/ping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const r = await res.json();
      if (r.reachable) {
        let msg = '✓ Reachable — ' + r.detail;
        if (r.machine_info?.model)  msg += '\n  Model: ' + r.machine_info.model;
        if (r.machine_info?.status) msg += '\n  Status: ' + r.machine_info.status;
        if (r.pages) {
          const ok = Object.entries(r.pages).filter(([, v]) => v).map(([k]) => k);
          if (ok.length) msg += '\n  Pages: ' + ok.join(', ');
        }
        showMsg('testResult', msg, 'ok');
      } else {
        showMsg('testResult', '✗ ' + r.detail, 'err');
      }
    } catch (e) {
      showMsg('testResult', 'Request failed: ' + e, 'err');
    }
  }

  // ══════════════════════════════════════════════════════════════════════
  // Save / delete
  // ══════════════════════════════════════════════════════════════════════
  async function saveMachine() {
    const data = getFormData();
    if (!data.name)     { showMsg('testResult', 'Machine name is required.', 'err'); return; }
    if (!data.ip)       { showMsg('testResult', 'IP address is required.', 'err'); return; }
    if (!data.protocol) { showMsg('testResult', 'Protocol is required.', 'err'); return; }

    const id     = document.getElementById('editId').value;
    const url    = id ? `/api/admin/machines/${id}` : '/api/admin/machines';
    const method = id ? 'PUT' : 'POST';

    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      closeModal();
      await loadMachines();
    } else {
      let errText = 'Unknown error';
      try {
        const err = await res.json();
        errText = err.error || JSON.stringify(err);
      } catch (e) { /* ignore */ }
      showMsg('testResult', 'Save failed: ' + errText, 'err');
    }
  }

  async function deleteMachine(id, name) {
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    const res = await fetch(`/api/admin/machines/${id}`, { method: 'DELETE' });
    if (res.ok) await loadMachines();
  }

  // ══════════════════════════════════════════════════════════════════════
  // Password change
  // ══════════════════════════════════════════════════════════════════════
  async function changePassword() {
    const current = document.getElementById('pwCurrent').value;
    const newPw   = document.getElementById('pwNew').value;
    const confirm = document.getElementById('pwConfirm').value;

    if (newPw !== confirm) {
      showMsg('pwMsg', 'New passwords do not match.', 'err');
      return;
    }
    const res = await fetch('/api/admin/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current, new_password: newPw }),
    });
    const data = await res.json();
    if (res.ok) {
      showMsg('pwMsg', 'Password updated successfully.', 'ok');
      document.getElementById('pwCurrent').value = '';
      document.getElementById('pwNew').value = '';
      document.getElementById('pwConfirm').value = '';
    } else {
      showMsg('pwMsg', data.error || 'Failed to update password.', 'err');
    }
    setTimeout(() => hideMsg('pwMsg'), 4000);
  }

  // ══════════════════════════════════════════════════════════════════════
  // Wire up
  // ══════════════════════════════════════════════════════════════════════
  async function init() {
    await loadProtocols();      // must be first — drives dropdown + table badges
    await loadMachines();

    document.getElementById('btn-add-machine').addEventListener('click', () => openModal());
    document.getElementById('btn-cancel').addEventListener('click', closeModal);
    document.getElementById('btn-test').addEventListener('click', testConnection);
    document.getElementById('btn-save').addEventListener('click', saveMachine);
    document.getElementById('btn-change-pw').addEventListener('click', changePassword);
    document.getElementById('mProtocol').addEventListener('change', onProtocolChange);

    document.getElementById('modalOverlay').addEventListener('click', function (e) {
      if (e.target === this) closeModal();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
