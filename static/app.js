// Modal overlays (FAB → create forms)
function stOpenModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  if (id === 'newPart') stResetPartModal();  // always open on a clean slate
  m.classList.add('open');
  const f = m.querySelector('input,select,textarea');
  if (f) f.focus();
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

// Email settings: hide the SMTP/template block when sending via InvoiceNinja
function stEmailProvider(sel) {
  const box = document.getElementById('smtpBlock');
  if (box) box.style.display = sel.value === 'invoiceninja' ? 'none' : '';
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

// ---- Warehouse set/lot modal ----
function stSubprodHtml(opts) {
  opts = opts || {};
  var sug = window.ST_EBAY ? '<button type="button" class="btn small" onclick="stSuggestPrice(this)" title="Suggest price from eBay">🔍</button>' : '';
  var rec = opts.receipt ? '<label class="file-field small">Own receipt (optional)<input type="file" name="part_receipt" accept="application/pdf,image/*"></label>' : '';
  return '<div class="subprod">' +
    '<label class="img-square" title="Choose image">' +
      '<input type="file" name="part_image" accept="image/*" onchange="stImgSquare(this)">' +
      '<span class="is-ph">🖼️</span></label>' +
    '<div class="pf-fields">' +
      '<input name="part_name" placeholder="Product name">' +
      '<div class="row-form"><input name="part_sale" placeholder="Sale value" inputmode="decimal">' + sug + '</div>' +
      '<input name="part_purchase" placeholder="Purchase price (optional)" inputmode="decimal">' +
      '<input name="part_note" placeholder="Note (optional)">' + rec +
    '</div>' +
    '<button type="button" class="act-btn danger" onclick="stRemoveSubprod(this)" title="Remove">✕</button>' +
  '</div>';
}
function stAddSubprod(id, opts) {
  var c = document.getElementById(id || 'setParts');
  if (!c) return;
  c.insertAdjacentHTML('beforeend', stSubprodHtml(opts));
  var card = c.lastElementChild;
  if (card) stWrapPricesIn(card);
}
function stResetPartModal() {
  var m = document.getElementById('newPart');
  if (!m) return;
  var f = m.querySelector('form');
  if (f) f.reset();
  var sp = document.getElementById('setParts'); if (sp) sp.innerHTML = '';
  var panel = document.getElementById('setPanel'); if (panel) panel.setAttribute('hidden', '');
  // form.reset() restores the mode select without firing onchange — re-apply it
  var rm = document.getElementById('wpRecMode'); if (rm) rm.value = 'new';
  stRecMode();
  // clear the image preview back to its placeholder
  m.querySelectorAll('img.is-preview').forEach(function (i) { i.remove(); });
  m.querySelectorAll('.img-square .is-ph').forEach(function (p) { p.style.display = ''; });
}
function stRemoveSubprod(btn) {
  var card = btn.closest('.subprod');
  var list = card ? card.closest('.subprod-list') : null;
  if (!card) return;
  card.remove();  // always remove the row
  if (!list) return;
  var remaining = list.querySelectorAll('.subprod').length;
  // Create modal: an empty set list means "single product" → collapse the panel.
  if (list.id === 'setParts' && remaining === 0) {
    var panel = document.getElementById('setPanel');
    if (panel) panel.setAttribute('hidden', '');
  }
  // Split modal always needs at least one row.
  if (list.id === 'splitParts' && remaining === 0) stAddSubprod('splitParts');
}
function stAddPart() {
  var p = document.getElementById('setPanel');
  if (p) p.removeAttribute('hidden');
  stAddSubprod('setParts', { receipt: true });
}
async function stSuggestPrice(btn) {
  var box = btn.closest('.subprod, tr');
  var nameEl = box.querySelector('input[name="part_name"]');
  var saleEl = box.querySelector('input[name="part_sale"]');
  var q = (nameEl.value || '').trim();
  if (!q) { nameEl.focus(); return; }
  btn.textContent = '…';
  try {
    var res = await fetch('/warehouse/price-suggest?q=' + encodeURIComponent(q));
    var d = await res.json();
    if (d.suggested != null) {
      saleEl.value = d.suggested;
      btn.title = 'eBay: ' + d.count + ' Angebote · ' + d.min + '–' + d.max + ' ' + d.currency;
    } else {
      btn.title = d.error ? ('Fehler: ' + d.error) : 'Keine Angebote gefunden';
    }
  } catch (e) { btn.title = 'Fehler beim Abruf'; }
  btn.textContent = '🔍';
}
function stOpenSplit(id, cost) {
  var f = document.getElementById('splitForm');
  if (f) {
    f.action = '/warehouse/' + id + '/split';
    var t = document.getElementById('splitTotal');
    if (t) t.value = cost || '';
  }
  stOpenModal('splitSet');
}
function stEditPart(btn) {
  var d = btn.closest('tr').querySelector('details.inline-edit');
  if (d) {
    d.open = true;
    d.scrollIntoView({ block: 'nearest' });
    var i = d.querySelector('form input[name=name]');
    if (i) i.focus();
  }
}
function stImgSquare(input) {
  var f = input.files && input.files[0];
  if (!f) return;
  var box = input.closest('.img-square');
  if (!box) return;
  var img = box.querySelector('img.is-preview');
  if (!img) { img = document.createElement('img'); img.className = 'is-preview'; box.insertBefore(img, input); }
  img.src = URL.createObjectURL(f);
  var ph = box.querySelector('.is-ph'); if (ph) ph.style.display = 'none';
}
// Receipt mode drives the whole block: exactly one input is shown, and the
// unused one is disabled so it never reaches the server. Read the select
// rather than trusting an event, so a browser-restored value stays honest.
function stRecMode() {
  var sel = document.getElementById('wpRecMode');
  if (!sel) return;
  var mode = sel.value || 'new';
  var fileWrap = document.getElementById('wpRecNew');
  var pickWrap = document.getElementById('wpRecPick');
  var rec = document.getElementById('wpReceipt');
  var exp = document.getElementById('wpExpense');
  var free = document.getElementById('wpFree');
  var pp = document.getElementById('wpPurchase');
  if (fileWrap) fileWrap.hidden = mode !== 'new';
  if (pickWrap) pickWrap.hidden = mode !== 'existing';
  if (rec) { rec.required = mode === 'new'; if (mode !== 'new') rec.value = ''; }
  if (exp) exp.disabled = mode !== 'existing';
  if (free) free.value = mode === 'free' ? '1' : '';
  // A gift has no purchase price; every other mode does.
  if (pp) { pp.disabled = mode === 'free'; if (mode === 'free') pp.value = ''; }
}
document.addEventListener('DOMContentLoaded', stRecMode);

// Install the service worker so the app can be added to a home screen. Failing
// registration is not worth surfacing — the app works exactly the same without.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  });
}

