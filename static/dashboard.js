/* ══════════════════════════════════════════════════════════════════════
   dashboard.js — Public dashboard logic
   Theme + session state are handled by nav.js. This file just handles
   the card grid, polling, drag-reorder, and cycle counters.

   Protocol-aware rendering:
     - http_brother (or any pallet-having machine): legacy P1/P2 layout
       driven by pallet_count, reads running_log/* fields.
     - focas_fanuc:  single program slot, reads program/number directly.
       Cycle time is seeded from /api/machine/{id}/current-cycle so it
       reflects actual elapsed time (not just time since page load).
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ── Inject dashboard-only header extras into the shared nav header ────
  function injectHeaderExtras() {
    const host = document.getElementById('app-header-right-extras');
    const extras = document.getElementById('dashboard-header-extras');
    if (!host || !extras) return;
    const nodes = Array.from(extras.children);
    nodes.reverse().forEach(n => host.insertBefore(n, host.firstChild));
    extras.remove();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(injectHeaderExtras, 0));
  } else {
    setTimeout(injectHeaderExtras, 0);
  }

  // ── Wall clock ────────────────────────────────────────────────────────
  function tickClock() {
    const el = document.getElementById('wall-clock');
    if (!el) return;
    el.textContent = new Date().toLocaleTimeString('en-US', {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ── State ─────────────────────────────────────────────────────────────
  let machines      = [];
  let isAdmin       = false;
  let firstLoad     = true;
  let cycleCounters = {};

  // FOCAS cycle counter state, seeded from the server's current-cycle
  // endpoint so we know the real start time:
  //   focasRunning[machineId] = {
  //     startedAtMs:  ms timestamp from the server's cycle.started event
  //     intervalId:   setInterval id for the count-up
  //     fetchedFor:   the started_at iso the seed corresponds to (so we
  //                   only re-fetch when the cycle changes)
  //   }
  let focasRunning  = {};

  // ── Field helpers ─────────────────────────────────────────────────────
  function rawVal(data, key) {
    const entry = data?.[key];
    if (!entry) return null;
    const v = entry.value;
    if (v === undefined || v === null) return null;
    if (v === '--' || v === '-' || v === '') return null;
    return v;
  }

  function val(data, key, fb = '--') {
    const v = rawVal(data, key);
    return v === null ? fb : v;
  }

  function stateOf(status, hasData) {
    if (!hasData) return 'offline';
    const s = (status || '').toLowerCase();
    if (s.includes('run'))   return 'running';
    if (s.includes('stand')) return 'standby';
    return 'standby';
  }

  function pillClass(state) {
    return { running: 'pill-running', standby: 'pill-standby', offline: 'pill-offline' }[state] || 'pill-loading';
  }

  function isRealAlarm(text) {
    if (!text || text === '--') return false;
    if (/\d{4}\/\d{2}\/\d{2}/.test(text)) return false;
    if (text.toLowerCase().includes('history')) return false;
    if (text.trim().length < 3) return false;
    return true;
  }

  function isFocas(machine) {
    return (machine.protocol || '').toLowerCase() === 'focas_fanuc';
  }

  // ── Cycle-time parsing / formatting (Brother) ─────────────────────────
  function parseCycleTime(timeStr) {
    if (!timeStr || timeStr === '--' || timeStr === '-') return 0;
    try {
      const match = timeStr.match(/^(\d{4}):(\d{2}):(\d{2})\.(\d)$/);
      if (!match) return 0;
      const [, hours, minutes, seconds, tenths] = match;
      return parseInt(hours) * 3600 + parseInt(minutes) * 60 + parseInt(seconds) + parseInt(tenths) / 10;
    } catch { return 0; }
  }

  function formatCycleTime(seconds) {
    if (seconds <= 0) return '--';
    const hours = Math.floor(seconds / 3600);
    const mins  = Math.floor((seconds % 3600) / 60);
    const secs  = Math.floor(seconds % 60);
    const tenths = Math.floor((seconds * 10) % 10);
    return String(hours).padStart(4, '0') + ':' +
           String(mins).padStart(2, '0') + ':' +
           String(secs).padStart(2, '0') + '.' + tenths;
  }

  function updateCycleCounter(machineId, newTimeStr, isRunning, pollSuccessful = true) {
    const element = document.getElementById(`cycle-${machineId}`);
    if (!element) return;

    if (!cycleCounters[machineId]) {
      cycleCounters[machineId] = {
        intervalId: null, currentSeconds: 0, lastPollSeconds: 0, isRunning: false
      };
    }

    const state = cycleCounters[machineId];
    const newSeconds = parseCycleTime(newTimeStr || '--');

    if (!isRunning) {
      if (state.intervalId) { clearInterval(state.intervalId); state.intervalId = null; }
      state.isRunning = false;
      state.currentSeconds = 0;
      element.textContent = (newTimeStr === '--' || newTimeStr === '0000:00:00.0') ? '--' : newTimeStr;
      return;
    }

    if (pollSuccessful && newSeconds > 0) {
      state.currentSeconds = newSeconds;
      state.lastPollSeconds = newSeconds;
    }

    if (!state.intervalId && state.currentSeconds > 0) {
      state.intervalId = setInterval(() => {
        state.currentSeconds += 0.1;
        element.textContent = formatCycleTime(state.currentSeconds) + ' ⏱️';
      }, 100);
    }

    state.isRunning = true;
    if (state.currentSeconds > 0) {
      element.textContent = formatCycleTime(state.currentSeconds) + ' ⏱️';
    } else {
      element.textContent = newTimeStr === '--' ? '--' : newTimeStr;
    }
  }

  // ── FOCAS cycle counter ──────────────────────────────────────────────
  // Seeds startedAtMs from the server's /api/machine/{id}/current-cycle
  // endpoint. That endpoint returns the timestamp of the most recent
  // unmatched cycle.started event, which is the source of truth for
  // actual elapsed time (vs. counting from page load).
  //
  // Re-fetches lazily: only when isRunning flips from false to true,
  // or when we don't yet have a seed.
  async function fetchFocasCycleStart(machineId) {
    try {
      const res = await fetch(`/api/machine/${machineId}/current-cycle`);
      if (!res.ok) return null;
      const j = await res.json();
      if (!j.running || !j.started_at) return null;
      const ms = Date.parse(j.started_at);
      if (isNaN(ms)) return null;
      return { startedAtMs: ms, isoTs: j.started_at };
    } catch {
      return null;
    }
  }

  function clearFocasState(machineId) {
    const state = focasRunning[machineId];
    if (state && state.intervalId) clearInterval(state.intervalId);
    delete focasRunning[machineId];
  }

  function renderFocasCycle(machineId, startedAtMs) {
    const element = document.getElementById(`cycle-${machineId}`);
    if (!element) return;
    if (!startedAtMs) {
      element.textContent = '--';
      return;
    }
    const elapsedSec = Math.max(0, (Date.now() - startedAtMs) / 1000);
    element.textContent = formatCycleTime(elapsedSec) + ' ⏱️';
  }

  function updateFocasCycleCounter(machineId, isRunning) {
    const element = document.getElementById(`cycle-${machineId}`);
    if (!element) return;

    let state = focasRunning[machineId];

    if (!isRunning) {
      // Stopped — clear any running counter and show --
      if (state && state.intervalId) clearInterval(state.intervalId);
      delete focasRunning[machineId];
      element.textContent = '--';
      return;
    }

    // Running — make sure we have a seed and a render interval
    if (!state) {
      // Seed asynchronously; render '--' until we have it
      state = { startedAtMs: null, intervalId: null, fetchedFor: null };
      focasRunning[machineId] = state;
      element.textContent = '--';

      fetchFocasCycleStart(machineId).then(seed => {
        // Make sure the state is still around (machine could have stopped
        // or been removed while we were fetching).
        const cur = focasRunning[machineId];
        if (!cur) return;
        if (seed) {
          cur.startedAtMs = seed.startedAtMs;
          cur.fetchedFor  = seed.isoTs;
        } else {
          // No matching cycle.started in the events table — fall back to
          // counting from now, so the dashboard shows *something*.
          cur.startedAtMs = Date.now();
          cur.fetchedFor  = '_local_fallback_';
        }
        renderFocasCycle(machineId, cur.startedAtMs);
        if (!cur.intervalId) {
          cur.intervalId = setInterval(
            () => renderFocasCycle(machineId, cur.startedAtMs),
            100,
          );
        }
      });
      return;
    }

    // Already have a state. If we don't have a seed yet, the fetch is
    // still in flight; don't double-fetch.
    if (state.startedAtMs && !state.intervalId) {
      state.intervalId = setInterval(
        () => renderFocasCycle(machineId, state.startedAtMs),
        100,
      );
    }
    if (state.startedAtMs) {
      renderFocasCycle(machineId, state.startedAtMs);
    }
  }

  // ── FOCAS Last Cycle End fetch + cache ────────────────────────────────
  // The FOCAS plugin doesn't expose a "last cycle end" timestamp in poll
  // data the way Brother does. Instead, we read it from the events table
  // via a dedicated endpoint. Cached per machine so we don't hammer the
  // API every poll tick (default Brother poll interval is 2s).
  const focasLastEndCache = {};   // machineId -> { fetchedAt, endedAt, program }
  const FOCAS_LAST_END_TTL_MS = 30 * 1000;  // 30s cache

  function refreshFocasLastCycleEnd(machine) {
    const endEl = document.getElementById(`end-${machine.id}`);
    if (!endEl) return;

    const cache = focasLastEndCache[machine.id];
    const now = Date.now();
    if (cache && (now - cache.fetchedAt) < FOCAS_LAST_END_TTL_MS) {
      endEl.textContent = cache.endedAt
        ? fmtMachineTime(cache.endedAt, machine.utc_offset)
        : '--';
      return;
    }

    // Mark as in-flight so we don't double-fetch
    focasLastEndCache[machine.id] = {
      fetchedAt: now,
      endedAt:   cache ? cache.endedAt : null,
      program:   cache ? cache.program : '',
    };

    fetch(`/api/machine/${encodeURIComponent(machine.id)}/last-completed-cycle`)
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (!j) return;
        focasLastEndCache[machine.id] = {
          fetchedAt: Date.now(),
          endedAt:   j.ended_at || null,
          program:   j.program || '',
        };
        const el = document.getElementById(`end-${machine.id}`);
        if (el) {
          el.textContent = j.ended_at
            ? fmtMachineTime(j.ended_at, machine.utc_offset)
            : '--';
        }
      })
      .catch(() => { /* leave the previous cache value in place */ });
  }

  function fmtMachineTime(raw, driftHours) {
    // The machine reported a wall-clock timestamp that the operator
    // sees on the controller's panel. The `driftHours` value (stored
    // as machine.utc_offset for backwards compatibility) tells us how
    // much that clock is OFF from local time.
    //
    // Examples:
    //   driftHours = 0      → machine clock matches local time (default)
    //   driftHours = +1     → machine clock is 1 hour BEHIND local
    //                          (so add 1 hour to display correctly)
    //   driftHours = -16    → machine clock is 16 hours AHEAD of local
    //                          (so subtract 16 hours to display correctly)
    //
    // We treat the raw timestamp as wall-clock-local-as-typed-on-the-
    // controller, parse it tz-naive, then add driftHours to get true
    // local time on the shop floor.
    if (!raw || raw === '--' || raw === '-') return raw;
    const drift = (driftHours === null || driftHours === undefined) ? 0 : driftHours;
    try {
      // Parse as local naive time — replace separators
      const norm = raw.replace(/\//g, '-').replace(' ', 'T');
      const d = new Date(norm);
      if (isNaN(d)) return raw;
      const adjusted = new Date(d.getTime() + drift * 3600000);
      const pad = n => String(n).padStart(2, '0');
      return adjusted.getFullYear() + '/' +
             pad(adjusted.getMonth() + 1) + '/' +
             pad(adjusted.getDate()) + ' ' +
             pad(adjusted.getHours()) + ':' +
             pad(adjusted.getMinutes()) + ':' +
             pad(adjusted.getSeconds());
    } catch { return raw; }
  }

  // ── Toast ─────────────────────────────────────────────────────────────
  let toastTimer;
  function showToast(msg) {
    const t = document.getElementById('order-toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
  }

  // ── Drag-and-drop reorder ─────────────────────────────────────────────
  let dragSrcId = null;

  function enableDrag(card, machineId) {
    card.setAttribute('draggable', 'true');

    card.addEventListener('dragstart', e => {
      dragSrcId = machineId;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });

    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      document.querySelectorAll('.machine-card').forEach(c => c.classList.remove('drag-over'));
    });

    card.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (dragSrcId !== machineId) card.classList.add('drag-over');
    });

    card.addEventListener('dragleave', () => {
      card.classList.remove('drag-over');
    });

    card.addEventListener('drop', e => {
      e.preventDefault();
      card.classList.remove('drag-over');
      if (!dragSrcId || dragSrcId === machineId) return;

      const fromIdx = machines.findIndex(m => m.id === dragSrcId);
      const toIdx   = machines.findIndex(m => m.id === machineId);
      if (fromIdx === -1 || toIdx === -1) return;

      const moved = machines.splice(fromIdx, 1)[0];
      machines.splice(toIdx, 0, moved);

      const grid = document.getElementById('machine-grid');
      machines.forEach(m => {
        const el = document.getElementById(`card-${m.id}`);
        if (el) grid.appendChild(el);
      });

      saveOrder();
    });
  }

  async function saveOrder() {
    try {
      const order = machines.map(m => m.id);
      const res = await fetch('/api/admin/machine-order', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
      });
      if (res.ok) showToast('Order saved');
    } catch (e) {
      console.error('saveOrder failed:', e);
    }
  }

  // ── Card builder ──────────────────────────────────────────────────────
  function buildProgramRow(machine) {
    if (isFocas(machine)) {
      return `
        <div class="prog-row prog-row-single">
          <div class="prog-item prog-item-full">
            <div class="data-label">Program</div>
            <div class="data-value v-xl" id="prog1-${machine.id}">—</div>
          </div>
        </div>
      `;
    }
    const pc = Number(machine.pallet_count ?? 2);
    if (pc === 0) return '';
    if (pc === 1) {
      return `
        <div class="prog-row prog-row-single">
          <div class="prog-item prog-item-full">
            <div class="data-label">Program</div>
            <div class="data-value v-xl" id="prog1-${machine.id}">—</div>
          </div>
        </div>
      `;
    }
    return `
      <div class="prog-row">
        <div class="prog-item" id="prog-cell-1-${machine.id}">
          <div class="data-label">Program · P1</div>
          <div class="data-value" id="prog1-${machine.id}">—</div>
        </div>
        <div class="prog-item" id="prog-cell-2-${machine.id}">
          <div class="data-label">Program · P2</div>
          <div class="data-value" id="prog2-${machine.id}">—</div>
        </div>
      </div>
    `;
  }

  function buildCard(machine) {
    const div = document.createElement('div');
    div.className = 'machine-card';
    div.id = `card-${machine.id}`;

    const bodyGrid = isFocas(machine)
      ? `
        <div class="data-grid-2col">
          <div class="data-cell">
            <div class="data-label">Cycle Time</div>
            <div class="data-value" id="cycle-${machine.id}">—</div>
          </div>
          <div class="data-cell">
            <div class="data-label">Last Cycle End</div>
            <div class="data-value v-dim" id="end-${machine.id}">—</div>
          </div>
        </div>
      `
      : `
        <div class="data-grid-2col">
          <div class="data-cell">
            <div class="data-label">Cycle Time</div>
            <div class="data-value" id="cycle-${machine.id}">—</div>
          </div>
          <div class="data-cell">
            <div class="data-label">Last Cycle End</div>
            <div class="data-value v-dim" id="end-${machine.id}">—</div>
          </div>
        </div>
      `;

    div.innerHTML = `
      <div class="card-header">
        <div class="card-header-left">
          <div class="drag-handle" title="Drag to reorder">
            <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor">
              <rect x="5" y="3" width="2" height="2" rx="1"/>
              <rect x="11" y="3" width="2" height="2" rx="1"/>
              <rect x="5" y="8" width="2" height="2" rx="1"/>
              <rect x="11" y="8" width="2" height="2" rx="1"/>
              <rect x="5" y="13" width="2" height="2" rx="1"/>
              <rect x="11" y="13" width="2" height="2" rx="1"/>
            </svg>
          </div>
          <div class="card-name-group">
            <a href="/machine/${machine.id}" style="text-decoration:none;color:inherit;">
              <div class="card-name">${machine.name}</div>
            </a>
            <div class="card-model" id="model-${machine.id}">—</div>
          </div>
        </div>
        <div class="status-pill pill-loading" id="badge-${machine.id}">…</div>
      </div>
      <div class="card-body">
        ${buildProgramRow(machine)}
        ${bodyGrid}
        <div class="alarm-banner" id="alarm-${machine.id}"></div>
      </div>
    `;

    if (isAdmin) enableDrag(div, machine.id);
    return div;
  }

  function detectActivePallet(data, isRunning) {
    if (!isRunning) return null;
    const p1End = rawVal(data, 'running_log/Pallet 1 operation end date and time');
    const p2End = rawVal(data, 'running_log/Pallet 2 operation end date and time');
    if (!p1End && p2End) return 1;
    if (!p2End && p1End) return 2;
    return null;
  }

  // ── Card updater ──────────────────────────────────────────────────────
  function updateCard(machine, row) {
    const data      = row?.data || {};
    const hasData   = !!row;
    const status    = val(data, 'machine/status', hasData ? 'Standby' : 'Offline');
    const state     = stateOf(status, hasData);
    const isRunning = status.toLowerCase().includes('run');

    const card = document.getElementById(`card-${machine.id}`);
    if (!card) return;

    const draggable = card.getAttribute('draggable');
    card.className = `machine-card state-${state}`;
    if (draggable) card.setAttribute('draggable', draggable);

    const cardHeader = card.querySelector('.card-header');
    if (cardHeader) {
      if (isRunning) cardHeader.classList.add('running');
      else           cardHeader.classList.remove('running');
    }

    const badge = document.getElementById(`badge-${machine.id}`);
    badge.textContent = status;
    badge.className = `status-pill ${pillClass(state)}`;

    const model = val(data, 'machine/model', '');
    document.getElementById(`model-${machine.id}`).textContent = model;

    // ─── FOCAS branch ────────────────────────────────────────────────
    if (isFocas(machine)) {
      const progEl = document.getElementById(`prog1-${machine.id}`);
      if (progEl) {
        const program = rawVal(data, 'program/number') || '--';
        progEl.textContent = program;
        if (isRunning && program !== '--') {
          progEl.classList.add('prog-number-active');
        } else {
          progEl.classList.remove('prog-number-active');
        }
      }

      updateFocasCycleCounter(machine.id, isRunning);
      refreshFocasLastCycleEnd(machine);

      const alarmEl = document.getElementById(`alarm-${machine.id}`);
      if (alarmEl) alarmEl.classList.remove('visible');
      return;
    }

    // ─── Brother / pallet-aware branch ──────────────────────────────
    const pc = Number(machine.pallet_count ?? 2);

    if (pc >= 1) {
      const p1El = document.getElementById(`prog1-${machine.id}`);
      if (p1El) {
        const p1Program =
          rawVal(data, 'running_log/Pallet 1 program') ||
          rawVal(data, 'running_log/Program') ||
          '--';
        p1El.textContent = p1Program;
      }
    }
    if (pc >= 2) {
      const p2El = document.getElementById(`prog2-${machine.id}`);
      if (p2El) {
        const p2Program = rawVal(data, 'running_log/Pallet 2 program') || '--';
        p2El.textContent = p2Program;
      }

      const activePallet = detectActivePallet(data, isRunning);
      const cell1 = document.getElementById(`prog-cell-1-${machine.id}`);
      const cell2 = document.getElementById(`prog-cell-2-${machine.id}`);
      const prog1 = document.getElementById(`prog1-${machine.id}`);
      const prog2 = document.getElementById(`prog2-${machine.id}`);
      if (cell1 && cell2 && prog1 && prog2) {
        cell1.classList.remove('prog-active', 'prog-inactive');
        cell2.classList.remove('prog-active', 'prog-inactive');
        prog1.classList.remove('prog-number-active');
        prog2.classList.remove('prog-number-active');

        if (activePallet === 1) {
          cell1.classList.add('prog-active');
          cell2.classList.add('prog-inactive');
          prog1.classList.add('prog-number-active');
        } else if (activePallet === 2) {
          cell2.classList.add('prog-active');
          cell1.classList.add('prog-inactive');
          prog2.classList.add('prog-number-active');
        }
      }
    }

    // Cycle time
    let cycleTimeValue = '--';
    if (pc >= 2) {
      const activePallet = detectActivePallet(data, isRunning);
      if (activePallet === 1) cycleTimeValue = val(data, 'running_log/Pallet 1 cycle time', '--');
      else if (activePallet === 2) cycleTimeValue = val(data, 'running_log/Pallet 2 cycle time', '--');
      if (cycleTimeValue === '--') cycleTimeValue = val(data, 'running_log/Cycle time', '--');
    } else {
      cycleTimeValue = val(data, 'running_log/Cycle time', '--');
    }
    updateCycleCounter(machine.id, cycleTimeValue, isRunning, hasData);

    // Last cycle end
    const p1EndTime     = rawVal(data, 'running_log/Pallet 1 operation end date and time');
    const p2EndTime     = rawVal(data, 'running_log/Pallet 2 operation end date and time');
    const singleEndTime = rawVal(data, 'running_log/Operation end date and time');
    let mostRecentEndTime = '--';
    if (p1EndTime && p2EndTime) {
      try {
        const p1Date = new Date(p1EndTime);
        const p2Date = new Date(p2EndTime);
        mostRecentEndTime = (p1Date > p2Date) ? p1EndTime : p2EndTime;
      } catch { mostRecentEndTime = p1EndTime; }
    } else if (p1EndTime)      mostRecentEndTime = p1EndTime;
    else if (p2EndTime)        mostRecentEndTime = p2EndTime;
    else if (singleEndTime)    mostRecentEndTime = singleEndTime;
    const endEl = document.getElementById(`end-${machine.id}`);
    if (endEl) endEl.textContent = fmtMachineTime(mostRecentEndTime, machine.utc_offset);

    // Alarm
    const alarmText = val(data, 'alarm_log/Current alarm', '');
    const alarmEl   = document.getElementById(`alarm-${machine.id}`);
    if (isRealAlarm(alarmText)) {
      alarmEl.textContent = '⚠  ' + alarmText.replace(/\n/g, '  ');
      alarmEl.classList.add('visible');
    } else {
      alarmEl.classList.remove('visible');
    }
  }

  function cardSignature(m) {
    return `${m.protocol || ''}|${m.pallet_count ?? ''}`;
  }

  // ── Load machine list ─────────────────────────────────────────────────
  async function loadMachines() {
    try {
      const res  = await fetch('/api/machines');
      const list = await res.json();
      const grid  = document.getElementById('machine-grid');
      const noMsg = document.getElementById('no-machines');

      if (list.length === 0) {
        grid.style.display = 'none';
        noMsg.style.display = 'block';
        machines = [];
        return;
      }
      noMsg.style.display = 'none';
      grid.style.display  = 'grid';

      const existing = new Map(machines.map(m => [m.id, m]));
      const liveIds  = new Set(list.map(m => m.id));

      list.forEach(m => {
        const prev = existing.get(m.id);
        if (prev && cardSignature(prev) !== cardSignature(m)) {
          const oldCard = document.getElementById(`card-${m.id}`);
          if (oldCard) oldCard.remove();
          existing.delete(m.id);
          clearFocasState(m.id);
          const cc = cycleCounters[m.id];
          if (cc && cc.intervalId) clearInterval(cc.intervalId);
          delete cycleCounters[m.id];
        }
      });

      list.forEach((m, i) => {
        if (!existing.has(m.id)) {
          const card = buildCard(m);
          card.style.animationDelay = firstLoad ? `${i * 60}ms` : '0ms';
          grid.appendChild(card);
        }
      });

      machines = list;

      document.querySelectorAll('.machine-card').forEach(el => {
        const id = el.id.replace(/^card-/, '');
        if (!liveIds.has(id)) {
          el.remove();
          clearFocasState(id);
          const cc = cycleCounters[id];
          if (cc && cc.intervalId) clearInterval(cc.intervalId);
          delete cycleCounters[id];
        }
      });

      firstLoad = false;
    } catch (e) {
      console.error('loadMachines:', e);
    }
  }

  // ── Poll ─────────────────────────────────────────────────────────────
  async function pollAll() {
    if (machines.length === 0) return;
    const statusEl = document.getElementById('refresh-status');
    if (statusEl) statusEl.textContent = 'Updating…';
    await Promise.all(machines.map(async m => {
      try {
        const res = await fetch(`/api/latest/${m.id}`);
        if (res.ok) updateCard(m, await res.json());
        else        updateCard(m, null);
      } catch { updateCard(m, null); }
    }));
    if (statusEl) statusEl.textContent =
      `Live · ${new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
  }

  // ── Boot ──────────────────────────────────────────────────────────────
  window.addEventListener('cnc:session', (e) => {
    isAdmin = !!(e.detail && e.detail.isAdmin);
    if (isAdmin) {
      document.body.classList.add('admin-mode');
      const bar = document.getElementById('reorder-bar');
      if (bar) bar.classList.add('visible');
      machines.forEach(m => {
        const card = document.getElementById(`card-${m.id}`);
        if (card && !card.getAttribute('draggable')) enableDrag(card, m.id);
      });
    }
  });

  async function init() {
    await loadMachines();
    await pollAll();
    setInterval(pollAll, 3000);
    setInterval(loadMachines, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
