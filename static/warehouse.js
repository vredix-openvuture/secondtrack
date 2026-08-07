// Warehouse-specific front-end: category field editor + dynamic part attributes.
// Loaded only on warehouse / category pages (functions are global).

// ---------- Category field-schema editor (Settings › Categories) ----------

const ST_CF_TYPES = [
  ["text", "Text"],
  ["number", "Number"],
  ["select", "Select"],
  ["bool", "Yes/No"],
  ["date", "Date"],
];

function stCfRow(f) {
  f = f || {};
  const opt = ST_CF_TYPES.map(
    (t) => '<option value="' + t[0] + '"' + (f.type === t[0] ? " selected" : "") + ">" + t[1] + "</option>"
  ).join("");
  const wrap = document.createElement("div");
  wrap.className = "cat-field-row";
  wrap.innerHTML =
    '<input class="cf-label" placeholder="Label (e.g. Platform)" value="' + stEsc(f.label) + '">' +
    '<select class="cf-type" onchange="stCfTypeChange(this)">' + opt + "</select>" +
    '<input class="cf-options" placeholder="Options, comma-separated" value="' + stEsc((f.options || []).join(", ")) + '">' +
    '<input class="cf-unit" placeholder="Unit" value="' + stEsc(f.unit) + '">' +
    '<label class="chk-pill cf-reqpill" title="Required"><input type="checkbox" class="cf-required"' + (f.required ? " checked" : "") + '><span class="dot"></span>req</label>' +
    '<button type="button" class="act-btn danger" title="Remove" onclick="this.closest(\'.cat-field-row\').remove()">✕</button>' +
    '<input type="hidden" class="cf-key" value="' + stEsc(f.key) + '">';
  return wrap;
}

function stCfTypeChange(sel) {
  const row = sel.closest(".cat-field-row");
  const opts = row.querySelector(".cf-options");
  if (opts) opts.style.display = sel.value === "select" ? "" : "none";
}

function stCatAddField(btn) {
  const editor = btn.closest(".cat-editor").querySelector(".cat-fields");
  const row = stCfRow({});
  editor.appendChild(row);
  stCfTypeChange(row.querySelector(".cf-type"));
  row.querySelector(".cf-label").focus();
}

function stCatInit() {
  document.querySelectorAll(".cat-fields").forEach((box) => {
    let defs = [];
    try { defs = JSON.parse(box.dataset.fields || "[]"); } catch (e) {}
    defs.forEach((f) => {
      const row = stCfRow(f);
      box.appendChild(row);
      stCfTypeChange(row.querySelector(".cf-type"));
    });
  });
}

function stCatSerialize(form) {
  const box = form.querySelector(".cat-fields");
  const hidden = form.querySelector('input[name="fields_json"]');
  if (!box || !hidden) return true;
  const out = [];
  box.querySelectorAll(".cat-field-row").forEach((row) => {
    const label = row.querySelector(".cf-label").value.trim();
    if (!label) return;
    const type = row.querySelector(".cf-type").value;
    const optsRaw = row.querySelector(".cf-options").value;
    out.push({
      key: row.querySelector(".cf-key").value.trim(),
      label: label,
      type: type,
      options: type === "select" ? optsRaw.split(",").map((s) => s.trim()).filter(Boolean) : [],
      unit: row.querySelector(".cf-unit").value.trim(),
      required: row.querySelector(".cf-required").checked,
    });
  });
  hidden.value = JSON.stringify(out);
  return true;
}

function stEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Show a part/set's current image inside an edit modal's image square.
function stShowModalImage(scope, url) {
  const sq = scope.querySelector(".img-square");
  if (!sq) return;
  sq.querySelectorAll("img.is-preview").forEach((i) => i.remove());
  const ph = sq.querySelector(".is-ph");
  if (url) {
    const img = document.createElement("img");
    img.className = "is-preview";
    img.src = url;
    sq.insertBefore(img, sq.querySelector("input"));
    if (ph) ph.style.display = "none";
  } else if (ph) {
    ph.style.display = "";
  }
}

// ---------- Dynamic part attributes (warehouse create/edit form) ----------

