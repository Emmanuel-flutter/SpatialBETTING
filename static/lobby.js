"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

const LIVE = ["live", "break"];

function kindOf(f) {
  if (LIVE.includes(f.status.phase)) return "live";
  return f.status.phase === "pre" ? "soon" : "done";
}

// The topic keeps only one message for older fixtures, and some fixtures stop before kick-off. Either
// way no kick-off was seen, so there is no score to show, and listing them as 0-0 would mislead.
const noMatchData = (f) => f.status.phase === "ended" && f.status.periodId == null;

function dayLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Date unknown";
  const today = new Date();
  const dayMs = 86400000;
  const key = (x) => x.toDateString();
  if (key(d) === key(today)) return "Today";
  if (key(d) === key(new Date(today.getTime() - dayMs))) return "Yesterday";
  if (key(d) === key(new Date(today.getTime() + dayMs))) return "Tomorrow";
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}

const kickOff = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
};

function statusCell(f, kind) {
  const cell = el("div", { class: "event-when" });
  if (kind === "live") {
    cell.append(el("span", { class: "event-pill is-live" }, f.status.phase === "break" ? "HT" : "LIVE"));
  } else if (kind === "done") {
    cell.append(el("span", { class: "event-pill" }, f.status.phase === "ft" ? "FT" : "Ended"));
  }
  cell.append(el("span", { class: "event-time" }, kickOff(f.startTime)));
  return cell;
}

function row(f) {
  const kind = kindOf(f);
  const li = el("li");
  const link = el("a", { class: `event-row is-${kind}`, href: `/match?fixture=${encodeURIComponent(f.fixtureId)}`,
    "aria-label": `${f.home} v ${f.away}${kind === "soon" ? `, kick-off ${kickOff(f.startTime)}` : `, ${f.score.home}-${f.score.away}, ${f.status.label}`}. Open markets` });
  const teams = el("div", { class: "event-teams" });
  for (const side of ["home", "away"]) {
    const line = el("div", { class: "event-team" });
    line.append(el("span", { class: "event-name" }, f[side]));
    if (kind !== "soon") line.append(el("b", { class: "event-score" }, String(f.score[side])));
    teams.append(line);
  }
  const go = el("span", { class: "event-chevron", "aria-hidden": "true" });
  go.innerHTML = '<svg viewBox="0 0 16 16"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  link.append(statusCell(f, kind), teams, go);
  li.append(link);
  return li;
}

// Live first, then upcoming by kick-off, then finished with the most recent first.
function order(a, b) {
  const rank = { live: 0, soon: 1, done: 2 };
  const ka = kindOf(a), kb = kindOf(b);
  if (ka !== kb) return rank[ka] - rank[kb];
  const byStart = (a.startTime || "").localeCompare(b.startTime || "");
  return ka === "done" ? -byStart : byStart;
}

function render(fixtures) {
  const groups = [];
  for (const f of fixtures) {
    const label = kindOf(f) === "live" ? "Live" : dayLabel(f.startTime);
    const last = groups.at(-1);
    if (last && last.label === label) last.items.push(f);
    else groups.push({ label, items: [f] });
  }
  $("match-list").replaceChildren(...groups.map((g) => {
    const section = el("li", { class: "event-group" });
    section.append(el("h3", { class: "event-day" }, g.label));
    const list = el("ol", { class: "event-list" });
    list.append(...g.items.map(row));
    section.append(list);
    return section;
  }));
}

async function load() {
  try {
    const res = await fetch("/api/fixtures", { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    // Live tab: only matches in play or about to start. Finished and stale matches are not listed.
    const shown = data.fixtures.filter((f) => f.home && f.away && !noMatchData(f) && kindOf(f) !== "done").sort(order);
    render(shown);
    $("match-empty").hidden = shown.length > 0;
    $("match-count").textContent = shown.length ? `${shown.length} matches` : "";
    $("conn").dataset.state = data.caughtUp ? "live" : "connecting";
    $("conn-label").textContent = data.caughtUp ? "Live feed" : `Reading the feed: ${shown.length} matches so far`;
    setTimeout(load, data.caughtUp ? 15000 : 3000);
  } catch {
    $("conn").dataset.state = "down";
    $("conn-label").textContent = "Cannot reach the server. Retrying";
    setTimeout(load, 5000);
  }
}

function selectMode(mode) {
  for (const m of ["live", "demo"]) {
    $(`tab-${m}`).setAttribute("aria-selected", String(m === mode));
    $(`panel-${m}`).hidden = m !== mode;
  }
  try { localStorage.setItem("sb-lobby-mode", mode); } catch { /* storage blocked: the choice just is not remembered */ }
}
$("tab-live").addEventListener("click", () => selectMode("live"));
$("tab-demo").addEventListener("click", () => selectMode("demo"));
try { if (localStorage.getItem("sb-lobby-mode") === "demo") selectMode("demo"); } catch { /* use the default */ }

load();
