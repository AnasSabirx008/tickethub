// TicketHub front-end. Talks only to /api (same origin, proxied by nginx to the gateway).
"use strict";

const state = { key: sessionStorage.getItem("th_key"), user: null, events: new Map() };
const $ = (id) => document.getElementById(id);

// Build DOM nodes with textContent only (never innerHTML with data -> no XSS)
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}

async function api(path, options = {}) {
  const headers = { "content-type": "application/json" };
  if (state.key) headers["x-api-key"] = state.key;
  const res = await fetch("/api" + path, { ...options, headers });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    const err = new Error((typeof d === "string" ? d : d && d.message) || `HTTP ${res.status}`);
    err.status = res.status;
    err.requestId = res.headers.get("x-request-id");   // shown to the user -> easy to find in logs
    throw err;
  }
  return data;
}

function toast(message, error = false, requestId = null) {
  const t = el("div", { class: "toast" + (error ? " error" : "") }, message);
  if (requestId) t.append(el("span", { class: "rid" }, "request id: " + requestId));
  $("toasts").append(t);
  setTimeout(() => t.remove(), error ? 8000 : 4000);
}

const fail = (e) => toast(e.message, true, e.requestId);
const fmtDate = (s) => new Date(s).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });

// ---------- session ----------
function renderSession() {
  const box = $("session");
  box.replaceChildren();
  if (state.user) {
    box.append(el("span", {}, "Signed in as " + state.user),
               el("button", { class: "ghost", onclick: logout }, "Sign out"));
  }
  $("login").classList.toggle("hidden", !!state.user);
  $("bookings-section").classList.toggle("hidden", !state.user);
}

async function login(key) {
  state.key = key;
  try {
    state.user = (await api("/me")).user;
    sessionStorage.setItem("th_key", key);
    renderSession();
    await loadBookings();
    await loadEvents();
  } catch (e) {
    state.key = null; state.user = null;
    sessionStorage.removeItem("th_key");
    toast(e.status === 401 ? "Invalid access key" : e.message, true, e.requestId);
  }
}

function logout() {
  state.key = null; state.user = null;
  sessionStorage.removeItem("th_key");
  renderSession(); loadEvents();
}

// ---------- events ----------
function eventCard(ev) {
  const pct = ev.total ? (ev.seats_left / ev.total) * 100 : 0;
  const level = ev.seats_left === 0 ? "out" : pct < 20 ? "low" : "";
  const bar = el("div", { class: "bar " + level }, el("div", { style: `width:${pct}%` }));

  const card = el("div", { class: "card" },
    el("h3", {}, ev.name),
    el("div", { class: "muted" }, fmtDate(ev.date)),
    bar,
    el("div", { class: "small muted" }, `${ev.seats_left} / ${ev.total} seats left`));

  if (!state.user) {
    card.append(el("div", { class: "small muted" }, "Sign in to book"));
  } else if (ev.seats_left === 0) {
    card.append(el("button", { disabled: "" }, "Sold out"));
  } else {
    const qty = el("select", {});
    for (let i = 1; i <= Math.min(10, ev.seats_left); i++) qty.append(el("option", { value: i }, i));
    const btn = el("button", {}, "Book");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api("/bookings", { method: "POST", body: JSON.stringify({ event_id: ev.id, quantity: Number(qty.value) }) });
        toast(`Booked ${qty.value} seat(s) for ${ev.name}`);
        await Promise.all([loadEvents(), loadBookings()]);
      } catch (e) { fail(e); await loadBookings(); }
      finally { btn.disabled = false; }
    });
    card.append(el("div", { class: "row" }, qty, btn));
  }
  return card;
}

async function loadEvents() {
  try {
    const events = await api("/events");
    state.events = new Map(events.map((e) => [e.id, e]));
    $("events").replaceChildren(...events.map(eventCard));
    $("refreshed").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("refreshed").textContent = "events unavailable (" + e.message + ")";
  }
}

// ---------- bookings ----------
async function loadBookings() {
  if (!state.user) return;
  try {
    const rows = await api("/bookings");
    $("no-bookings").classList.toggle("hidden", rows.length > 0);
    $("bookings").replaceChildren(...rows.map((b) => {
      const ev = state.events.get(b.event_id);
      const action = el("td", {});
      if (b.status === "CONFIRMED") {
        action.append(el("button", { class: "danger", onclick: () => cancel(b) }, "Cancel"));
      }
      return el("tr", {},
        el("td", {}, ev ? ev.name : b.event_id),
        el("td", {}, b.quantity),
        el("td", {}, el("span", { class: "badge " + b.status }, b.status)),
        el("td", { class: "muted" }, new Date(b.created_at).toLocaleString()),
        action);
    }));
  } catch (e) { fail(e); }
}

async function cancel(b) {
  try {
    await api(`/bookings/${encodeURIComponent(b.id)}/cancel`, { method: "POST" });
    toast("Booking cancelled");
    await Promise.all([loadEvents(), loadBookings()]);
  } catch (e) { fail(e); }
}

// ---------- start ----------
$("login-form").addEventListener("submit", (e) => { e.preventDefault(); login($("key").value.trim()); });

(async () => {
  renderSession();
  if (state.key) await login(state.key); else await loadEvents();
  // live seat counts; paused when the tab is hidden
  setInterval(() => { if (!document.hidden) loadEvents(); }, 5000);
})();