// Mobile drawer. Deliberately not persisted like the desktop collapse state —
// a menu that is still open on the next page load is in the way, not helpful.
function stToggleNav() {
  var r = document.documentElement;
  r.setAttribute('data-nav', r.getAttribute('data-nav') === 'open' ? 'closed' : 'open');
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') document.documentElement.setAttribute('data-nav', 'closed');
});
// Following a link inside the drawer should close it, otherwise it covers the
// page it just navigated to.
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.sidebar nav a').forEach(function (a) {
    a.addEventListener('click', function () {
      document.documentElement.setAttribute('data-nav', 'closed');
    });
  });
});

// A "+ New …" entry inside a dropdown reveals that dropdown's create fields,
// so picking an existing one and adding one are the same control.
function stPickNew(sel, fieldsId) {
  var box = document.getElementById(fieldsId);
  if (!box) return;
  box.hidden = sel.value !== '__new';
  if (!box.hidden) {
    var first = box.querySelector('input');
    if (first) first.focus();
  }
}

// The project head shows the facts; the edit form is on request.
function stToggleProjectEdit(btn) {
  var panel = document.getElementById('projEdit');
  if (!panel) return;
  panel.hidden = !panel.hidden;
  btn.textContent = panel.hidden ? btn.dataset.closed : btn.dataset.open;
  if (!panel.hidden) {
    panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    var first = panel.querySelector('input, select, textarea');
    if (first) first.focus();
  }
}

