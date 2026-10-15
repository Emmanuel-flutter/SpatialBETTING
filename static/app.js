"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const SHOTS = new Set([13, 14, 15, 16]);
const PITCH_DOTS = 60;
const PASS_TRAILS = 12;
const DEFAULT_KITS = { home: "#0f33d5", away: "#d40000" };

const state = { events: new Map(), summary: null, teams: null, filter: "all", clockOffset: 0, shownClock: {} };
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function icon(name) {
  return $(`icon-${name}`).content.firstElementChild.cloneNode(true);
}

const clamp = (v) => Math.min(100, Math.max(0, v));
// The data always has the home team attacking right. When the source knows the real ends (the demo),
// the view mirrors in the periods where the home team attacks left, so the teams swap ends at half time.
let flipped = false;
const view = (v) => (flipped ? 100 - clamp(v) : clamp(v));
const px = (x) => view(x) * 1.05;
const py = (y) => (100 - view(y)) * 0.68; // Opta y grows toward the attacker's left (top of screen).

function updateOrientation() {
  const s = state.summary;
  const left = s?.orientation?.homeAttacksLeft || [];
  const next = left.includes(s?.status.periodId ?? 1); // before kick-off, the first-half ends
  if (next === flipped) return false;
  flipped = next;
  layoutBoard();
  return true;
}
const pad = (n) => String(n).padStart(2, "0");
const teamName = (side) => state.teams?.[side]?.name || (side === "home" ? "Home" : "Away");

function rgb(hex) {
  return /^#[0-9a-f]{6}$/i.test(hex || "") ? [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) : null;
}

function teamColours() {
  const kits = { home: rgb(state.teams?.home?.kit), away: rgb(state.teams?.away?.kit) };
  const light = (c) => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255 > 0.85; // Vanishes on white.
  if (!kits.home || !kits.away || light(kits.home) || light(kits.away)) return DEFAULT_KITS;
  const distance = Math.hypot(...kits.home.map((v, i) => v - kits.away[i]));
  return distance < 120 ? DEFAULT_KITS : { home: state.teams.home.kit, away: state.teams.away.kit };
}

/* ---------- state ---------- */

function upsert(event) {
  const current = state.events.get(event.id);
  if (current && current.seqId > event.seqId) return;
  state.events.set(event.id, event);
}

function applySnapshot(data) {
  state.events.clear();
  // A snapshot replaces everything. After a server restart or feed rebuild its seq starts again from 0.
  state.summary = null;
  state.shownClock = {};
  for (const e of [...data.timeline, ...data.pitch]) upsert(e);
  applySummary(data);
}

function applySummary(summary) {
  // Updates are built in the feed thread, so one can arrive after a newer snapshot.
  if (state.summary && summary.seq < state.summary.seq) return;
  state.summary = summary;
  state.teams = summary.teams;
  state.clockOffset = summary.serverTime - Date.now() / 1000;
}

function liveEvents() {
  return [...state.events.values()]
    .filter((e) => !e.deleted)
    .sort((a, b) => (a.periodId || 0) - (b.periodId || 0) || a.min - b.min || a.sec - b.sec || a.id - b.id);
}

/* ---------- render ---------- */

function render() {
  if (!state.summary) return;
  updateOrientation();
  renderHeader();
  renderPitch();
  renderTimeline();
  renderStats();
  renderLineups();
}

