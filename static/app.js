// Modal overlays (FAB → create forms)
function stOpenModal(id) {
  const m = document.getElementById(id);
  if (m) { m.classList.add('open'); const f = m.querySelector('input,select,textarea'); if (f) f.focus(); }
}
function stCloseModal(el) {
  const m = el.closest ? el.closest('.modal-backdrop') : document.getElementById(el);
  if (m) m.classList.remove('open');
}
document.addEventListener('click', (e) => {
  if (e.target.classList && e.target.classList.contains('modal-backdrop')) {
    e.target.classList.remove('open');
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open'));
});

// List / cards view toggle, persisted per page-scope key
function stSetView(scope, mode) {
  document.body.setAttribute('data-view', mode);
  try { localStorage.setItem('st-view-' + scope, mode); } catch (e) {}
  document.querySelectorAll('.view-toggle button').forEach(b => {
    b.classList.toggle('on', b.dataset.mode === mode);
  });
}
function stInitView(scope, def) {
  let mode = def || 'list';
  try { mode = localStorage.getItem('st-view-' + scope) || mode; } catch (e) {}
  stSetView(scope, mode);
}

// Image upload preview
function stPreview(input) {
  const wrap = input.closest('.file-field');
  if (!wrap || !input.files || !input.files[0]) return;
  let img = wrap.querySelector('img.preview');
  if (!img) { img = document.createElement('img'); img.className = 'preview'; wrap.appendChild(img); }
  img.src = URL.createObjectURL(input.files[0]);
}

// ---- Markdown "/" slash menu for .md-field textareas ----
const ST_SLASH = [
  { label: "☑ Checklist", snippet: "- [ ] " },
  { label: "• Bullet list", snippet: "- " },
  { label: "1. Numbered list", snippet: "1. " },
  { label: "# Heading", snippet: "## " },
];
let stSlashMenu = null, stSlashTarget = null;
function stHideSlash() { if (stSlashMenu) stSlashMenu.style.display = "none"; stSlashTarget = null; }
function stApplySlash(snippet) {
  const ta = stSlashTarget; if (!ta) return;
  const pos = ta.selectionStart;
  const before = ta.value.slice(0, pos).replace(/\/$/, "") + snippet;
  ta.value = before + ta.value.slice(pos);
  ta.setSelectionRange(before.length, before.length); ta.focus();
  stHideSlash();
}
document.addEventListener("input", (e) => {
  const ta = e.target;
  if (!ta.classList || !ta.classList.contains("md-field")) return;
  const before = ta.value.slice(0, ta.selectionStart);
  if (/(^|\n)\/$/.test(before)) {
    if (!stSlashMenu) {
      stSlashMenu = document.createElement("div");
      stSlashMenu.className = "slash-menu";
      ST_SLASH.forEach((o) => {
        const b = document.createElement("button");
        b.type = "button"; b.textContent = o.label;
        b.addEventListener("mousedown", (ev) => { ev.preventDefault(); stApplySlash(o.snippet); });
        stSlashMenu.appendChild(b);
      });
      document.body.appendChild(stSlashMenu);
    }
    stSlashTarget = ta;
    const r = ta.getBoundingClientRect();
    stSlashMenu.style.display = "flex";
    stSlashMenu.style.left = (window.scrollX + r.left + 8) + "px";
    stSlashMenu.style.top = (window.scrollY + r.top + 30) + "px";
  } else {
    stHideSlash();
  }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") stHideSlash(); });
document.addEventListener("click", (e) => {
  if (stSlashMenu && !stSlashMenu.contains(e.target) && e.target !== stSlashTarget) stHideSlash();
});

// Invoice: hide new-customer fields when an existing client is chosen
function stToggleNewClient(sel) {
  const box = document.getElementById('newClient');
  if (box) box.style.display = sel.value ? 'none' : '';
}

// Wallpaper slider live output
function stRange(input) {
  const out = document.getElementById(input.dataset.output);
  if (out) out.textContent = input.value + (input.dataset.unit || '');
}

// Sidebar collapse/expand (persisted)
function stSetSidebar(state) {
  document.documentElement.setAttribute('data-sidebar', state);
  try { localStorage.setItem('st-sidebar', state); } catch (e) {}
}
function stToggleSidebar() {
  stSetSidebar(document.documentElement.getAttribute('data-sidebar') === 'open' ? 'closed' : 'open');
}
function stInitSidebar() {
  let s = (window.ST_SIDEBAR_DEFAULT === 'open') ? 'open' : 'closed';
  try { s = localStorage.getItem('st-sidebar') || s; } catch (e) {}
  stSetSidebar(s);
}

// ---- Dashboard widget sortable (simple HTML5 drag) ----
function stSyncOrder() {
  const list = document.getElementById('widgetSort');
  const hidden = document.getElementById('widgetOrder');
  if (list && hidden) {
    hidden.value = [...list.querySelectorAll('li')].map(li => li.dataset.key).join(',');
  }
}
function stSortableInit(listId, hiddenId) {
  const list = document.getElementById(listId);
  if (!list) return;
  let dragged = null;
  list.querySelectorAll('li').forEach(li => {
    li.addEventListener('dragstart', () => { dragged = li; li.classList.add('dragging'); });
    li.addEventListener('dragend', () => { li.classList.remove('dragging'); stSyncOrder(); });
    li.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragged || dragged === li) return;
      const r = li.getBoundingClientRect();
      const after = e.clientY > r.top + r.height / 2;
      list.insertBefore(dragged, after ? li.nextSibling : li);
    });
  });
  stSyncOrder();
}

// ---- Keyboard shortcuts ----
const ST_NAV = { d: '/', p: '/projects', w: '/warehouse', h: '/hub', t: '/tasks', s: '/stats' };
let stGoPending = false, stGoTimer = null;

function stTyping(e) {
  const t = e.target;
  return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
}

document.addEventListener('keydown', (e) => {
  // Help: Ctrl+/ (Ctrl+? on most layouts is Ctrl+Shift+/)
  if (e.ctrlKey && (e.key === '/' || e.key === '?')) {
    e.preventDefault(); stOpenModal('shortcutHelp'); return;
  }
  if (stTyping(e) || e.ctrlKey || e.metaKey || e.altKey) return;

  // "g" then a nav key
  if (stGoPending && ST_NAV[e.key]) {
    stGoPending = false; clearTimeout(stGoTimer);
    window.location.href = ST_NAV[e.key]; return;
  }
  if (e.key === 'g') {
    stGoPending = true; clearTimeout(stGoTimer);
    stGoTimer = setTimeout(() => { stGoPending = false; }, 1000); return;
  }
  // "n" → primary create on this page (clicks the FAB)
  if (e.key === 'n') {
    const fab = document.querySelector('.fab');
    if (fab) { e.preventDefault(); fab.click(); }
    return;
  }
  // "v" → toggle list/cards view if a toggle exists
  if (e.key === 'v') {
    const btns = document.querySelectorAll('.view-toggle button');
    if (btns.length === 2) {
      const cur = document.body.getAttribute('data-view') || 'list';
      btns.forEach(b => { if (b.dataset.mode !== cur) b.click(); });
    }
  }
});

// ---- Auto-init (app.js is deferred, so the DOM is already parsed here) ----
stInitSidebar();
(function () {
  const vt = document.querySelector('[data-view-scope]');
  if (vt) stInitView(vt.getAttribute('data-view-scope'), vt.getAttribute('data-view-default') || 'list');
  if (document.getElementById('widgetSort')) stSortableInit('widgetSort', 'widgetOrder');
})();