// Fetch a category's field schema and render inputs into `container`, keeping
// any current values passed in `preset` (key → value).
async function stLoadAttrFields(container, catId, preset) {
  if (!container) return;
  preset = preset || {};
  if (!catId) { container.innerHTML = ""; return; }
  container.innerHTML = '<p class="muted small">…</p>';
  let fields = [];
  try {
    const res = await fetch("/settings/categories/" + catId + "/fields");
    fields = await res.json();
  } catch (e) { container.innerHTML = ""; return; }
  if (!fields.length) { container.innerHTML = ""; return; }
  container.innerHTML = "";
  fields.forEach((f) => {
    const name = "attr_" + f.key;
    const val = preset[f.key];
    const lbl = document.createElement("label");
    lbl.className = "attr-field";
    // Label text + unit live in ONE element so the flex column never splits
    // them onto two lines.
    const head = '<span class="lbl">' + stEsc(f.label) +
      (f.unit ? ' <span class="unit">(' + stEsc(f.unit) + ")</span>" : "") + "</span>";
    let inner;
    if (f.type === "select") {
      const opts = ['<option value="">—</option>'].concat(
        (f.options || []).map((o) => '<option value="' + stEsc(o) + '"' + (String(val) === o ? " selected" : "") + ">" + stEsc(o) + "</option>")
      ).join("");
      inner = head + '<select name="' + name + '">' + opts + "</select>";
    } else if (f.type === "bool") {
      inner = '<span class="chk"><input type="checkbox" name="' + name + '" value="1"' + (val ? " checked" : "") + "> " + stEsc(f.label) + "</span>";
    } else if (f.type === "date") {
      inner = head + '<input type="date" name="' + name + '" value="' + stEsc(val) + '">';
    } else if (f.type === "number") {
      inner = head + '<input name="' + name + '" inputmode="decimal" value="' + stEsc(val) + '">';
    } else {
      inner = head + '<input name="' + name + '" value="' + stEsc(val) + '">';
    }
    lbl.innerHTML = inner;
    container.appendChild(lbl);
  });
}

// ---------- Row (⋯) overflow menu ----------
var stRM = { id: null, code: "", cost: 0 };

function stRowMenu(ev, btn, id, code, cost) {
  ev.stopPropagation();
  const menu = document.getElementById("rowMenu");
  if (!menu) return;
  // Toggle off if the same button is clicked again.
  if (menu.classList.contains("open") && stRM.id === id) { stCloseRowMenu(); return; }
  stRM = { id: id, code: code, cost: cost };

  const install = document.getElementById("rmInstall");
  if (install) install.selectedIndex = 0;
  const edit = document.getElementById("rmEdit");
  if (edit) edit.onclick = () => { stCloseRowMenu(); stEditPartModal(id); };
  const label = document.getElementById("rmLabel");
  if (label) label.href = "/label/" + code;
  const split = document.getElementById("rmSplit");
  if (split) split.onclick = () => { stCloseRowMenu(); stOpenSplit(id, cost); };
  const del = document.getElementById("rmDelete");
  if (del) del.onclick = () => stFormSubmit("/warehouse/" + id + "/delete", null, null, "Delete part?");

  // Position (fixed) near the button, clamped to the viewport.
  menu.classList.add("open");
  const r = btn.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = r.right - mw;
  let top = r.bottom + 6;
  if (left < 8) left = 8;
  if (top + mh > window.innerHeight - 8) top = r.top - mh - 6;
  menu.style.left = Math.max(8, left) + "px";
  menu.style.top = Math.max(8, top) + "px";
}

function stCloseRowMenu() {
  const menu = document.getElementById("rowMenu");
  if (menu) menu.classList.remove("open");
}