function renderHeader() {
  const { score, status, startTime } = state.summary;
  const colours = teamColours();
  for (const side of ["home", "away"]) {
    document.documentElement.style.setProperty(`--${side}`, colours[side]);
    $(`name-${side}`).textContent = teamName(side);
    $(`stat-${side}`).textContent = teamName(side);
    // The legend names the team attacking right first.
    $(`dir-${side}`).textContent = teamName(flipped ? (side === "home" ? "away" : "home") : side);
  }
  document.title = `${teamName("home")} ${score.home}-${score.away} ${teamName("away")} · Sky Bet`;
  const box = (v) => el("b", {}, String(v));
  const sep = el("i", { "aria-hidden": "true" }, ":");
  $("score").replaceChildren(...(status.phase === "pre" ? [box("–"), sep, box("–")] : [box(score.home), sep, box(score.away)]));
  $("score").setAttribute("aria-label", status.phase === "pre" ? "No score yet" : `${score.home} ${score.away}`);
  $("status").textContent = status.label;
  $("status").dataset.phase = status.phase;
  const ko = startTime ? new Date(startTime) : null;
  $("kickoff").textContent = ko ? `Kick-off ${ko.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "";
  renderClock();
}

function renderClock() {
  const s = state.summary;
  if (!s || !s.clock || s.status.phase === "pre") { $("clock").textContent = ""; return; }
  if (s.status.phase !== "live") { $("clock").textContent = s.status.label; return; }
  let seconds = s.clock.min * 60 + s.clock.sec;
  if (s.clock.periodId === s.status.periodId) {
    // A demo can run faster than real time (or pause); the server sends the rate. With no new event
    // the clock holds after holdSec, so a feed that stops mid-half does not run on.
    const run = Math.max(0, Date.now() / 1000 + state.clockOffset - s.clock.seenAt) * (s.clock.rate ?? 1);
    seconds += Math.min(run, s.clock.holdSec ?? Infinity);
  }
  seconds = Math.floor(seconds);
  // A new event re-anchors on its own match time, which can trail the running clock. Never step back.
  if (state.shownClock.periodId === s.status.periodId) seconds = Math.max(seconds, state.shownClock.seconds);
  state.shownClock = { periodId: s.status.periodId, seconds };
  $("clock").textContent = `${Math.floor(seconds / 60)}:${pad(seconds % 60)}`;
}

function matchesFilter(e) {
  if (state.filter === "off") return false;
  if (state.filter === "shots") return SHOTS.has(e.typeId);
  if (state.filter === "passes") return e.typeId === 1;
  return true;
}

function renderPitch() {
  const dotsLayer = $("layer-dots");
  const linesLayer = $("layer-lines");
  const latestLayer = $("layer-latest"); // above the zones, so a price pill never hides the newest event
  dotsLayer.replaceChildren();
  linesLayer.replaceChildren();
  latestLayer.replaceChildren();

  const positioned = liveEvents().filter((e) => e.x != null && e.team);
  const onPitch = positioned.filter(matchesFilter);
  const shown = state.filter === "shots" ? onPitch : onPitch.slice(-PITCH_DOTS);

  shown.filter((e) => e.typeId === 1 && e.endX != null).slice(-PASS_TRAILS).forEach((e) => {
    linesLayer.append(svg("line", {
      class: "trail", x1: px(e.x), y1: py(e.y), x2: px(e.endX), y2: py(e.endY),
      stroke: `var(--${e.team})`, "stroke-dasharray": e.outcome === 1 ? "" : "0.8 0.6",
    }));
  });

  shown.forEach((e, i) => {
    const latest = i === shown.length - 1;
    const cx = px(e.x), cy = py(e.y);
    const group = svg("g");
    if (e.typeId === 16) {
      group.append(svg("circle", { class: "goal-mark", cx, cy, r: 1.6 }));
    } else {
      group.append(svg("circle", { class: `dot${latest ? " latest" : ""}`, cx, cy, r: SHOTS.has(e.typeId) ? 1.3 : 0.9, fill: `var(--${e.team})` }));
    }
    if (latest) group.append(svg("circle", { class: "halo", cx, cy, r: 1.4 }));
    (latest ? latestLayer : dotsLayer).append(group);
  });

  // The text names the newest event even when the filter hides the dots.
  const last = (state.filter === "off" ? positioned : onPitch).at(-1);
  const latestText = $("latest");
  if (last) {
    latestText.replaceChildren(
      el("strong", {}, `${pad(last.min)}:${pad(last.sec)} ${last.name}`),
      document.createTextNode(` · ${last.player || teamName(last.team)}`),
    );
  } else {
    latestText.textContent = state.summary.status.phase === "pre" ? "Waiting for kick-off." : "No events for this filter yet.";
  }
}

function describe(e) {
  const who = e.player ? `${e.player}, ${teamName(e.team)}` : teamName(e.team);
  switch (e.typeId) {
    case 16: return { icon: "goal", cls: "is-goal", text: "Goal", sub: [who, e.ownGoal && "Own goal", e.penalty && "Penalty", e.goalConfirmed && "Confirmed"].filter(Boolean).join(" · ") };
    case 38: return { icon: "goal", cls: "", text: "Possible goal", sub: who };
    case 13: case 14: case 15: return { icon: "shot", cls: "", text: e.name, sub: who };
    case 17: {
      const label = { yellow: "Yellow card", second_yellow: "Second yellow card", red: "Red card" }[e.card] || "Card";
      return { icon: "card", cls: e.card === "yellow" ? "is-yellow" : "is-red", text: label, sub: who };
    }
    case 18: return { icon: "sub", cls: "", text: "Substitution off", sub: who };
    case 19: return { icon: "sub", cls: "", text: "Substitution on", sub: who };
    case 27: return e.possibleVar ? { icon: "whistle", cls: "", text: "Possible VAR check", sub: "" } : null;
    default: return null;
  }
}

function renderTimeline() {
  const list = $("timeline");
  list.replaceChildren();
  const rows = liveEvents().filter((e) => !(e.typeId === 30 && e.team === "away") && !(e.typeId === 32 && e.team === "away"));
  for (const e of rows.reverse()) {
    if (e.typeId === 32 || e.typeId === 30) {
      const label = e.typeId === 30 ? (e.gameEnd ? "Full time" : `End of ${e.periodId === 1 ? "1st half" : e.periodId === 2 ? "2nd half" : `period ${e.periodId}`}`)
        : `Start of ${e.periodId === 1 ? "1st half" : e.periodId === 2 ? "2nd half" : `period ${e.periodId}`}`;
      list.append(el("li", { class: "divider" }, label));
      continue;
    }
    const d = describe(e);
    if (!d) continue;
    const li = el("li", { "data-team": e.team || "" });
    const what = el("span", { class: "what" }, d.text);
    if (d.sub) what.append(el("small", {}, d.sub));
    const ic = el("span", { class: d.cls });
    ic.append(icon(d.icon));
    li.append(el("span", { class: "minute" }, `${pad(e.min)}:${pad(e.sec)}`), ic, what);
    list.append(li);
  }
  $("timeline-empty").hidden = list.children.length > 0;
}

function renderStats() {
  const { stats } = state.summary;
  const rows = [["Shots", "shots"], ["Corners", "corners"], ["Fouls", "fouls"], ["Yellow cards", "yellow"], ["Red cards", "red"]];
  $("stat-body").replaceChildren(...rows.map(([label, key]) => {
    const h = stats.home[key], a = stats.away[key], total = h + a;
    const tr = el("tr");
    const mid = el("td", { class: "label" }, label);
    const bar = el("div", { class: "stat-bar", "aria-hidden": "true" });
    bar.append(el("span", { style: `width:${total ? (h / total) * 100 : 50}%` }), el("span", { style: `width:${total ? (a / total) * 100 : 50}%` }));
    mid.append(bar);
    tr.append(el("td", {}, String(h)), mid, el("td", {}, String(a)));
    return tr;
  }));
}

function renderLineups() {
  for (const side of ["home", "away"]) {
    const team = state.teams?.[side];
    const box = $(`lineup-${side}`);
    const head = el("h3");
    head.append(el("span", { class: "kit", style: `background:var(--${side})`, "aria-hidden": "true" }), document.createTextNode(teamName(side)));
    if (!team || !team.starters.length) {
      box.replaceChildren(head, el("p", { class: "empty" }, "Line-up not announced yet."));
      continue;
    }
    const list = el("ol");
    team.starters.forEach((name) => list.append(el("li", {}, name)));
    const bench = el("p", { class: "bench" }, team.subs.length ? `Bench: ${team.subs.join(", ")}` : "");
    box.replaceChildren(head, list, bench);
  }
}

/* ---------- controls ---------- */

const TABS = ["roulette", "www", "timeline"];

function selectTab(name) {
  for (const tab of TABS) {
    const on = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(on));
    $(`view-${tab}`).hidden = !on;
  }
  renderGame();
}

TABS.forEach((tab) => $(`tab-${tab}`).addEventListener("click", () => selectTab(tab)));
document.querySelector(".market-nav").addEventListener("keydown", (ev) => {
  if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
  const current = TABS.findIndex((t) => $(`tab-${t}`).getAttribute("aria-selected") === "true");
  const next = TABS[(current + (ev.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
  selectTab(next);
  $(`tab-${next}`).focus();
});

document.querySelectorAll("[data-filter]").forEach((chip) => chip.addEventListener("click", () => {
  state.filter = chip.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
  renderPitch();
}));

/* ---------- roulette (P2P, shared cycle) ---------- */

const RED = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);
const COL_W = 105 / 12, ROW_H = 68 / 3;
const ZONES = [...Array(36).keys()].map((i) => i + 1);
const OUTSIDE_LABEL = {
  red: "Red", black: "Black", odd: "Odd", even: "Even", low: "1-18", high: "19-36",
  third1: "Zones 1-12", third2: "Zones 13-24", third3: "Zones 25-36", row1: "Top channel", row2: "Centre", row3: "Bottom channel",
};
// The betslip is per market, in the browser only: nothing is staked until Place bet.
const game = { state: null, market: "next_pass", seconds: 10, chip: 5, message: null, offset: 0, slips: {}, pending: null, odds: null };
const money = (v) => `${v < 0 ? "-" : ""}£${Math.abs(Number(v)).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const short = (v) => (v >= 1000 ? `£${Math.round(v / 100) / 10}k` : `£${v}`);
const seconds = (v) => (v >= 60 ? `${v / 60} min` : `${v}s`);
// Where cell n is drawn: its column and row, mirrored when the view is flipped.
const cellSlot = (n) => {
  const c = (n - 1) / 3 | 0, r = (n - 1) % 3;
  return flipped ? { c: 11 - c, r: 2 - r } : { c, r };
};
const cellCentre = (n) => { const { c, r } = cellSlot(n); return { x: c * COL_W + COL_W / 2, y: r * ROW_H }; };
const keyOf = (s) => (s.kind === "zone" ? `z${s.zone}` : s.kind);
const outsideName = (kind) => (kind.startsWith("row") || kind === "low" || kind === "high" ? `${OUTSIDE_LABEL[kind]} (${bandLabel(kind)})` : OUTSIDE_LABEL[kind]);
const betLabel = (b) => (b.kind === "zone" ? `Zone ${b.zone}` : outsideName(b.kind));
let socket = null;

// The halves and bands depend on which way the board is drawn.
function bandLabel(kind) {
  const homeLeft = !flipped;
  return {
    low: homeLeft ? "home half" : "away half", high: homeLeft ? "away half" : "home half",
    row1: flipped ? "bottom channel" : "top channel", row2: "centre", row3: flipped ? "top channel" : "bottom channel",
  }[kind];
}

// The room is in the page address: ?room=<id>. No room means the public room of the match.
const ROOM = (() => {
  const r = new URLSearchParams(location.search).get("room") || "public";
  return /^(public|[a-z0-9]{6})$/.test(r) ? r : "public";
})();

function playerId() {
  const fresh = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`);
  try {
    let id = localStorage.getItem("sb-player");
    if (!id) { id = fresh(); localStorage.setItem("sb-player", id); }
    return id;
  } catch {
    return fresh();
  }
}
const PLAYER = playerId();

function sendGame(message) {
  if (socket && socket.readyState === WebSocket.OPEN) { socket.send(JSON.stringify(message)); return true; }
  return false;
}

/* ---------- the betslip ---------- */

const slip = () => (game.slips[game.market] ||= new Map());

function addSelection(kind, zone) {
  const s = slip();
  const key = keyOf({ kind, zone });
  const max = game.state?.maxStake || 1000;
  const cur = s.get(key);
  if (!cur && s.size >= (game.state?.maxSelections || 48)) { flash("the betslip is full"); return; }
  // Tapping again adds another chip, as on a roulette table.
  s.set(key, { kind, zone: kind === "zone" ? zone : null, amount: Math.min(max, (cur?.amount || 0) + game.chip) });
  renderGame();
}

function placeSlip() {
  const st = game.state;
  const s = slip();
  if (!st || !s.size || game.pending) return;
  const ref = Math.random().toString(36).slice(2, 10);
  const selections = [...s.values()].map((x) => (x.kind === "zone" ? { kind: "zone", zone: x.zone, amount: x.amount } : { kind: x.kind, amount: x.amount }));
  if (sendGame({ type: "br_place", ref, market: game.market, seconds: game.seconds, selections })) {
    game.pending = { ref, market: game.market };
    renderGame();
  }
}

let flashTimer = null;
function flash(text) {
  game.message = text;
  renderRoundStatus();
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { game.message = null; renderRoundStatus(); }, 3500);
}

/* ---------- board, grid and controls ---------- */

function buildBoard() {
  const layer = $("board-cells");
  for (let c = 0; c < 12; c++) {
    for (let r = 0; r < 3; r++) {
      const n = c * 3 + r + 1;
      const g = svg("g", { class: `cell ${RED.has(n) ? "red" : "black"}`, "data-cell": n, role: "button", tabindex: "0" });
      g.append(svg("rect", { class: "zone", width: COL_W, height: ROW_H }));
      g.append(svg("circle", { class: "cell-badge", r: 1.35 }));
      const num = svg("text", { class: "cell-number", "text-anchor": "middle" });
      num.textContent = String(n);
      const pill = svg("g", { class: "price" });
      pill.append(svg("rect", { width: 5.8, height: 2.4, rx: 0.4 }));
      pill.append(svg("text", { "text-anchor": "middle" }));
      g.append(num, pill, svg("g", { class: "stake" }));
      layer.append(g);
    }
  }
  document.querySelectorAll("#board .cell").forEach((g) => {
    const n = Number(g.dataset.cell);
    g.addEventListener("click", () => addSelection("zone", n));
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); addSelection("zone", n); } });
  });
  document.querySelectorAll("#outside-bets .outside").forEach((b) => b.addEventListener("click", () => addSelection(b.dataset.kind)));
}

// Places every cell for the current view. Runs at start-up and when the teams swap ends.
function layoutBoard() {
  document.querySelectorAll("#board-cells .cell").forEach((g) => {
    const { c, r } = cellSlot(Number(g.dataset.cell));
    const x = c * COL_W, y = r * ROW_H, mid = x + COL_W / 2;
    const [zone, badge, num, pill] = g.children;
    zone.setAttribute("x", x); zone.setAttribute("y", y);
    badge.setAttribute("cx", x + 1.9); badge.setAttribute("cy", y + 1.9);
    num.setAttribute("x", x + 1.9); num.setAttribute("y", y + 2.55);
    pill.children[0].setAttribute("x", mid - 2.9); pill.children[0].setAttribute("y", y + ROW_H - 3.3);
    pill.children[1].setAttribute("x", mid); pill.children[1].setAttribute("y", y + ROW_H - 1.62);
  });
  $("outside-bets").classList.toggle("flipped", flipped);
  $("lbl-low").textContent = flipped ? "Away half" : "Home half";
  $("lbl-high").textContent = flipped ? "Home half" : "Away half";
  $("lbl-row1").textContent = flipped ? "Bottom channel" : "Top channel";
  $("lbl-row3").textContent = flipped ? "Top channel" : "Bottom channel";
  if (typeof WWWUI !== "undefined") WWWUI.render();
  renderGame();
}

function buildPickers(st) {
  if ($("chip-rack").children.length) return;
  Object.entries(st.markets).forEach(([name, m]) => {
    const b = el("button", { class: "mtab", "data-market": name, "aria-pressed": String(name === game.market) });
    b.append(document.createTextNode(m.label), el("span", { class: "mtab-dot", "aria-hidden": "true" }));
    b.addEventListener("click", () => { game.market = name; renderGame(); });
    $("market-picker").append(b);
  });
  // Chips are quick picks for the stake box; any whole amount up to the maximum can be typed.
  $("stake-input").max = String(st.maxStake || 1000);
  st.chipValues.forEach((v) => {
    const b = el("button", { class: "casino-chip", "aria-pressed": String(v === game.chip), "aria-label": `£${v}` }, `£${v}`);
    b.addEventListener("click", () => { game.chip = v; $("stake-input").value = String(v); renderGame(); });
    $("chip-rack").append(b);
  });
  st.roundSeconds.forEach((v) => {
    const b = el("button", { "data-seconds": v, "aria-pressed": String(v === game.seconds) }, seconds(v));
    b.addEventListener("click", () => { game.seconds = v; renderGame(); });
    $("round-picker").append(b);
  });
}

function stakeChip(x, cy, stake) {
  const holder = svg("g");
  holder.append(svg("circle", { class: "stake-shadow", cx: x + 0.35, cy: cy + 0.45, r: 2.4 }));
  holder.append(svg("circle", { class: "stake-chip", cx: x, cy, r: 2.4 }));
  holder.append(svg("circle", { class: "stake-ring", cx: x, cy, r: 1.9 }));
  const t = svg("text", { class: "stake-text", x, y: cy + 0.68, "text-anchor": "middle" });
  t.textContent = stake >= 1000 ? `${Math.round(stake / 100) / 10}k` : String(stake);
  holder.append(t);
  return holder;
}

/* ---------- pays now: what £1 more would return, from the round's pot ---------- */

// Weight of the existing bets that win if zone z comes up: the zone itself and every outside bet holding it.
function zoneWeight(r, st, z) {
  let w = (r.byZone?.[z] || 0) * st.multipliers.zone;
  for (const [kind, zones] of Object.entries(st.groups)) {
    if (zones.includes(z)) w += (r.byOutside?.[kind] || 0) * st.multipliers[kind];
  }
  return w;
}
// What £1 more on the selection returns if it wins, with the pot as it is now. A zone: exact, given that zone
// comes up. An outside bet: the average over its zones, each taken as equally likely.
function paysNow(r, st, sel) {
  if (!r || !r.pot) return null;
  const m = sel.kind === "zone" ? st.multipliers.zone : st.multipliers[sel.kind];
  const pays = (z) => ((r.pot + 1) * (1 - st.houseCut) * m) / (zoneWeight(r, st, z) + m);
  if (sel.kind === "zone") return pays(sel.zone);
  const zones = st.groups[sel.kind];
  return zones.reduce((a, z) => a + pays(z), 0) / zones.length;
}
// The most the slip can return, over every zone that can come up, with the round's pot as it is now.
function slipReturn(r, st, selections) {
  const pot = (r?.pot || 0) + selections.reduce((a, s) => a + s.amount, 0);
  let best = 0;
  for (const z of ZONES) {
    const mine = selections.filter((s) => (s.kind === "zone" ? s.zone === z : st.groups[s.kind].includes(z)))
      .reduce((a, s) => a + s.amount * (s.kind === "zone" ? st.multipliers.zone : st.multipliers[s.kind]), 0);
    if (!mine) continue;
    const others = r ? zoneWeight(r, st, z) : 0;
    best = Math.max(best, (pot * (1 - st.houseCut) * mine) / (others + mine));
  }
  return best;
}

/* ---------- live odds: move with the ball (demo, services/liveodds.py) ---------- */

// Decimal odds for the selection from where the ball is now, or null when the market has none. Display only:
// a win still pays from the pot. An outside bet: one over the summed chances of its zones.
function liveOdds(st, sel) {
  const row = game.odds?.[game.market];
  if (!row) return null;
  if (sel.kind === "zone") return row[sel.zone - 1];
  return Math.max(1.01, 1 / st.groups[sel.kind].reduce((a, z) => a + 1 / row[z - 1], 0));
}
const oddsText = (v) => v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2);

/* ---------- the round ---------- */

const serverNow = () => Date.now() / 1000 + game.offset;

// The step of the market's round now, from the server's times.
function roundStep(r, st) {
  if (!r) return null;
  const now = serverNow();
  if (r.status === "open" && now < r.locksAt) return { step: "bet", left: r.locksAt - now, span: r.seconds };
  if (r.status === "settled" || r.status === "void") return { step: "result", left: Math.max(0, r.doneAt + st.resultSeconds - now), span: st.resultSeconds, done: true };
  return { step: "waiting" };
}
const bettingOpen = (step) => !step || step.step === "bet";

function renderGame() {
  const st = game.state;
  if (!st) return;
  buildPickers(st);
  $("wallet").textContent = money(st.balance);
  const market = st.markets[game.market];
  const r = st.rounds[game.market] || null;
  const step = roundStep(r, st);
  const open = bettingOpen(step);

  $("market-question").textContent = `Where on the pitch will the ${market.label.toLowerCase()} happen?`;
  const moving = !!game.odds?.[game.market];
  $("rule-note").textContent = `${market.definition} ${moving ? "The odds move with the ball; a win pays from the pot. " : ""}${market.voidAfter
    ? `Void if none comes within ${market.voidAfter >= 120 ? `${market.voidAfter / 60} min` : `${market.voidAfter}s`}.`
    : "It runs until the next one."}`;
  [...$("market-picker").children].forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.market === game.market));
    const live = st.rounds[b.dataset.market];
    b.classList.toggle("has-round", !!live && !["settled", "void"].includes(live.status));
  });
  // The window is set by whoever starts the round; joiners bet into it.
  $("window-group").classList.toggle("fixed", !!r);
  [...$("round-picker").children].forEach((b) => {
    b.setAttribute("aria-pressed", String(r ? Number(b.dataset.seconds) === r.seconds : Number(b.dataset.seconds) === game.seconds));
    b.disabled = !!r;
  });

  $("board").classList.toggle("locked", !open);
  $("board").classList.toggle("hide-odds", !$("odds-toggle").checked);

  const last = step?.done && st.last[game.market]?.id === r.id ? st.last[game.market] : null;
  const winning = new Set(last && last.status !== "void" ? last.zones : []);
  const s = slip();
  const inRound = {};
  for (const b of r?.mine || []) inRound[keyOf(b)] = (inRound[keyOf(b)] || 0) + b.amount;
  const live = r && !step?.done ? r : null;
  // With live odds: the odds from where the ball is now. Without: what £1 more there returns now, from the pot,
  // and nothing to show while the pot is empty.
  const price = (sel) => {
    const odds = liveOdds(st, sel);
    if (odds != null) return oddsText(odds);
    const v = paysNow(live, st, sel);
    return v == null ? "–" : v.toFixed(2);
  };

  document.querySelectorAll("#board .cell").forEach((g) => {
    const n = Number(g.dataset.cell);
    const sel = { kind: "zone", zone: n };
    const total = r?.byZone?.[n] || 0, picked = s.get(keyOf(sel))?.amount || 0, mine = inRound[keyOf(sel)] || 0;
    const shown = price(sel), [beforeMarket, before] = (g.dataset.odds || "|").split("|");
    g.querySelector(".price text").textContent = shown;
    g.dataset.odds = moving ? `${game.market}|${shown}` : "";
    // A live price that moves with the ball flashes: green when it shortens (more likely), red when it drifts.
    // Only against the same market's last live price, so a market switch or the first odds do not flash.
    if (moving && beforeMarket === game.market && before !== shown) {
      g.classList.remove("shorter", "longer");
      void g.getBoundingClientRect(); // restart the flash animation
      g.classList.add(Number(shown) < Number(before) ? "shorter" : "longer");
    }
    // An empty pot has no price to show: the pill stays hidden until someone bets, unless live odds show.
    g.classList.toggle("no-price", !moving && !live?.pot);
    g.classList.toggle("has-money", total > 0);
    g.classList.toggle("picked", picked > 0 || mine > 0);
    g.classList.toggle("win", winning.has(n));
    g.setAttribute("aria-label", `Zone ${n}, ${moving ? "odds" : "pays"} ${shown}${total ? `, ${money(total)} in the pot` : ""}${picked ? `, ${money(picked)} on your betslip` : ""}${mine ? `, ${money(mine)} placed` : ""}`);
    const chipValue = picked + mine;
    const { x, y } = cellCentre(n);
    g.querySelector(".stake").replaceChildren(...(chipValue ? [stakeChip(x, y + ROW_H / 2 - 1.6, chipValue)] : []));
  });
  document.querySelectorAll("#outside-bets .outside").forEach((b) => {
    const kind = b.dataset.kind, sel = { kind };
    const total = r?.byOutside?.[kind] || 0, picked = s.get(kind)?.amount || 0, mine = inRound[kind] || 0;
    const parts = [];
    if ($("odds-toggle").checked && (moving || live?.pot)) parts.push(price(sel));
    if (picked || mine) parts.push(money(picked + mine));
    else if (total) parts.push(`${money(total)} in`);
    b.querySelector("small").textContent = parts.join(" · ");
    b.setAttribute("aria-pressed", String(picked > 0 || mine > 0));
    b.disabled = !open;
    b.classList.toggle("win", st.groups[kind].some((z) => winning.has(z)));
  });

  const marker = $("board-marker");
  marker.replaceChildren();
  if (last && last.x != null && last.status !== "void") {
    const cx = px(last.x), cy = py(last.y);
    marker.append(svg("circle", { class: "ripple", cx, cy, r: 1.6 }));
    marker.append(svg("circle", { class: "ball", cx, cy, r: 1.3 }));
  }

  // The betslip: selections not yet placed, then the round this market is running.
  const rows = [...s.values()];
  const staked = rows.reduce((a, b) => a + b.amount, 0);
  $("slip-stake").textContent = money(staked);
  // In a pot, a win pays from other players' stakes. With nobody else in, the best a slip can do is its own
  // stake back, so say that instead of showing the stake as a "return".
  const others = (live?.pot || 0) - (live?.mine || []).reduce((a, b) => a + b.amount, 0);
  $("slip-return").textContent = !rows.length ? money(0) : others > 0 ? money(slipReturn(live, st, rows)) : "Your stake, until others join";
  $("slip-pot-line").hidden = !r;
  $("slip-pot").textContent = money(r?.pot || 0);
  $("slip-players").textContent = String(r?.players || 0);
  $("slip-viewers").textContent = String(st.viewers || 0);
  const placed = (r?.mine || []).reduce((a, b) => a + b.amount, 0);
  $("slip-mine-line").hidden = !placed || !!step?.done;
  $("slip-mine").textContent = money(placed);
  $("slip-count").textContent = String(rows.length);
  $("slip-count").hidden = !rows.length;
  $("slip-empty").hidden = rows.length > 0;
  $("slip-rows").replaceChildren(...rows.map((b) => {
    const li = el("li");
    const sel = el("span", { class: "slip-sel" });
    const tag = b.kind === "zone" ? String(b.zone) : { red: "", black: "", odd: "O", even: "E", low: "1-18", high: "19-36" }[b.kind] ?? "1/3";
    const tone = (b.kind === "zone" && RED.has(b.zone)) || b.kind === "red" ? " red" : "";
    sel.append(el("span", { class: `slip-num${tone}` }, tag), document.createTextNode(betLabel(b)));
    const input = el("input", { class: "slip-amount", type: "number", inputmode: "numeric", min: "1", step: "1", value: String(b.amount), "aria-label": `Stake on ${betLabel(b)}` });
    input.addEventListener("change", () => {
      const v = Math.floor(Number(input.value));
      if (Number.isFinite(v) && v >= 1) b.amount = Math.min(v, st.maxStake);
      renderGame();
    });
    const remove = el("button", { class: "slip-remove", "aria-label": `Remove ${betLabel(b)}` });
    remove.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';
    remove.addEventListener("click", () => { s.delete(keyOf(b)); renderGame(); });
    const stake = el("span", { class: "slip-stake" });
    stake.append(el("span", { "aria-hidden": "true" }, "£"), input, remove);
    li.append(sel, el("span", { class: "slip-price" }, price(b)), el("span", { class: "slip-market" }, `${market.label} · share of the pot`), stake);
    return li;
  }));

  const inPlay = state.summary?.status.phase === "live";
  const canPlace = rows.length > 0 && inPlay && open && !game.pending;
  const label = !r ? `Place bet${staked ? ` ${money(staked)}` : ""}` : open ? `Join round${staked ? ` ${money(staked)}` : ""}` : "Round running";
  $("btn-spin").disabled = !canPlace;
  $("btn-spin-mobile").disabled = !canPlace;
  $("btn-spin").textContent = game.pending ? "Placing…" : label;
  $("btn-spin-mobile").textContent = game.pending ? "Placing…" : !r || open ? "Place bet" : "Round running";
  $("btn-clear").disabled = !rows.length;
  $("mobile-count").textContent = `${rows.length} selection${rows.length === 1 ? "" : "s"}`;
  $("mobile-stake").textContent = money(staked);
  $("mobile-slip").hidden = !rows.length || $("view-roulette").hidden;
  [...$("chip-rack").children].forEach((b, i) => b.setAttribute("aria-pressed", String(st.chipValues[i] === game.chip)));
  renderRoundStatus();

  $("history-empty").hidden = st.history.length > 0;
  $("history").replaceChildren(...st.history.map((h) => {
    const li = el("li");
    const net = h.myReturn - h.myStake;
    const kind = h.status === "void" || !h.myStake ? "v" : h.myReturn > 0 && net >= 0 ? "w" : "l";
    const what = el("span", {}, `${h.label} · ${h.zones.length ? `zone ${h.zones.join(" & ")}` : "void"} · pot ${money(h.pot)}`);
    what.append(el("small", {}, h.reason || `${h.players} player${h.players === 1 ? "" : "s"}`));
    li.append(el("span", { class: `outcome ${kind}`, "aria-label": { v: "Void", w: "Won", l: "Lost" }[kind] }, kind.toUpperCase()), what,
      el("span", { class: net > 0 ? "net-win" : net < 0 ? "net-lose" : "" }, h.myStake ? `${net > 0 ? "+" : ""}${money(net)}` : "–"));
    return li;
  }));
}

function renderRoundStatus() {
  const st = game.state;
  const box = $("round-status");
  const overlay = $("board-overlay");
  const meter = $("round-meter");
  box.className = "round-status";
  overlay.hidden = true;
  if (!st) return;
  const market = st.markets[game.market];
  const r = st.rounds[game.market] || null;
  const step = roundStep(r, st);
  meter.hidden = !step || step.done;
  meter.classList.toggle("locked", step?.step === "waiting");
  $("round-meter-fill").style.width = step?.step === "bet" ? `${Math.min(100, (step.left / (step.span || 1)) * 100)}%` : "100%";
  if (game.message) { box.classList.add("error"); box.textContent = `Sorry, ${game.message}.`; return; }
  const last = st.last[game.market];
  if (step?.done && last && last.id === r.id) {
    const where = `zone ${last.zones.join(" & ")}`;
    let text;
    if (last.status === "void") text = `Round void: ${last.reason}.${last.myStake ? " Stake returned." : ""}`;
    else if (!last.myStake) text = `The ${market.label.toLowerCase()} was in ${where}.`;
    else {
      const net = last.myReturn - last.myStake;
      box.classList.add(last.myReturn > 0 && net >= 0 ? "win" : "lose");
      // A winning bet can return no profit: with no losers in the pot, winners only get their stakes back.
      text = last.myReturn > 0 && net > 0 ? `You won ${money(net)} (returned ${money(last.myReturn)} from the pot). ${where[0].toUpperCase()}${where.slice(1)}.`
        : last.myReturn > 0 ? `Your pick won, but the pot returned ${money(last.myReturn)} (${money(net)}). ${where[0].toUpperCase()}${where.slice(1)}.`
        : `No win. It was ${where}.`;
    }
    box.textContent = text;
    overlay.hidden = false;
    $("overlay-text").textContent = text;
  } else if (step?.step === "bet") {
    box.textContent = `Betting closes in ${Math.ceil(step.left)}s. ${r.mine.length ? "You are in this round." : "Add selections and join the round."}`;
  } else if (step) {
    box.textContent = `Betting closed. Waiting for the ${market.label.toLowerCase()}.`;
  } else {
    box.textContent = state.summary?.status.phase === "live"
      ? "Pick a chip, tap zones, then place your bet."
      : "Bets open when the match is in play.";
  }
}

$("btn-clear").addEventListener("click", () => { slip().clear(); renderGame(); });
$("btn-spin").addEventListener("click", placeSlip);
$("btn-spin-mobile").addEventListener("click", placeSlip);
$("odds-toggle").addEventListener("change", renderGame);

function handleGameMessage(data) {
  if (data.type === "br_state") {
    game.state = data.state;
    game.offset = data.state.serverTime - Date.now() / 1000;
  } else if (data.type === "br_placed") {
    // Every selection is in the round now: the betslip empties.
    if (game.pending && data.ref === game.pending.ref) {
      game.slips[game.pending.market]?.clear();
      game.pending = null;
    }
  } else if (data.type === "br_error") {
    if (game.pending && (!data.ref || data.ref === game.pending.ref)) game.pending = null;
    flash(data.message);
  } else if (data.type === "pen_state" || data.type === "pen_error") {
    if (typeof Moments === "undefined") return;
    if (data.type === "pen_state") {
      $("wallet").textContent = money(data.state.balance);
      Moments.penaltyState(data.state);
    }
    else Moments.penaltyError(data.message);
    return;
  }
  renderGame();
}

/* ---------- rooms ---------- */

function roomLink(id) {
  const q = new URLSearchParams(location.search);
  if (id === "public") q.delete("room"); else q.set("room", id);
  return `${location.origin}${location.pathname}?${q}`;
}
if (ROOM !== "public") {
  $("room-label").textContent = `Private room ${ROOM}`;
  $("room-help").textContent = "Only people with this link play in this pot.";
  $("room-copy").hidden = false;
  $("room-leave").hidden = false;
  $("room-leave").href = roomLink("public");
  $("room-create").hidden = true;
}
$("room-create").addEventListener("click", () => {
  const letters = "abcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  location.href = roomLink([...bytes].map((b) => letters[b % letters.length]).join(""));
});
$("room-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(roomLink(ROOM));
    $("room-copy").textContent = "Link copied";
  } catch {
    $("room-copy").textContent = roomLink(ROOM);
  }
  setTimeout(() => { $("room-copy").textContent = "Copy room link"; }, 2500);
});

/* ---------- connection ---------- */

function setConnection(kind, label) {
  $("conn").dataset.state = kind;
  $("conn-label").textContent = label;
}

// The lobby links here with ?fixture=<Opta fixture id>. With no fixture the server uses its configured default.
const FIXTURE = new URLSearchParams(location.search).get("fixture") || "";
const DEMO = FIXTURE.startsWith("demo-");
if (DEMO) {
  $("demo-crumb").hidden = false;
  $("data-source").textContent = "Demo data: Elite Soccer Dataset (Bassek et al., Scientific Data, 2025), CC BY 4.0";
}

let retryDelay = 1000;
function connect() {
  const query = new URLSearchParams({ player: PLAYER, room: ROOM });
  if (FIXTURE) query.set("fixture", FIXTURE);
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?${query}`);
  socket = ws;
  ws.addEventListener("open", () => { retryDelay = 1000; setConnection("live", DEMO ? "Demo match on the real clock" : "Live feed"); });
  ws.addEventListener("message", (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type.startsWith("br_") || data.type.startsWith("pen_")) { handleGameMessage(data); return; }
    if (data.type.startsWith("www_")) {
      if (typeof WWWUI !== "undefined") WWWUI.handle(data);
      return;
    }
    if (data.type === "ball") { renderBall(data, "Ball tracking"); return; }
    if (data.type === "odds") { game.odds = data.markets; renderGame(); return; }
    if (data.type === "track") { onTrack(data); return; }
    if (data.type === "demo") { renderDemoBar(data); return; }
    if (data.type === "snapshot") applySnapshot(data.data);
    else if (data.type === "update") { data.events.forEach(upsert); applySummary(data.summary); }
    else return;
    render();
    renderGame();
    if (typeof Moments !== "undefined") {
      if (data.type === "snapshot") Moments.snapshot();
      else Moments.events(data.events);
    }
  });
  ws.addEventListener("close", (ev) => {
    if (ev.code === 1008) { setConnection("down", ev.reason === "unknown room" ? "This room link is not valid." : "This match is not available. Pick another in the lobby."); return; }
    if (ev.code === 1013 && ev.reason === "too many rooms open") { setConnection("down", "Too many rooms are open on this match. Try the public room."); return; }
    if (ev.code === 1013 && ev.reason === "too many matches open") setConnection("down", "Too many matches open. Retrying");
    else setConnection("down", "Reconnecting");
    // A slip sent just before the drop may or may not have gone in. Unlock it; the next br_state shows
    // what is in the round, so the player can see it before placing again.
    if (game.pending) {
      game.pending = null;
      game.message = "the connection dropped while placing; check your bets in the round before you place again";
    }
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  });
}

