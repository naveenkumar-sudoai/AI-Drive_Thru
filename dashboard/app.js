/* AI Drive-Thru dashboard — plain JS, no framework, no build step.
 *
 * Three views toggled by the top nav: Live Queue, Menu Editor, Stats.
 * Live updates arrive over the backend WebSocket (/ws) as:
 *   {"type": "new_order",     "order": {...}}
 *   {"type": "order_updated", "order": {...}}
 */

"use strict";

// --- config -----------------------------------------------------------

const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
const ACTIVE_STATUSES = ["pending", "preparing", "ready"];

const PLACEHOLDER_SVG =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">' +
      '<rect width="200" height="200" fill="#e2e8f0"/>' +
      '<circle cx="100" cy="76" r="36" fill="#94a3b8"/>' +
      '<path d="M100 124c-40 0-66 28-66 60v6h132v-6c0-32-26-60-66-60z" fill="#94a3b8"/>' +
      "</svg>"
  );

const STATUS_BADGE = {
  pending: { label: "pending", cls: "pending" },
  preparing: { label: "preparing", cls: "preparing" },
  ready: { label: "ready", cls: "ready" },
  picked_up: { label: "picked up", cls: "picked_up" },
};

// --- state ------------------------------------------------------------

let menu = [];
let orders = [];        // active queue orders (pending/preparing/ready), sorted FIFO
let stats = null;
let currentView = "queue";
let editingId = null;   // menu item currently being edited inline
let ws = null;
let statsTimer = null;
let hasConnected = false;

// --- tiny DOM helpers -------------------------------------------------

const $ = (sel) => document.querySelector(sel);

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function money(n) {
  return "$" + Number(n || 0).toFixed(2);
}

function timeAgo(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m ago";
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// --- init -------------------------------------------------------------

async function init() {
  bindNav();
  bindMenuForm();
  bindQueueActions();
  await Promise.all([loadMenu(), loadOrders(), loadStats()]);
  connectWS();
  setInterval(renderQueueTimes, 15000); // keep "time since" fresh
}

// --- nav switching ----------------------------------------------------

function bindNav() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
}

function switchView(view) {
  currentView = view;
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view)
  );
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + view)
  );
  if (view === "stats") renderStats();
  if (view === "menu") renderMenuTable();
  if (view === "queue") renderQueue();
}

// --- data loading -----------------------------------------------------

async function loadMenu() {
  const res = await fetch("/menu");
  menu = await res.json();
  renderMenuTable();
}

async function loadOrders() {
  const res = await fetch("/orders?status=" + ACTIVE_STATUSES.join(","));
  orders = await res.json();
  sortQueue();
  renderQueue();
}

async function loadStats() {
  const res = await fetch("/stats");
  stats = await res.json();
  renderStats();
}

function sortQueue() {
  orders.sort((a, b) => a.timestamp - b.timestamp); // oldest first (FIFO)
}

// --- websocket --------------------------------------------------------

function connectWS() {
  setConn("connecting");
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    setConn("ok");
    if (hasConnected) {
      // Reconnect: broadcasts may have been missed while offline, so re-sync.
      loadOrders();
      loadStats();
    }
    hasConnected = true;
  };
  ws.onclose = () => {
    setConn("bad");
    setTimeout(connectWS, 2000); // auto-reconnect
  };
  ws.onerror = () => setConn("bad");
  ws.onmessage = (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    if (msg.type === "new_order" || msg.type === "order_updated") {
      applyOrder(msg.order);
    }
  };
}

function setConn(state) {
  const el = $("#connStatus");
  const txt = $("#connText");
  el.classList.remove("ok", "bad");
  if (state === "ok") {
    el.classList.add("ok");
    txt.textContent = "live";
  } else if (state === "bad") {
    el.classList.add("bad");
    txt.textContent = "reconnecting";
  } else {
    txt.textContent = "connecting";
  }
}

// Insert/update/remove an order in the local queue and re-render if needed.
function applyOrder(order) {
  const active = ACTIVE_STATUSES.includes(order.status);
  const idx = orders.findIndex((o) => o.id === order.id);

  if (!active) {
    orders = orders.filter((o) => o.id !== order.id);
  } else if (idx >= 0) {
    orders[idx] = order;
  } else {
    orders.push(order);
  }
  sortQueue();
  if (currentView === "queue") renderQueue();
  scheduleStatsRefresh();
}

function scheduleStatsRefresh() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(loadStats, 600);
}

// --- queue rendering --------------------------------------------------

function renderQueue() {
  const list = $("#queueList");
  const empty = $("#queueEmpty");
  list.innerHTML = "";
  empty.classList.toggle("hidden", orders.length > 0);

  for (const order of orders) {
    list.appendChild(buildOrderCard(order));
  }
}

