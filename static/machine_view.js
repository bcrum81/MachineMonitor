/* ══════════════════════════════════════════════════════════════════════
   machine_view.js — WebSocket live-stream viewer for a saved machine
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // Path is /machine/{id}/live — the id is the second-to-last segment
  const pathParts = location.pathname.split('/').filter(Boolean);
  const machineId = pathParts[pathParts.length - 2];

  let ws = null;
  let pollCount = 0;
  let nodeData = {};

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

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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

  function guessPage(path) { return path.split('/')[0] || 'unknown'; }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'status':
        document.getElementById('emptyTitle').textContent = 'Connecting to machine…';
        document.getElementById('statusMsg').textContent = msg.msg;
        setStatus('no_data', 'CONNECTING…');
        break;

      case 'nodes':
        msg.data.forEach(n => {
          nodeData[n.path] = { value: n.value, type: n.type, page: guessPage(n.path) };
        });
        renderAll();
        document.getElementById('nodeCount').textContent = msg.data.length;
        setStatus('running', 'CONNECTED');

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
          if (!prev) changed = true;
          else if (prev.value !== v.value) flashCard(k, v.value);
          nodeData[k] = { value: v.value, type: v.type, page: v.page || guessPage(k) };
        }
        if (changed) {
          renderAll();
          document.getElementById('nodeCount').textContent = Object.keys(nodeData).length;
        }

        const clock = nodeData['machine/clock'];
        if (clock) {
          const pill = document.getElementById('machinePill');
          const modelNode  = nodeData['machine/model'];
          const statusNode = nodeData['machine/status'];
          pill.textContent = [modelNode?.value, statusNode?.value].filter(Boolean).join(' — ');
          pill.classList.add('show');
        }
        document.getElementById('rawView').textContent = JSON.stringify(msg, null, 2);
        break;

      case 'error':
        document.getElementById('emptyTitle').textContent = 'Machine Offline';
        document.getElementById('statusMsg').textContent = 'Will reconnect automatically when the machine is reachable.';
        setStatus('offline', 'OFFLINE');
        break;
    }
  }

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
    document.querySelectorAll('.data-tabs .tab').forEach(t => {
      t.addEventListener('click', () => switchTab(t.dataset.tab));
    });

    const res = await fetch('/api/machines');
    if (res.ok) {
      const machines = await res.json();
      const machine = machines.find(m => m.id === machineId);
      if (machine) {
        document.title = machine.name + ' — Live View';
        const titleEl = document.querySelector('.app-title-main');
        if (titleEl) titleEl.textContent = machine.name + ' · Live';
      } else {
        document.getElementById('emptyTitle').textContent = 'Machine Not Found';
        setStatus('offline', 'NOT FOUND');
        return;
      }
    }
    connectWS();
  }

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(proto + '://' + location.host + '/stream/' + machineId);
    setStatus('no_data', 'CONNECTING…');
    ws.onmessage = e => handleMessage(JSON.parse(e.data));
    ws.onerror   = () => {};
    ws.onclose   = () => {
      setStatus('offline', 'OFFLINE');
      setTimeout(connectWS, 5000);
    };
    document.getElementById('pollInterval').textContent = '—';
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