/* ---------- stake box ---------- */

$("stake-input").addEventListener("input", () => {
  const v = Math.floor(Number($("stake-input").value));
  if (Number.isFinite(v) && v >= 1) game.chip = Math.min(v, game.state?.maxStake || 1000);
  [...$("chip-rack").children].forEach((b, i) => b.setAttribute("aria-pressed", String(game.state?.chipValues[i] === game.chip)));
});

/* ---------- ball and in-play flag (demo, from tracking data) ---------- */

function renderBall(b, source = "Ball tracking") {
  const layer = $("layer-ball");
  let ball = $("ball-dot");
  if (!ball) {
    ball = svg("g", { id: "ball-dot" });
    ball.append(svg("circle", { class: "ball-shadow", r: 1.15, cx: 0.25, cy: 0.35 }), svg("circle", { class: "ball-core", r: 1.05 }));
    layer.append(ball);
  }
  ball.style.transform = `translate(${px(b.x)}px, ${py(b.y)}px)`;
  ball.classList.toggle("dead", !b.live);
  $("ball-flag").hidden = false;
  $("ball-flag").dataset.live = String(b.live);
  const holder = b.possession ? ` · ${teamName(b.possession)} ball` : "";
  $("ball-flag-text").textContent = `${source} · ${b.live ? "In play" : b.off ? "Out of play" : "Dead ball"}${holder}`;
}