// ⋯ menu for sets / wip / finished — mirrors the parts kebab exactly.
var stSM = { id: null };
function stSetMenu(ev, btn, id, code, kind) {
  ev.stopPropagation();
  const menu = document.getElementById("setMenu");
  if (!menu) return;
  if (menu.classList.contains("open") && stSM.id === id) { stCloseSetMenu(); return; }
  stSM = { id: id };

  const edit = document.getElementById("smEdit");
  if (edit) edit.onclick = () => { stCloseSetMenu(); (kind === "lot" ? stEditLotModal : stEditSetModal)(id); };
  const label = document.getElementById("smLabel");
  if (label) label.href = "/label/" + code;
  const finish = document.getElementById("smFinish");
  if (finish) {
    finish.style.display = (kind === "wip") ? "" : "none";
    finish.onclick = () => stFormSubmit("/warehouse/set/" + id + "/finish", null, null, window.ST_FINISH_CONFIRM);
  }
  const del = document.getElementById("smDelete");
  if (del) del.onclick = () => stFormSubmit("/warehouse/set/" + id + "/delete", null, null,
    kind === "wip" ? window.ST_DEL_WIP : window.ST_DEL_SET);

  menu.classList.add("open");
  const r = btn.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = r.right - mw;
  let top = r.bottom + 6;
  if (left < 8) left = 8;
  if (top + mh > window.innerHeight - 8) top = r.top - mh - 6;
  menu.style.left = Math.max(8, left) + "px";
  menu.style.top = Math.max(8, top) + "px";
}

function stCloseSetMenu() {
  const menu = document.getElementById("setMenu");
  if (menu) menu.classList.remove("open");
}

function stMenuInstall(projectId) {
  if (!projectId) return;
  stInstall(stRM.id, projectId);
}

function stInstall(id, projectId) {
  if (!projectId) return;
  stFormSubmit("/warehouse/" + id + "/install", "project_id", projectId, null);
}

// Build + submit a throwaway POST form (optionally one field, optional confirm).
function stFormSubmit(action, field, value, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  const f = document.createElement("form");
  f.method = "post";
  f.action = action;
  if (field) {
    const i = document.createElement("input");
    i.type = "hidden"; i.name = field; i.value = value;
    f.appendChild(i);
  }
  document.body.appendChild(f);
  f.submit();
}

// Live filter the stock table by name.
function stFilterRows(input) {
  const q = (input.value || "").trim().toLowerCase();
  // Search now lives in the top panel; filter the one rendered view section.
  const scope = input.closest(".wh-section") || document.querySelector(".wh-section") || document;
  scope.querySelectorAll("[data-name]").forEach((el) => {
    const name = el.getAttribute("data-name") || "";
    el.style.display = (!q || name.indexOf(q) !== -1) ? "" : "none";
  });
}

document.addEventListener("click", (e) => {
  const menu = document.getElementById("rowMenu");
  if (menu && menu.classList.contains("open") && !menu.contains(e.target)) stCloseRowMenu();
  const sm = document.getElementById("setMenu");
  if (sm && sm.classList.contains("open") && !sm.contains(e.target)) stCloseSetMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { stCloseRowMenu(); stCloseSetMenu(); } });
window.addEventListener("scroll", () => { stCloseRowMenu(); stCloseSetMenu(); }, true);

// Open the edit modal for a part, populated from its JSON (mirrors create).
async function stEditPartModal(id) {
  const modal = document.getElementById("editPart");
  const form = document.getElementById("editPartForm");
  if (!modal || !form) return;
  form.reset();
  form.action = "/warehouse/" + id + "/update";
  let d = {};
  try {
    const r = await fetch("/warehouse/" + id + "/json");
    if (!r.ok) return;
    d = await r.json();
  } catch (e) { return; }
  const set = (name, val) => {
    const el = form.querySelector('[name="' + name + '"]');
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!val && val !== "0" && val !== "false";
    else el.value = (val === null || val === undefined ? "" : val);
  };
  ["name", "purchase_price", "sale_price", "quantity", "notes", "condition",
   "category_id", "supplier_id", "location_id"].forEach((k) => set(k, d[k]));
  // Global optional fields (opt_<key>) from the part's extra blob.
  const extra = d.extra || {};
  Object.keys(extra).forEach((k) => set("opt_" + k, extra[k]));
  const optToggle = form.querySelector(".opt-toggle");
  if (optToggle) optToggle.open = Object.keys(extra).length > 0;
  stLoadAttrFields(form.querySelector(".attr-fields"), d.category_id, d.attributes || {});
  const code = document.getElementById("epCode");
  if (code) code.textContent = d.code || "";
  const lbl = document.getElementById("epLabel");
  if (lbl) lbl.href = "/label/" + (d.code || "");
  const sp = document.getElementById("epSplit");
  if (sp) sp.onclick = () => { stCloseModal(modal); stOpenSplit(id, d.purchase_price || 0); };
  stShowModalImage(modal, d.image);
  modal.classList.add("open");
  const nm = form.querySelector('[name="name"]');
  if (nm) nm.focus();
}