// ---- Work sessions: one panel for logging and for correcting ----
// The row's ✎ fills the same form that "+ Work session" opens, so there is one
// layout to learn rather than a second, inline one.
function stEditSession(btn) {
  var form = document.getElementById('wsForm');
  var panel = document.getElementById('wsPanel');
  if (!form || !panel) return;
  var d = btn.dataset;
  form.action = d.action;
  form.work_date.value = d.date || '';
  form.hours.value = d.hours || '';
  form.hourly_rate.value = d.rate || '';
  form.description.value = d.description || '';
  document.getElementById('wsSubmit').textContent = form.dataset.editLabel;
  document.getElementById('wsCancel').hidden = false;
  panel.open = true;
  panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  form.hours.focus();
}
// Back to logging a new entry. `close` also collapses the panel (Cancel);
// the summary calls it bare, because clicking it always means "new entry".
function stResetSession(close) {
  var form = document.getElementById('wsForm');
  var panel = document.getElementById('wsPanel');
  if (!form) return;
  form.action = form.dataset.newAction;
  form.reset();
  form.work_date.value = form.dataset.today;
  document.getElementById('wsSubmit').textContent = form.dataset.newLabel;
  document.getElementById('wsCancel').hidden = true;
  if (close && panel) panel.open = false;
}
// ---- Price fields: digits only, currency suffix, 2-decimal normalise ----
function stIsMoney(inp) { return /price|amount|rate|sale|purchase|total/i.test(inp.name || ''); }
function stWrapPrice(inp) {
  if (!inp || inp.dataset.priced) return;
  inp.dataset.priced = '1';
  inp.classList.add('price-input');
  var cur = window.ST_CURRENCY || '';
  if (!cur || (inp.parentNode && inp.parentNode.classList.contains('price-field'))) return;
  var span = document.createElement('span');
  span.className = 'price-field';
  span.setAttribute('data-cur', cur);
  inp.parentNode.insertBefore(span, inp);
  span.appendChild(inp);
}
function stWrapPricesIn(root) {
  root.querySelectorAll('input[inputmode="decimal"]').forEach(function (inp) {
    if (stIsMoney(inp)) stWrapPrice(inp);
  });
}
function stPriceClean(el) {
  // keep digits and a single decimal separator (either , or .)
  var v = el.value.replace(/[^0-9.,]/g, '');
  var i = v.search(/[.,]/);
  if (i !== -1) v = v.slice(0, i + 1) + v.slice(i + 1).replace(/[.,]/g, '');
  if (v !== el.value) el.value = v;
}
function stPriceNorm(el) {
  var v = el.value.trim();
  if (!v) return;                       // leave empty fields empty
  var n = parseFloat(v.replace(',', '.'));
  if (isNaN(n)) { el.value = ''; return; }
  el.value = n.toFixed(2).replace('.', ',');   // 12 → "12,00", 12.5 → "12,50"
}
document.addEventListener('input', function (e) {
  if (e.target.matches && e.target.matches('input[inputmode="decimal"]')) stPriceClean(e.target);
});
document.addEventListener('blur', function (e) {   // capture: blur doesn't bubble
  if (e.target.matches && e.target.matches('input[inputmode="decimal"]')) stPriceNorm(e.target);
}, true);

// Warehouse create: live per-unit prices from lot totals ÷ quantity
function stUnitPrices() {
  var out = document.getElementById('wpUnit');
  if (!out) return;
  var num = function (id) {
    var el = document.getElementById(id);
    return el ? (parseFloat((el.value || '').replace(',', '.')) || 0) : 0;
  };
  var q = num('wpQty') || 1, pp = num('wpPurchase'), sp = num('wpSale');
  var cur = window.ST_CURRENCY || '€';
  var f = function (v) { return v.toFixed(2).replace('.', ',') + ' ' + cur; };
  var parts = [];
  if (pp > 0) parts.push('Einkauf/Stück: ' + f(pp / q));
  if (sp > 0) parts.push('VK/Stück: ' + f(sp / q));
  out.textContent = (q > 1 && parts.length) ? parts.join('  ·  ') : '';
}