/* ---------- live: the ball on the estimated path from Opta events ---------- */

// The server sends the same estimated track the rounds settle on (services/balltrack.py): a straight line
// from each positioned Opta event to the next. Events reach us some seconds after the play (median about
// 2-2.5 s), so the ball is drawn LIVE_BALL_DELAY behind the server clock: then there is usually a later point
// to move toward and the ball glides instead of jumping. It still trails the real play by that delay, and it
// waits at the newest point when the feed is slower than that.
const LIVE_BALL_DELAY = 3;
const liveTrack = { points: [], offset: 0, running: false };

function onTrack(data) {
  liveTrack.points = data.points;
  liveTrack.offset = data.serverTime - Date.now() / 1000;
  if (!liveTrack.running && liveTrack.points.length) {
    liveTrack.running = true;
    requestAnimationFrame(animateLiveBall);
  }
}

function livePosition(t) {
  const pts = liveTrack.points;
  if (!pts.length) return null;
  if (t <= pts[0][0]) return { x: pts[0][1], y: pts[0][2], live: pts[0][3], off: pts[0][4] };
  for (let i = 1; i < pts.length; i++) {
    if (pts[i][0] >= t) {
      const a = pts[i - 1], b = pts[i];
      const share = b[0] === a[0] ? 1 : (t - a[0]) / (b[0] - a[0]);
      // The state is the earlier point's, as on the server: dead until the next normal event.
      return { x: a[1] + (b[1] - a[1]) * share, y: a[2] + (b[2] - a[2]) * share, live: a[3], off: a[4] };
    }
  }
  const last = pts[pts.length - 1];
  return { x: last[1], y: last[2], live: last[3], off: last[4] };
}