// Open the big edit modal for a set / finished good (mirrors the part editor).
async function stEditSetModal(id) {
  const modal = document.getElementById("editSet");
  const form = document.getElementById("editSetForm");
  if (!modal || !form) return;
  form.reset();
  form.action = "/warehouse/set/" + id + "/update";
  let d = {};
  try {
    const r = await fetch("/warehouse/set/" + id + "/json");
    if (!r.ok) return;
    d = await r.json();
  } catch (e) { return; }
  const set = (name, val) => {
    const el = form.querySelector('[name="' + name + '"]');
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!val;
    else el.value = (val === null || val === undefined ? "" : val);
  };
  ["name", "sale_price", "location_id", "notes"].forEach((k) => set(k, d[k]));
  set("sellable", d.sellable);
  stRenderSetComponents(d);
  const availPanel = document.getElementById("esAvailPanel");
  if (availPanel) availPanel.hidden = true;   // start closed; opens on "Add part"
  // WIP vs finished chrome.
  const isWip = d.status === "wip";
  const title = document.getElementById("esTitle");
  if (title) title.textContent = isWip ? (window.ST_EDIT_WIP || "Edit WIP") : (window.ST_EDIT_FINISHED || "Edit finished good");
  const wipBar = document.getElementById("esWipBar");
  if (wipBar) wipBar.hidden = !isWip;
  const finBtn = document.getElementById("esFinishBtn");
  if (finBtn) finBtn.onclick = () => stFinishWip(id);
  const warn = document.getElementById("esFinWarn");
  if (warn) warn.hidden = isWip;
  const code = document.getElementById("esCode");
  if (code) code.textContent = d.code || "";
  const lbl = document.getElementById("esLabel");
  if (lbl) lbl.href = "/label/" + (d.code || "");
  stShowModalImage(modal, d.image);
  modal.classList.add("open");
  const nm = form.querySelector('[name="name"]');
  if (nm) nm.focus();
}

// ---- Finished-good component booking ----
var stSet = { id: null };

function stMoney(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return "—";
  return n.toFixed(2).replace(".", ",") + " " + (window.ST_CURRENCY || "€");
}

function stFinishWip(id) {
  if (!confirm(window.ST_FINISH_CONFIRM || "Move to finished? It gets a finished storage number.")) return;
  stFormSubmit("/warehouse/set/" + id + "/finish", null, null, null);
}

function stRenderSetComponents(d) {
  stSet = { id: d.id, status: d.status };
  const list = d.members || [];
  const material = list.reduce((s, m) => s + (Number(m.sale) || 0) * (Number(m.qty) || 1), 0);

  const cost = document.getElementById("esCost");
  if (cost) cost.textContent = list.length ? (list.length + " " + (window.ST_PARTS_LABEL || "parts")) : "";

  const hint = document.getElementById("esCostHint");
  if (hint) hint.textContent = list.length ? ((window.ST_MATERIAL_LABEL || "Material value") + " " + stMoney(material)) : "";

  const members = document.getElementById("esMembers");
  if (members) {
    if (!list.length) {
      members.innerHTML = '<p class="muted small comp-empty">' +
        (window.ST_NO_PARTS_LONG || "No parts booked yet — book components below.") + "</p>";
    } else {
      const rows = list.map((m) => {
        const q = Number(m.qty) || 1;
        const nm = stEsc(m.name) + (q > 1 ? ' <span class="muted">× ' + q + "</span>" : "");
        return '<tr><td class="comp-nm">' + nm + "</td>" +
          '<td class="r">' + stMoney((Number(m.sale) || 0) * q) + "</td>" +
          '<td class="act"><button type="button" class="act-btn danger" title="Remove" onclick="stSetRemovePart(' + m.id + ')"><svg class="ic"><use href="#ic-x"/></svg></button></td></tr>';
      }).join("");
      const total = '<tr class="sum"><td>' + (window.ST_MATERIAL_LABEL || "Material value") + "</td>" +
        '<td class="r">' + stMoney(material) + "</td><td></td></tr>";
      members.innerHTML =
        '<table class="comp-table"><thead><tr>' +
        "<th>" + (window.ST_PART_LABEL || "Part") + "</th>" +
        '<th class="r">' + (window.ST_VALUE_LABEL || "Value") + "</th><th></th></tr></thead>" +
        "<tbody>" + rows + total + "</tbody></table>";
    }
  }

  const avail = document.getElementById("esAvail");
  if (avail) {
    const items = d.available || [];
    avail.innerHTML = items.length ? items.map((a) => {
      const q = Number(a.qty) || 1;
      const qc = q > 1
        ? '<input class="r av-qty" inputmode="numeric" value="1" min="1" max="' + q + '" title="' + (window.ST_QTY || "Quantity") + '">'
        : "";
      return '<div class="avail-item">' +
        '<span class="av-nm">' + stEsc(a.name) + (q > 1 ? ' <span class="muted">× ' + q + "</span>" : "") + "</span>" +
        qc +
        '<button type="button" class="act-btn" title="' + (window.ST_BOOK || "Book") + '" onclick="stBookAvail(this,' + a.id + ')"><svg class="ic"><use href="#ic-arrow"/></svg></button>' +
        "</div>";
    }).join("") : '<p class="muted small comp-empty">' + (window.ST_NO_AVAIL || "No loose parts available.") + "</p>";
  }
}