function buildOrderCard(order) {
  const card = document.createElement("div");
  card.className = "order-card";
  card.dataset.id = order.id;

  const imgSrc = order.photo_path ? "/" + order.photo_path : PLACEHOLDER_SVG;

  const itemsHtml = (order.items || [])
    .map((it) => {
      const notes = it.notes ? '<span class="notes">' + escapeHtml(it.notes) + "</span>" : "";
      return (
        '<li><span class="qty">' +
        escapeHtml(it.quantity) +
        "×</span>" +
        escapeHtml(it.name) +
        notes +
        "</li>"
      );
    })
    .join("");

  const badge = STATUS_BADGE[order.status] || { label: order.status, cls: "pending" };

  card.innerHTML =
    '<div class="order-photo"><img alt="customer" src="' + imgSrc + '"></div>' +
    '<div class="order-body">' +
    '<div class="order-head">' +
    '<span class="order-id">#' + escapeHtml(order.id) + "</span>" +
    '<span class="order-time" data-ts="' + order.timestamp + '">' + timeAgo(order.timestamp) + " · " + clock(order.timestamp) + "</span>" +
    '<span class="badge ' + badge.cls + '">' + badge.label + "</span>" +
    "</div>" +
    '<ul class="order-items">' + itemsHtml + "</ul>" +
    '<div class="order-foot">' +
    '<div class="total">' + money(order.total_price) + "</div>" +
    '<div class="actions">' + buildActions(order.status) + "</div>" +
    "</div>" +
    "</div>";

  // placeholder fallback if a stored photo is missing on disk
  const img = card.querySelector(".order-photo img");
  img.onerror = () => {
    img.onerror = null;
    img.src = PLACEHOLDER_SVG;
  };

  return card;
}

function buildActions(status) {
  if (status === "pending") {
    return '<button class="btn amber" data-action="preparing">Start Preparing</button>';
  }
  if (status === "preparing") {
    return '<button class="btn green" data-action="ready">Mark Ready</button>';
  }
  if (status === "ready") {
    return '<button class="btn primary" data-action="picked_up">Picked Up</button>';
  }
  return "";
}

function renderQueueTimes() {
  document.querySelectorAll(".order-time[data-ts]").forEach((el) => {
    const ts = Number(el.dataset.ts);
    el.textContent = timeAgo(ts) + " · " + clock(ts);
  });
}

// queue action buttons (event delegation)
function bindQueueActions() {
  $("#queueList").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const card = btn.closest(".order-card");
    if (!card) return;
    const status = btn.dataset.action;
    btn.disabled = true;
    try {
      await updateStatus(card.dataset.id, status);
    } finally {
      btn.disabled = false;
    }
  });
}

async function updateStatus(id, status) {
  const res = await fetch("/orders/" + encodeURIComponent(id), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error("status update failed");
  const updated = await res.json();
  applyOrder(updated); // WebSocket also fires; this is idempotent + gives instant feedback
}

// --- menu rendering ---------------------------------------------------

function bindMenuForm() {
  $("#addItemForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#newName").value.trim();
    const price = parseFloat($("#newPrice").value);
    const available = $("#newAvailable").checked;
    if (!name || isNaN(price)) return;

    const res = await fetch("/menu", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, price, available }),
    });
    if (res.ok) {
      $("#addItemForm").reset();
      $("#newAvailable").checked = true;
      await loadMenu();
    } else {
      const err = await res.json().catch(() => ({}));
      alert("Could not add item: " + (err.detail || res.statusText));
    }
  });
}

function renderMenuTable() {
  const tbody = $("#menuTableBody");
  tbody.innerHTML = "";

  if (!menu.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">No menu items yet — add your first item above.</td></tr>';
    return;
  }

  for (const item of menu) {
    const tr = document.createElement("tr");

    if (editingId === item.id) {
      tr.innerHTML =
        '<td><input class="input grow" id="editName" value="' + escapeHtml(item.name) + '"></td>' +
        '<td><input class="input" id="editPrice" type="number" step="0.01" min="0" value="' + item.price + '"></td>' +
        '<td></td>' +
        '<td class="right">' +
        '<button class="btn sm primary" onclick="saveEdit(' + item.id + ')">Save</button> ' +
        '<button class="btn sm" onclick="cancelEdit()">Cancel</button>' +
        "</td>";
    } else {
      tr.innerHTML =
        "<td>" + escapeHtml(item.name) + "</td>" +
        "<td>" + money(item.price) + "</td>" +
        "<td>" +
        '<label class="switch"><input type="checkbox" ' +
        (item.available ? "checked" : "") +
        ' onchange="toggleAvailable(' + item.id + ', this.checked)"><span class="slider"></span></label>' +
        "</td>" +
        '<td class="right">' +
        '<button class="btn sm" onclick="editItem(' + item.id + ')">Edit</button> ' +
        '<button class="btn sm danger" onclick="deleteItem(' + item.id + ')">Delete</button>' +
        "</td>";
    }
    tbody.appendChild(tr);
  }
}