function animateLiveBall() {
  const at = livePosition(Date.now() / 1000 + liveTrack.offset - LIVE_BALL_DELAY);
  if (at) {
    renderBall(at, "Estimated from Opta events");
    $("ball-dot").classList.add("raf"); // every frame moves it, so no CSS easing on top
  }
  requestAnimationFrame(animateLiveBall);
}

/* ---------- demo playback ---------- */

const demoView = { speed: 1, length: 0, position: 0 };
const clockText = (s) => `${Math.floor(s / 60)}:${pad(Math.floor(s % 60))}`;
function renderDemoBar(d) {
  Object.assign(demoView, d);
  $("demo-bar").hidden = false;
  $("demo-fill").style.width = `${d.length ? (d.position / d.length) * 100 : 0}%`;
  $("demo-track").setAttribute("aria-valuenow", String(Math.round(d.length ? (d.position / d.length) * 100 : 0)));
  $("demo-time").textContent = `${clockText(d.position)} / ${clockText(d.length)}`;
  const paused = d.speed === 0;
  $("demo-play").setAttribute("aria-label", paused ? "Play" : "Pause");
  $("demo-play").innerHTML = paused
    ? '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3l8 5-8 5z" fill="currentColor"/></svg>'
    : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3v10M11 3v10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  document.querySelectorAll("#demo-speed button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.speed) === d.speed)));
}
const demoControl = (action, value) => sendGame({ type: "demo_control", action, value });
if (DEMO) {
  document.querySelectorAll("[data-skip]").forEach((b) => b.addEventListener("click", () => demoControl("skip", Number(b.dataset.skip))));
  document.querySelectorAll("#demo-speed button").forEach((b) => b.addEventListener("click", () => demoControl("speed", Number(b.dataset.speed))));
  $("demo-play").addEventListener("click", () => demoControl("speed", demoView.speed === 0 ? 1 : 0));
  $("demo-restart").addEventListener("click", () => demoControl("restart"));
  $("demo-track").addEventListener("click", (ev) => {
    const r = $("demo-track").getBoundingClientRect();
    demoControl("seek", Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * demoView.length);
  });
  $("demo-track").addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") { ev.preventDefault(); demoControl("skip", ev.key === "ArrowLeft" ? -30 : 30); }
  });
}

buildBoard();
layoutBoard();
setInterval(() => { renderClock(); renderRoundStatus(); }, 1000);
connect();