function stToggleAvail() {
  const p = document.getElementById("esAvailPanel");
  if (p) p.hidden = !p.hidden;
}
function stBookAvail(btn, partId) {
  if (!stSetFinishedGuard()) return;
  const row = btn.closest(".avail-item");
  const q = row ? row.querySelector(".av-qty") : null;
  const qty = q ? (parseInt(q.value, 10) || 1) : 1;
  stSetPost("add-part", { part_id: partId, qty: qty });
}

async function stSetPost(action, fields) {
  if (!stSet.id) return;
  const body = Object.keys(fields).map((k) => k + "=" + encodeURIComponent(fields[k])).join("&");
  try {
    const r = await fetch("/warehouse/set/" + stSet.id + "/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    });
    if (r.ok) stRenderSetComponents(await r.json());
  } catch (e) {}
}

function stSetFinishedGuard() {
  return stSet.status !== "finished" ||
    confirm(window.ST_FIN_WARN || "This item is already finished. Change its parts anyway?");
}
function stSetRemovePart(partId) {
  if (!stSetFinishedGuard()) return;
  stSetPost("remove-part", { part_id: partId });
}

// ---- New set (purchase lot) ----
function stNewSetAddRow() {
  const box = document.getElementById("newSetParts");
  if (!box) return;
  const row = document.createElement("div");
  row.className = "set-member";
  row.innerHTML =
    '<input name="part_name" placeholder="' + (window.ST_PARTNAME || "Part name") + '">' +
    '<input name="part_sale" class="r" inputmode="decimal" placeholder="' + (window.ST_SALE || "Sale") + '" style="max-width:130px">' +
    '<button type="button" class="act-btn danger" onclick="this.closest(\'.set-member\').remove()"><svg class="ic"><use href="#ic-x"/></svg></button>';
  box.appendChild(row);
  row.querySelector("input").focus();
}
function stToggleSetFree(cb) {
  const wrap = document.getElementById("nsReceiptWrap");
  const rec = document.getElementById("nsReceipt");
  if (cb.checked) { if (rec) { rec.required = false; rec.value = ""; } if (wrap) wrap.style.display = "none"; }
  else { if (rec) rec.required = true; if (wrap) wrap.style.display = ""; }
}

// ---- Edit set (purchase lot): editable member parts ----
var stLot = { id: null };
async function stEditLotModal(id) {
  const modal = document.getElementById("editLot"), form = document.getElementById("editLotForm");
  if (!modal || !form) return;
  form.reset();
  form.action = "/warehouse/set/" + id + "/update-lot";
  let d = {};
  try { const r = await fetch("/warehouse/set/" + id + "/json"); if (!r.ok) return; d = await r.json(); } catch (e) { return; }
  const set = (n, v) => { const el = form.querySelector('[name="' + n + '"]'); if (el) el.value = (v == null ? "" : v); };
  ["name", "purchase_price", "location_id", "notes"].forEach((k) => set(k, d[k]));
  stRenderLotMembers(d);
  const code = document.getElementById("elCode"); if (code) code.textContent = d.code || "";
  const lbl = document.getElementById("elLabel"); if (lbl) lbl.href = "/label/" + (d.code || "");
  stShowModalImage(modal, d.image);
  modal.classList.add("open");
  const nm = form.querySelector('[name="name"]'); if (nm) nm.focus();
}
function stRenderLotMembers(d) {
  stLot = { id: d.id };
  const list = d.members || [];
  const vk = document.getElementById("elVk");
  if (vk) vk.textContent = list.length ? ((window.ST_VK_TOTAL || "Sale total") + " " + stMoney(d.vk_total)) : "";
  const box = document.getElementById("elMembers");
  if (box) {
    box.innerHTML = list.length ? list.map((m) =>
      '<div class="set-member">' +
      '<input value="' + stEsc(m.name) + '" data-f="name" onchange="stLotSaveMember(' + m.id + ', this)">' +
      '<input class="r" inputmode="decimal" value="' + (m.sale == null ? "" : m.sale) + '" data-f="sale" onchange="stLotSaveMember(' + m.id + ', this)" style="max-width:130px">' +
      '<button type="button" class="act-btn danger" onclick="stLotRemoveMember(' + m.id + ')"><svg class="ic"><use href="#ic-x"/></svg></button>' +
      "</div>"
    ).join("") : '<p class="muted small comp-empty">' + (window.ST_NO_PARTS_LONG || "No parts yet.") + "</p>";
  }
}
async function stLotPost(path, fields) {
  if (!stLot.id) return;
  const body = Object.keys(fields).map((k) => k + "=" + encodeURIComponent(fields[k])).join("&");
  try {
    const r = await fetch("/warehouse/set/" + stLot.id + "/" + path, {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body,
    });
    if (r.ok) stRenderLotMembers(await r.json());
  } catch (e) {}
}
function stLotSaveMember(pid, el) {
  const row = el.closest(".set-member");
  stLotPost("member/" + pid + "/save", {
    name: row.querySelector('[data-f="name"]').value,
    sale: row.querySelector('[data-f="sale"]').value,
  });
}
function stLotRemoveMember(pid) { stLotPost("member/" + pid + "/remove", {}); }
function stLotAddMember() {
  const nm = document.getElementById("elAddName"), sl = document.getElementById("elAddSale");
  const name = nm ? nm.value : "";
  if (!name.trim()) { if (nm) nm.focus(); return; }
  stLotPost("member/add", { name: name, sale: sl ? sl.value : "" }).then(() => {
    if (nm) nm.value = ""; if (sl) sl.value = ""; if (nm) nm.focus();
  });
}

// Called when the category <select> changes in a part form.
function stCatChange(sel) {
  const form = sel.closest("form") || document;
  const box = form.querySelector(".attr-fields");
  let preset = {};
  if (box && box.dataset.preset) {
    try { preset = JSON.parse(box.dataset.preset); } catch (e) {}
  }
  stLoadAttrFields(box, sel.value, preset);
}

// Lazily load an inline-edit row's attribute inputs the first time it opens
// (avoids a fetch per part on page load).
function stAttrLazy(box) {
  if (!box || box.dataset.loaded) return;
  box.dataset.loaded = "1";
  let preset = {};
  try { preset = JSON.parse(box.dataset.preset || "{}"); } catch (e) {}
  if (box.dataset.cat) stLoadAttrFields(box, box.dataset.cat, preset);
}

document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.tagName === "DETAILS" && d.open) {
    const box = d.querySelector(".attr-fields[data-cat]");
    if (box) stAttrLazy(box);
  }
}, true);

// Scan focus: /warehouse?focus=<code> → open + highlight the matching row.
function stScanFocus() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("focus");
  if (!code) return;
  const el = document.getElementById("row-" + code) || document.getElementById("card-" + code);
  if (!el) return;
  el.classList.add("scan-focus");
  const det = el.querySelector("details.inline-edit");
  if (det) { det.open = true; stAttrLazy(det.querySelector(".attr-fields[data-cat]")); }
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(() => el.classList.remove("scan-focus"), 2600);
}

(function () {
  stCatInit();
  stScanFocus();
})();
