/* ══════════════════════════════════════════════════════════════════════
   tester.js — Admin machine tester: ping + manual-config WebSocket stream
   Pulls protocol list from /api/protocols so FOCAS/OPC UA show up
   automatically as they are registered.
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  let ws          = null;
  let pollCount   = 0;
  let nodeData    = {};
  let PROTOCOLS   = [];
  let PROTO_BY_ID = {};

  const PAGE_LABELS = {
    running_log:    'Running Log — Cycle Times & Program',
    work_counter:   'Work Counter — Part Counts',
    alarm_log:      'Alarms',
    tool:           'ATC Tool Data',
    status_log:     'Status History',
    mainte_info:    'Maintenance Info',
    measure_result: 'Measurement Results',
    header:         'Machine Info',
  };

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  // ── Load protocols on page init ────────────────────────────────────────
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

    const sel = document.getElementById('protocol');
    if (!PROTOCOLS.length) {
      sel.innerHTML = '<option value="">(no protocols registered)</option>';
      return;
    }
    sel.innerHTML = PROTOCOLS.map(p =>
      `<option value="${esc(p.id)}">${esc(p.display_name)}</option>`
    ).join('');

    // Default to the first implemented protocol, else the first in the list
    const defaultProto = PROTOCOLS.find(p => p.implemented) || PROTOCOLS[0];
    if (defaultProto) sel.value = defaultProto.id;

    onProtoChange();
  }

  function onProtoChange() {
    const p = PROTO_BY_ID[document.getElementById('protocol').value];
    const portEl = document.getElementById('port');
    const hint   = document.getElementById('protocolHint');
    const auth   = document.getElementById('authFields');

    if (!p) {
      portEl.placeholder = 'auto';
      if (hint) { hint.textContent = ''; hint.style.color = ''; }
      return;
    }
    portEl.placeholder = p.default_port != null ? String(p.default_port) : 'auto';

    if (hint) {
      if (!p.implemented) {
        hint.textContent = '⚠ Scaffold stub. Ping may do a TCP check only; live stream is not available.';
        hint.style.color = '#b45309';
      } else {
        hint.textContent = '';
        hint.style.color = '';
      }
    }

    if (auth) auth.style.display = p.requires_auth ? '' : 'none';
  }

  function log(msg, cls = '') {
    const el = document.getElementById('statusLog');
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false });
    el.innerHTML += `<div class="entry ${cls}"><span class="ts">${ts}</span>${esc(msg)}</div>`;
    el.scrollTop = el.scrollHeight;
  }

  function setStatus(state, label) {
    document.getElementById('statusDot').className = 'status-dot ' + (state || '');
    document.getElementById('statusLabel').textContent = label;
  }

  function switchTab(name) {
    document.querySelectorAll('.data-tabs .tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === name);
    });
    document.querySelectorAll('.tab-content').forEach(c => {
      c.classList.toggle('active', c.id === 'tab-' + name);
    });
  }

  function getConfig() {
    return {
      ip:            document.getElementById('ip').value.trim(),
      protocol:      document.getElementById('protocol').value,
      port:          parseInt(document.getElementById('port').value) || null,
      username:      document.getElementById('username').value || null,
      password:      document.getElementById('password').value || null,
      poll_interval: parseFloat(document.getElementById('poll').value) || 2.0,
    };
  }

  async function doPing() {
    const cfg = getConfig();
    if (!cfg.ip) { log('Enter an IP address first.', 'err'); return; }
    if (!cfg.protocol) { log('Select a protocol.', 'err'); return; }
    log(`Pinging ${cfg.ip} via ${cfg.protocol} …`);
    setStatus('no_data', 'TESTING…');
    document.getElementById('connectBtn').disabled = true;
    document.getElementById('pagesSection').classList.add('hidden');

    try {
      const res  = await fetch('/ping', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
      const data = await res.json();

      if (data.reachable) {
        log(`✓ Reachable — ${data.detail}`, 'ok');
        if (data.machine_info?.model)  log(`  Model: ${data.machine_info.model}`, 'info');
        if (data.machine_info?.status) log(`  Status: ${data.machine_info.status}`, 'info');
        setStatus('no_data', 'REACHABLE');

        // Only enable the live-stream button if the selected protocol supports it
        // and is implemented.
        const p = PROTO_BY_ID[cfg.protocol];
        const canStream = p && p.implemented && p.supports_live_stream;
        document.getElementById('connectBtn').disabled = !canStream;
        if (!canStream) {
          log('  Live stream not available for this protocol.', 'warn');
        }

        if (data.pages && Object.keys(data.pages).length) {
          const listEl = document.getElementById('pagesList');
          listEl.innerHTML = '';
          for (const [name, ok] of Object.entries(data.pages)) {
            const tag = document.createElement('span');
            tag.className = 'page-tag ' + (ok ? 'ok' : 'err');
            tag.textContent = name;
            listEl.appendChild(tag);
            if (!ok) log(`  Page /${name} not available`, 'warn');
          }
          document.getElementById('pagesSection').classList.remove('hidden');
        }
      } else {
        log(`✗ ${data.detail}`, 'err');
        setStatus('offline', 'UNREACHABLE');
      }
    } catch (e) {
      log('Request failed: ' + e, 'err');
      setStatus('offline', 'ERROR');
    }
  }

  function doConnect() {
    if (ws) ws.close();
    const cfg = getConfig();
    if (!cfg.ip) { log('Enter IP address.', 'err'); return; }
    if (!cfg.protocol) { log('Select a protocol.', 'err'); return; }

    pollCount = 0;
    nodeData = {};
    document.getElementById('liveContent').classList.add('hidden');
    document.getElementById('liveContent').innerHTML = '';
    document.getElementById('emptyState').classList.remove('hidden');
    document.getElementById('emptyState').style.display = 'flex';
    document.getElementById('pollCount').textContent = '—';
    document.getElementById('nodeCount').textContent = '—';
    document.getElementById('lastUpdate').textContent = '—';
    document.getElementById('pollInterval').textContent = cfg.poll_interval + 's';
    document.getElementById('machinePill').classList.remove('show');

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/stream`);

    setStatus('no_data', 'CONNECTING…');
    document.getElementById('stopBtn').disabled = false;
    document.getElementById('connectBtn').disabled = true;
    document.getElementById('pingBtn').disabled = true;

    ws.onopen    = () => ws.send(JSON.stringify(cfg));
    ws.onmessage = e  => handleMessage(JSON.parse(e.data));
    ws.onerror   = () => { log('WebSocket error', 'err'); setStatus('offline', 'ERROR'); };
    ws.onclose   = () => {
      log('Stream stopped.', 'warn');
      setStatus('', 'DISCONNECTED');
      document.getElementById('connectBtn').disabled = false;
      document.getElementById('pingBtn').disabled = false;
      document.getElementById('stopBtn').disabled = true;
    };
  }

  function doStop() { if (ws) { ws.close(); ws = null; } }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'status':
        log(msg.msg, 'info');
        setStatus('no_data', 'CONNECTING…');
        break;

      case 'nodes':
        msg.data.forEach(n => { nodeData[n.path] = { value: n.value, type: n.type, page: guessPage(n.path) }; });
        renderAll();
        document.getElementById('nodeCount').textContent = msg.data.length;
        setStatus('running', 'CONNECTED');
        log(`Streaming ${msg.data.length} data points.`, 'ok');

        const modelKey  = Object.keys(nodeData).find(k => k.endsWith('/model'));
        const statusKey = Object.keys(nodeData).find(k => k.endsWith('/status'));
        if (modelKey || statusKey) {
          const pill = document.getElementById('machinePill');
          pill.textContent = [nodeData[modelKey]?.value, nodeData[statusKey]?.value].filter(Boolean).join(' — ');
          pill.classList.add('show');
        }
        break;

      case 'poll':
        pollCount++;
        document.getElementById('pollCount').textContent = pollCount;
        document.getElementById('lastUpdate').textContent = new Date(msg.ts).toLocaleTimeString();
        setStatus('running', 'CONNECTED');

        let changed = false;
        for (const [k, v] of Object.entries(msg.data)) {
          const prev = nodeData[k];
          const isNew = !prev;
          const isChanged = prev && prev.value !== v.value;
          nodeData[k] = { value: v.value, type: v.type, page: v.page || guessPage(k) };
          if (isNew)     changed = true;
          if (isChanged) flashCard(k, v.value);
        }
        if (changed) {
          renderAll();
          document.getElementById('nodeCount').textContent = Object.keys(nodeData).length;
        }

        const clock = nodeData['machine/clock'];
        if (clock) {
          const statusNode = nodeData['machine/status'];
          const modelNode  = nodeData['machine/model'];
          const pill = document.getElementById('machinePill');
          pill.textContent = [modelNode?.value, statusNode?.value].filter(Boolean).join(' — ');
          pill.classList.add('show');
        }

        document.getElementById('rawView').textContent = JSON.stringify(msg, null, 2);
        break;

      case 'error':
        log('ERROR: ' + msg.msg, 'err');
        setStatus('offline', 'ERROR');
        break;

      case 'info':
        log(msg.msg, 'info');
        break;
    }
  }

  function guessPage(path) { return path.split('/')[0] || 'unknown'; }

  function renderAll() {
    const container = document.getElementById('liveContent');
    container.innerHTML = '';

    const groups = {};
    for (const [path, node] of Object.entries(nodeData)) {
      const page = node.page || guessPage(path);
      if (!groups[page]) groups[page] = [];
      groups[page].push({ path, ...node });
    }

    const orderedPages = ['header', 'running_log', 'work_counter', 'alarm_log', 'tool', 'status_log', 'mainte_info', 'measure_result', 'unknown'];
    for (const page of orderedPages) {
      if (!groups[page] || groups[page].length === 0) continue;
      const section = document.createElement('div');
      const label = PAGE_LABELS[page] || page;
      section.innerHTML = `<div class="page-section-title">${esc(label)} <span class="count-badge">${groups[page].length}</span></div><div class="node-grid"></div>`;
      container.appendChild(section);

      const grid = section.querySelector('.node-grid');
      for (const node of groups[page]) {
        const shortLabel = node.path.replace(page + '/', '').replace('running_log/', '').replace('machine/', '');
        const card = document.createElement('div');
        card.className = 'node-card' + (page === 'header' ? ' header-card' : '');
        card.id = 'card-' + CSS.escape(node.path);
        card.innerHTML = `<div class="node-path">${esc(shortLabel)}</div><div class="node-value">${esc(node.value)}</div>`;
        grid.appendChild(card);
      }
    }

    document.getElementById('emptyState').classList.add('hidden');
    container.classList.remove('hidden');
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.gap = '16px';
  }

  function flashCard(path, newValue) {
    const card = document.getElementById('card-' + CSS.escape(path));
    if (!card) return;
    const valEl = card.querySelector('.node-value');
    if (valEl) {
      valEl.textContent = newValue;
      valEl.classList.add('changed');
      card.classList.add('flash');
      setTimeout(() => { valEl.classList.remove('changed'); card.classList.remove('flash'); }, 1500);
    }
  }

  async function init() {
    document.getElementById('pingBtn').addEventListener('click', doPing);
    document.getElementById('connectBtn').addEventListener('click', doConnect);
    document.getElementById('stopBtn').addEventListener('click', doStop);
    document.getElementById('protocol').addEventListener('change', onProtoChange);
    document.querySelectorAll('.data-tabs .tab').forEach(t => {
      t.addEventListener('click', () => switchTab(t.dataset.tab));
    });
    await loadProtocols();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