// Kanban: show/hide done tasks (persisted), and keep column counts in sync
function stKanbanCounts() {
  var board = document.querySelector('.kanban-board');
  if (!board) return;
  var hide = board.classList.contains('hide-done');
  board.querySelectorAll('.kanban-col').forEach(function (col) {
    var n = 0;
    col.querySelectorAll('.task-card').forEach(function (c) {
      if (!(hide && c.classList.contains('done'))) n++;
    });
    var badge = col.querySelector('h3 .count');
    if (badge) badge.textContent = n;
  });
}
function stToggleDone(btn) {
  var board = document.querySelector('.kanban-board');
  if (!board) return;
  var hide = board.classList.toggle('hide-done');
  btn.textContent = hide ? btn.dataset.show : btn.dataset.hide;
  btn.classList.toggle('on', hide);
  try { localStorage.setItem('st-kanban-done', hide ? 'hide' : 'show'); } catch (e) {}
  stKanbanCounts();
}
function stInitKanbanDone() {
  var board = document.querySelector('.kanban-board');
  if (!board) return;
  var pref = 'show';
  try { pref = localStorage.getItem('st-kanban-done') || 'show'; } catch (e) {}
  var hide = pref === 'hide';
  board.classList.toggle('hide-done', hide);
  var btn = document.getElementById('doneToggle');
  if (btn) { btn.textContent = hide ? btn.dataset.show : btn.dataset.hide; btn.classList.toggle('on', hide); }
  stKanbanCounts();
}
// Kanban drag & drop: move cards between buckets and persist to Vikunja
var stDragCard = null;
document.addEventListener('dragstart', function (e) {
  var card = e.target.closest ? e.target.closest('.kanban-board .task-card[draggable="true"]') : null;
  if (!card) return;
  stDragCard = card;
  card.classList.add('dragging');
  try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', card.dataset.taskId || ''); } catch (x) {}
});
document.addEventListener('dragend', function () {
  if (stDragCard) stDragCard.classList.remove('dragging');
  document.querySelectorAll('.kanban-col.drop-hover').forEach(function (c) { c.classList.remove('drop-hover'); });
  stDragCard = null;
});
document.addEventListener('dragover', function (e) {
  var col = e.target.closest ? e.target.closest('.kanban-board .kanban-col') : null;
  if (col && stDragCard) { e.preventDefault(); try { e.dataTransfer.dropEffect = 'move'; } catch (x) {} col.classList.add('drop-hover'); }
});
document.addEventListener('dragleave', function (e) {
  var col = e.target.closest ? e.target.closest('.kanban-board .kanban-col') : null;
  if (col && !col.contains(e.relatedTarget)) col.classList.remove('drop-hover');
});
document.addEventListener('drop', function (e) {
  var col = e.target.closest ? e.target.closest('.kanban-board .kanban-col') : null;
  if (!col || !stDragCard) return;
  e.preventDefault();
  col.classList.remove('drop-hover');
  var card = stDragCard; stDragCard = null;
  var board = col.closest('.kanban-board');
  var list = col.querySelector('.kanban-tasks');
  var tid = card.dataset.taskId, bid = col.dataset.bucketId, pid = board ? board.dataset.projectId : '';
  card.classList.remove('dragging');
  if (!tid || !bid || !list || card.parentNode === list) return;
  var ph = list.querySelector('p.muted'); if (ph) ph.remove();
  list.appendChild(card);
  card.classList.toggle('done', col.dataset.done === '1');
  stKanbanCounts();
  fetch('/tasks/' + encodeURIComponent(tid) + '/bucket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'project_id=' + encodeURIComponent(pid) + '&bucket_id=' + encodeURIComponent(bid)
  }).catch(function () {});
});

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
  stWrapPricesIn(document);   // currency suffix + price behaviour on all money fields
  var ep = document.querySelector('select[name="email_provider"]');
  if (ep) stEmailProvider(ep);   // collapse SMTP block if IN is the sender
  stInitKanbanDone();
})();