function editItem(id) {
  editingId = id;
  renderMenuTable();
  const input = $("#editName");
  if (input) input.focus();
}

function cancelEdit() {
  editingId = null;
  renderMenuTable();
}

async function saveEdit(id) {
  const name = $("#editName").value.trim();
  const price = parseFloat($("#editPrice").value);
  if (!name || isNaN(price)) {
    alert("Name and a valid price are required.");
    return;
  }
  const res = await fetch("/menu/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, price }),
  });
  if (res.ok) {
    editingId = null;
    await loadMenu();
  } else {
    const err = await res.json().catch(() => ({}));
    alert("Could not save: " + (err.detail || res.statusText));
  }
}

async function toggleAvailable(id, available) {
  const item = menu.find((m) => m.id === id);
  if (item) item.available = available; // keep local state consistent
  await fetch("/menu/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ available }),
  });
}

async function deleteItem(id) {
  if (!confirm("Delete this menu item?")) return;
  const res = await fetch("/menu/" + id, { method: "DELETE" });
  if (res.ok) {
    await loadMenu();
  } else {
    alert("Could not delete item.");
  }
}

// --- stats rendering --------------------------------------------------

function renderStats() {
  if (!stats) return;
  $("#statOrders").textContent = stats.total_orders_today;
  $("#statRevenue").textContent = money(stats.total_revenue_today);

  renderTopItemsChart(stats.top_items_today || []);
  renderHoursChart(stats.orders_per_hour || []);

  const list = $("#topAllTime");
  list.innerHTML = "";
  const top = stats.top_items_all_time || [];
  if (!top.length) {
    list.innerHTML = '<li class="muted">No orders yet.</li>';
  } else {
    for (const it of top) {
      list.innerHTML +=
        "<li><span>" + escapeHtml(it.name) + '</span><span class="q">' + it.quantity + "</span></li>";
    }
  }
}

function renderTopItemsChart(items) {
  const canvas = $("#chartTop");
  const fallback = $("#chartTopFallback");
  try {
    fallback.classList.add("hidden");
    drawBarChart(canvas, items.map((it) => ({ label: it.name, value: it.quantity })));
  } catch (err) {
    canvas.classList.add("hidden");
    fallback.classList.remove("hidden");
    fallback.innerHTML =
      "<ul>" +
      items.map((it) => "<li><span>" + escapeHtml(it.name) + "</span><span>" + it.quantity + "</span></li>").join("") +
      "</ul>";
  }
}

function renderHoursChart(perHour) {
  const canvas = $("#chartHours");
  const fallback = $("#chartHoursFallback");
  const data = perHour.map((v, h) => ({ label: h + ":00", value: v }));
  try {
    fallback.classList.add("hidden");
    drawBarChart(canvas, data);
  } catch (err) {
    canvas.classList.add("hidden");
    fallback.classList.remove("hidden");
    fallback.innerHTML =
      "<ul>" +
      data
        .filter((d) => d.value > 0)
        .map((d) => "<li><span>" + d.label + "</span><span>" + d.value + "</span></li>")
        .join("") +
      "</ul>";
  }
}

/* Minimal canvas bar chart — no library, works offline. */
function drawBarChart(canvas, items) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth || 320;
  const cssH = canvas.height || 240;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const pad = { top: 18, right: 8, bottom: 34, left: 8 };
  const innerW = cssW - pad.left - pad.right;
  const innerH = cssH - pad.top - pad.bottom;

  const maxVal = Math.max(1, ...items.map((d) => d.value));
  const slot = innerW / Math.max(1, items.length);
  const barW = Math.min(60, slot * 0.6);

  ctx.font = "11px -apple-system, Segoe UI, Roboto, sans-serif";

  items.forEach((d, i) => {
    const x = pad.left + slot * i + (slot - barW) / 2;
    const h = (d.value / maxVal) * innerH;
    const y = pad.top + (innerH - h);

    ctx.fillStyle = "#2563eb";
    ctx.fillRect(x, y, barW, h);

    // value label
    ctx.fillStyle = "#0f172a";
    ctx.textAlign = "center";
    ctx.fillText(String(d.value), x + barW / 2, y - 5);

    // x label (truncate long item names)
    let label = String(d.label);
    if (label.length > 10) label = label.slice(0, 9) + "…";
    ctx.fillStyle = "#64748b";
    ctx.fillText(label, x + barW / 2, pad.top + innerH + 15);
  });
}

// responsive chart redraw
window.addEventListener("resize", () => {
  if (currentView === "stats" && stats) renderStats();
});

// expose menu handlers used by inline onclick attributes
window.editItem = editItem;
window.cancelEdit = cancelEdit;
window.saveEdit = saveEdit;
window.toggleAvailable = toggleAvailable;
window.deleteItem = deleteItem;

document.addEventListener("DOMContentLoaded", init);
