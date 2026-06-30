/* ══════════════════════════════════════════════════════════════════════
   nav.js — shared navigation for all CNC Shop Monitor pages
   Renders:
     - top <header class="app-header">
     - off-canvas side menu (right-side slide-out)
   Also:
     - persists dark/light theme in localStorage
     - fetches /api/admin/check to show the right login/logout button
   Usage:
     1. Include /static/styles.css
     2. Before </body>, include <script src="/static/nav.js"></script>
     3. Optionally set window.CNC_NAV = { title: 'Reports', subtitle: 'Cycle Times', active: 'reports' };
        BEFORE loading nav.js to customize the page title shown in the header.
        `active` should match one of: dashboard, tester, admin, integrations, reports, machine
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ── Config ────────────────────────────────────────────────────────────
  const cfg = window.CNC_NAV || {};
  const pageTitle    = cfg.title    || 'Shop Monitor';
  const pageSubtitle = cfg.subtitle || '';
  const activeKey    = cfg.active   || autoDetectActive();

  function autoDetectActive() {
    const p = location.pathname;
    if (p === '/' || p === '')              return 'dashboard';
    if (p.startsWith('/admin/integrations'))return 'integrations';
    if (p.startsWith('/admin/reports'))     return 'reports';
    if (p.startsWith('/admin'))             return 'admin';
    if (p.startsWith('/tester'))            return 'tester';
    if (p.startsWith('/machine/'))          return 'machine';
    return '';
  }

  // ── Theme ─────────────────────────────────────────────────────────────
  function getTheme() {
    return localStorage.getItem('theme') || 'dark';
  }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
    updateThemeButton(t);
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    applyTheme(cur === 'dark' ? 'light' : 'dark');
  }
  function updateThemeButton(t) {
    const btns = document.querySelectorAll('.theme-toggle');
    const label = t === 'dark' ? '🌙 Dark' : '☀️ Light';
    btns.forEach(b => b.textContent = label);
  }
  // Apply theme immediately — before header renders, to avoid a flash
  applyTheme(getTheme());

  // ── Menu items (order matters) ────────────────────────────────────────
  const MENU_ITEMS = [
    { key: 'dashboard',    href: '/',                    label: 'Machine List',  badge: 'Public' },
    { key: 'tester',       href: '/tester',              label: 'Machine Tester',badge: 'Admin'  },
    { key: 'admin',        href: '/admin',               label: 'Admin Panel',   badge: 'Admin'  },
    { key: 'integrations', href: '/admin/integrations',  label: 'Integrations',  badge: 'Admin'  },
    { key: 'reports',      href: '/admin/reports',       label: 'Reports',       badge: 'Admin'  },
  ];

  // ── Render header ─────────────────────────────────────────────────────
  function renderHeader() {
    // Look for a placeholder element first; otherwise inject at top of body
    let host = document.getElementById('app-nav-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'app-nav-host';
      document.body.insertBefore(host, document.body.firstChild);
    }
    const sub = pageSubtitle
      ? `<div class="app-title-sub">${escHtml(pageSubtitle)}</div>`
      : '';
    host.innerHTML = `
      <header class="app-header">
        <div class="app-header-left">
          <div class="app-logo">CNC</div>
          <div class="app-title-block">
            <div class="app-title-main">${escHtml(pageTitle)}</div>
            ${sub}
          </div>
        </div>
        <div class="app-header-right" id="app-header-right-extras">
          <button type="button" class="theme-toggle" id="nav-theme-toggle">🌙 Dark</button>
          <button type="button" class="menu-toggle" id="nav-menu-toggle" aria-label="Open menu">
            <span class="icon"><span></span></span>
            Menu
          </button>
        </div>
      </header>

      <div class="side-menu-backdrop" id="nav-backdrop"></div>

      <aside class="side-menu" id="nav-side-menu" aria-hidden="true">
        <div class="side-menu-header">
          <div class="title">Navigation</div>
          <button type="button" class="side-menu-close" id="nav-close" aria-label="Close menu">✕</button>
        </div>
        <div class="side-menu-body">
          <div class="side-menu-section-label">Shop Floor</div>
          ${menuItemHtml('dashboard')}
          ${menuItemHtml('tester')}

          <div class="side-menu-section-label">Administration</div>
          ${menuItemHtml('admin')}
          ${menuItemHtml('integrations')}
          ${menuItemHtml('reports')}
        </div>
        <div class="side-menu-footer">
          <div class="side-menu-session" id="nav-session-label">Checking session&hellip;</div>
          <a class="side-menu-btn" href="#" id="nav-auth-btn">Login</a>
        </div>
      </aside>
    `;
    updateThemeButton(getTheme());
  }

  function menuItemHtml(key) {
    const item = MENU_ITEMS.find(x => x.key === key);
    if (!item) return '';
    const isActive = key === activeKey ? ' active' : '';
    const badgeHtml = item.badge
      ? `<span class="sm-badge">${escHtml(item.badge)}</span>`
      : '';
    return `
      <a class="side-menu-item${isActive}" href="${item.href}">
        <span class="sm-icon">${iconFor(key)}</span>
        <span class="sm-label">${escHtml(item.label)}</span>
        ${badgeHtml}
      </a>
    `;
  }

  function iconFor(key) {
    // Simple inline SVG icons, stroked with currentColor
    const icons = {
      dashboard: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5"/><rect x="9" y="2" width="5" height="5"/><rect x="2" y="9" width="5" height="5"/><rect x="9" y="9" width="5" height="5"/></svg>',
      tester:    '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3h10v10H3z"/><path d="M6 6l4 4M10 6l-4 4"/></svg>',
      admin:     '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="2.5"/><path d="M3 14c0-2.5 2.5-4 5-4s5 1.5 5 4"/></svg>',
      integrations:'<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 8h6M2 8a2 2 0 0 1 2-2h0a2 2 0 0 1 2 2v0a2 2 0 0 1-2 2h0a2 2 0 0 1-2-2zM10 8a2 2 0 0 1 2-2h0a2 2 0 0 1 2 2v0a2 2 0 0 1-2 2h0a2 2 0 0 1-2-2z"/></svg>',
      reports:   '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 13V5M7 13V3M11 13v-6M14 13H2"/></svg>',
    };
    return icons[key] || '';
  }

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ── Menu open/close ───────────────────────────────────────────────────
  function openMenu()  {
    document.getElementById('nav-side-menu').classList.add('open');
    document.getElementById('nav-backdrop').classList.add('open');
    document.getElementById('nav-side-menu').setAttribute('aria-hidden', 'false');
  }
  function closeMenu() {
    document.getElementById('nav-side-menu').classList.remove('open');
    document.getElementById('nav-backdrop').classList.remove('open');
    document.getElementById('nav-side-menu').setAttribute('aria-hidden', 'true');
  }

  // ── Session state (login/logout button) ───────────────────────────────
  async function refreshSession() {
    const label   = document.getElementById('nav-session-label');
    const authBtn = document.getElementById('nav-auth-btn');
    if (!label || !authBtn) return;

    let isAdmin = false;
    try {
      const res = await fetch('/api/admin/check', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        isAdmin = data.admin === true;
      }
    } catch (_) { isAdmin = false; }

    if (isAdmin) {
      label.innerHTML = 'Signed in <span class="admin-pill">Admin</span>';
      authBtn.textContent = 'Logout';
      authBtn.href = '/logout';
      authBtn.className = 'side-menu-btn danger';
      document.body.classList.add('is-admin');
    } else {
      label.textContent = 'Not signed in';
      authBtn.textContent = 'Login';
      authBtn.href = '/login';
      authBtn.className = 'side-menu-btn primary';
      document.body.classList.remove('is-admin');
    }
    // Notify any page scripts that care
    window.dispatchEvent(new CustomEvent('cnc:session', { detail: { isAdmin } }));
  }

  // ── Wire up ───────────────────────────────────────────────────────────
  function init() {
    renderHeader();

    document.getElementById('nav-menu-toggle').addEventListener('click', openMenu);
    document.getElementById('nav-close').addEventListener('click', closeMenu);
    document.getElementById('nav-backdrop').addEventListener('click', closeMenu);
    document.getElementById('nav-theme-toggle').addEventListener('click', toggleTheme);

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeMenu();
    });

    refreshSession();
  }

  // Expose a small helper API for pages that want it
  window.CNC = window.CNC || {};
  window.CNC.openMenu  = openMenu;
  window.CNC.closeMenu = closeMenu;
  window.CNC.toggleTheme = toggleTheme;
  window.CNC.refreshSession = refreshSession;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
