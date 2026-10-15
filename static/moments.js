"use strict";

// Live moments: goal takeover, foul and card toasts, and a free-to-play goal-mouth pick for
// penalties and attacking free kicks. Reads the same event stream as app.js and reuses its
// helpers ($, el, svg, pad, teamName, px, py, state).
const Moments = (() => {
  const SHOT_TYPES = new Set([13, 14, 15, 16]);
  const PICK_SECONDS = 20;
  const VOID_AFTER = { penalty: 240, freekick: 90 }; // seconds after the award with no qualifying shot
  const SPOT_WAIT = 90; // seconds to wait for goal-mouth coordinates after the shot arrives
  // The snapshot holds only recent positioned events, so an older foul can arrive later as a
  // re-push with a higher seqId. Anything this far behind the match clock is not a new moment.
  const STALE_AFTER = 120;
  // Frame inside the posts and under the bar, in goal SVG units (viewBox 0 0 120 60).
  const FRAME = { x0: 12, x1: 108, y0: 8, y1: 48 };
  // Opta goal-mouth y (q102) and z (q103). Goals and saves in the recorded fixtures sit inside
  // y 45-55 and z 0-36, so the frame is taken from that. Not yet checked against Opta docs.
  // y grows toward the attacker's left, as pitch y does.
  const GM = { left: 55, right: 45, bar: 36 };
  const COLS = ["left", "centre", "right"];
  const ROWS = ["Top", "Middle", "Bottom"];
  const CELL_W = (FRAME.x1 - FRAME.x0) / 3, CELL_H = (FRAME.y1 - FRAME.y0) / 3;

  const seen = new Set();
  let ready = false;
  let moment = null;
  let demoEvents = [];
  let tickTimer = null;

  const now = () => Date.now() / 1000;
  const matchSeconds = (e) => (e.periodId || 0) * 10000 + e.min * 60 + e.sec;
  const nameOf = (e) => e._teamName || teamName(e.team);
  const cellName = (i) => `${ROWS[(i / 3) | 0]} ${COLS[i % 3]}`;

  /* ---------- feed hooks ---------- */

  function snapshot() {
    // A snapshot replaces the match. A demo restart sends the same event ids again, so start afresh.
    seen.clear();
    for (const id of state.events.keys()) seen.add(id);
    ready = true;
    if (moment) settle();
  }

  function events(list) {
    if (!ready) return;
    for (const e of list) {
      if (seen.has(e.id)) continue;
      seen.add(e.id);
      if (!e.deleted && !isStale(e)) trigger(e);
    }
    if (moment) settle();
  }

  function isStale(e) {
    const clock = state.summary?.clock;
    if (!clock) return false;
    return matchSeconds(e) < matchSeconds(clock) - STALE_AFTER;
  }

  function trigger(e) {
    if (e.typeId === 16) goalTakeover(e, moment?.kind === "penalty" && e.penalty ? 1600 : 0);
    if (e.typeId === 17 && e.card) toast({ kind: e.card === "yellow" ? "yellow" : "red", title: e.card === "yellow" ? "Yellow card" : e.card === "second_yellow" ? "Second yellow, red card" : "Red card", sub: `${e.player || "Player"}, ${nameOf(e)}`, e });
    if (e.typeId !== 4 || !e.team) return;
    if (e.outcome === 0) toast({ kind: "foul", title: `Foul by ${e.player || nameOf(e)}`, sub: `${nameOf(e)} · ${pad(e.min)}:${pad(e.sec)}`, e });
    if (e.outcome !== 1) return;
    // A penalty is a peer-to-peer pot run by the server; the popup opens from its pen_state.
    if (e.penalty) return;
    if (isDangerousFreeKick(e) && !(moment && moment.status !== "done")) open("freekick", e);
  }

  // Attacking-side coordinates: Opta x grows toward the goal the team attacks.
  function attacking(e) {
    return e.team === "away" ? { x: 100 - e.x, y: 100 - e.y } : { x: e.x, y: e.y };
  }

  function distance(e) {
    const a = attacking(e);
    return Math.round(Math.hypot((100 - a.x) * 1.05, (a.y - 50) * 0.68));
  }

  function isDangerousFreeKick(e) {
    if (e.x == null) return false;
    const a = attacking(e);
    const inBox = a.x >= 83 && a.y >= 21 && a.y <= 79;
    return a.x >= 70 && !inBox && distance(e) <= 35;
  }

  /* ---------- goal-mouth moment ---------- */

  function open(kind, award, opts = {}) {
    const pickSeconds = opts.pickSeconds || PICK_SECONDS;
    moment = {
      kind, award, team: award.team, teamLabel: nameOf(award), taker: opts.taker || null,
      openedAt: now(), closesAt: now() + pickSeconds, pickSeconds,
      voidAt: now() + (opts.voidAfter || VOID_AFTER[kind]),
      pick: null, status: "open", shot: null, shotSeenAt: null, replay: opts.replay || null,
      p2p: opts.p2p || null,
    };
    buildGoal();
    $("setpiece").hidden = false;
    $("setpiece").dataset.kind = kind;
    $("sp-kicker").textContent = kind === "penalty" ? "Penalty" : `Free kick · ${distance(award)}m`;
    $("sp-title").textContent = kind === "penalty" ? `Penalty to ${moment.teamLabel}` : `Free kick to ${moment.teamLabel}`;
    $("sp-sub").textContent = kind === "penalty" ? "Where will the penalty go? Pick your spot." : "Where will the free kick go? Pick your spot.";
    $("sp-replay").textContent = moment.replay ? `Replay: ${moment.replay}` : "";
    $("sp-badge").textContent = moment.p2p ? "Peer to peer" : "Free to play";
    $("sp-note").textContent = moment.p2p
      ? "Only a goal counts. Winners share the pot; a save or a miss returns every stake."
      : "No stake. Picks are not bets.";
    $("sp-stake").hidden = !moment.p2p;
    if (moment.p2p) $("sp-sub").textContent = "Where will it be scored? Pick a zone of the goal and stake.";
    renderOutcomes();
    renderPick();
    announce(`${$("sp-title").textContent}. Picks close in ${pickSeconds} seconds.`);
    $("setpiece").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
    clearInterval(tickTimer);
    tickTimer = setInterval(tick, 250);
    tick();
  }

  function tick() {
    if (!moment) return;
    const left = Math.max(0, moment.closesAt - now());
    const ring = $("sp-ring");
    if (moment.status === "open") {
      $("sp-secs").textContent = String(Math.ceil(left));
      ring.style.strokeDashoffset = String(100 - (left / moment.pickSeconds) * 100);
      $("sp-timer").classList.toggle("urgent", left <= 5);
      if (left <= 0) lock("Picks closed. Here it comes.");
    }
    if (moment.p2p) return; // the server settles the pot
    if (moment.status !== "done" && now() > moment.voidAt && !moment.shot) {
      finish("void", moment.kind === "penalty" ? "No penalty kick arrived from the feed. Pick cancelled." : "No shot came from the free kick. Pick cancelled.");
    }
    if (moment.status === "pending" && now() - moment.shotSeenAt > SPOT_WAIT) {
      finish("void", "Scored, but the feed did not confirm the exact spot. Pick cancelled.");
    }
  }

  function lock(text) {
    moment.status = "locked";
    $("sp-secs").textContent = "0";
    $("sp-timer").classList.remove("urgent");
    $("goal").classList.add("locked");
    overlay(text);
    status(moment.pick == null ? "No pick made. Watch where it goes." : `Your pick: ${pickLabel(moment.pick)}.`);
    announce("Picks closed.");
  }

  function candidates() {
    return [...state.events.values(), ...demoEvents];
  }

  // The first shot by the awarded team after the award; for a penalty, it must carry the penalty flag.
  function findShot() {
    const after = matchSeconds(moment.award);
    const shots = candidates()
      .filter((e) => !e.deleted && SHOT_TYPES.has(e.typeId) && e.team === moment.team && matchSeconds(e) >= after)
      .filter((e) => moment.kind !== "penalty" || e.penalty)
      .filter((e) => moment.kind !== "freekick" || matchSeconds(e) - after <= 75)
      .sort((a, b) => matchSeconds(a) - matchSeconds(b));
    return shots[0] || null;
  }

  function settle() {
    if (!moment || moment.status === "done" || moment.p2p) return;
    const shot = findShot();
    if (!shot) return;
    if (moment.status === "open") lock("Picks closed. Here it comes.");
    if (!moment.shotSeenAt) moment.shotSeenAt = now();
    moment.shot = shot;
    const outcome = shot.typeId === 16 ? "scored" : shot.typeId === 15 ? "saved" : "missed";
    const spot = spotOf(shot);
    if (outcome === "scored" && !spot && typeof moment.pick === "number") {
      // Only a zone pick needs the goal-mouth spot. The live feed can send it later in a re-push; the
      // demo data has no goal-mouth position at all, so a zone pick there cannot settle.
      if (typeof DEMO !== "undefined" && DEMO) {
        finish("void", "Scored. This demo data has no goal-mouth position, so zone picks cannot settle. Pick cancelled.");
        return;
      }
      moment.status = "pending";
      overlay("Scored. Waiting for the exact spot from the feed.");
      return;
    }
    reveal(outcome, spot, shot);
  }

  function spotOf(shot) {
    const gy = shot.goalMouthY, gz = shot.goalMouthZ;
    if (gy == null || gz == null) return null;
    const x = FRAME.x0 + ((GM.left - gy) / (GM.left - GM.right)) * (FRAME.x1 - FRAME.x0);
    const y = FRAME.y1 - (gz / GM.bar) * (FRAME.y1 - FRAME.y0);
    const inside = x >= FRAME.x0 && x <= FRAME.x1 && y >= FRAME.y0 && y <= FRAME.y1;
    const cell = inside ? Math.min(2, ((y - FRAME.y0) / CELL_H) | 0) * 3 + Math.min(2, ((x - FRAME.x0) / CELL_W) | 0) : null;
    return { x: Math.max(2, Math.min(118, x)), y: Math.max(2, Math.min(56, y)), cell, inside };
  }

  function reveal(outcome, spot, shot) {
    moment.status = "done";
    clearInterval(tickTimer);
    $("goal").classList.remove("locked");
    overlay(null);
    // With no goal-mouth spot: a goal goes into the middle of the net, a save to the keeper, a miss wide.
    const target = spot || (outcome === "scored" ? { x: 60, y: 24 } : outcome === "saved" ? { x: 60, y: 30 } : { x: 112, y: 4 });
    animateBall(target, outcome);
    const where = spot?.cell != null ? cellName(spot.cell).toLowerCase() : null;
    const what = outcome === "scored" ? `Scored${where ? `, ${where}` : ""}` : outcome === "saved" ? "Saved" : shot.typeId === 14 ? "Off the woodwork" : "Off target";
    const pick = moment.pick;
    const current = moment;
    let won = null;
    if (pick != null) {
      won = typeof pick === "number" ? outcome === "scored" && spot?.cell === pick : pick === outcome;
    }
    setTimeout(() => {
      if (moment !== current) return;
      if (spot?.cell != null && outcome !== "missed") cellEl(spot.cell)?.classList.add("hit");
      if (outcome !== "scored") $("sp-outcomes").querySelector(`[data-outcome="${outcome}"]`)?.classList.add("hit");
      const text = won == null ? `${what}. ${shot.player || ""}`.trim()
        : won ? `You called it. ${what}.` : `Not this time. ${what}.`;
      status(text, won == null ? "" : won ? "win" : "lose");
      overlay(text, won ? "win" : null);
      announce(text);
      $("sp-close").hidden = false;
    }, matchMedia("(prefers-reduced-motion: reduce)").matches ? 150 : 820);
  }

  function finish(kind, text) {
    moment.status = "done";
    clearInterval(tickTimer);
    $("goal").classList.add("locked");
    overlay(text);
    status(text);
    announce(text);
    $("sp-close").hidden = false;
  }

  function close() {
    clearInterval(tickTimer);
    moment = null;
    $("setpiece").hidden = true;
  }

  /* ---------- peer-to-peer penalty (server pot) ---------- */

  let penOffset = 0;
  let penLast = null;

  function penaltyState(st) {
    penOffset = st.serverTime - now();
    penLast = st.last;
    const pot = st.pot;
    const live = pot && (pot.status === "open" || pot.status === "locked");
    if (live && !(moment?.p2p && moment.p2p.id === pot.id)) {
      const award = { team: pot.team, min: pot.min, sec: pot.sec, _teamName: teamName(pot.team) };
      open("penalty", award, { pickSeconds: st.pickSeconds, p2p: pot });
      // The server's close time, in this browser's clock.
      moment.closesAt = pot.closesAt - penOffset;
      moment.openedAt = moment.closesAt - st.pickSeconds;
    }
    if (!moment?.p2p) return;
    if (pot && pot.id === moment.p2p.id) moment.p2p = pot;
    if (live && pot.status === "locked" && moment.status === "open") lock(pot.kickIn ? "The kick is in." : "Picks closed. Here it comes.");
    if (moment.status !== "done") renderPot();
    if (st.last && st.last.id === moment.p2p.id && moment.status !== "done") revealPot(st.last);
  }

  function penaltyError(message) {
    if (moment?.p2p) status(`Sorry, ${message}.`, "lose");
  }

  function renderPot() {
    const pot = moment.p2p;
    const mine = {};
    for (const b of pot.mine) mine[b.zone] = (mine[b.zone] || 0) + b.amount;
    document.querySelectorAll("#gm-cells .gm-cell").forEach((c) => {
      const zone = Number(c.dataset.cell);
      c.classList.toggle("picked", zone in mine);
      c.setAttribute("aria-pressed", String(zone in mine));
      const on = pot.byZone[zone];
      c.querySelector(".gm-money").textContent = on ? money(on) : "";
    });
    const myTotal = pot.mine.reduce((s, b) => s + b.amount, 0);
    $("sp-pot").textContent = money(pot.pot);
    $("sp-players").textContent = String(pot.players);
    $("sp-mine").textContent = money(myTotal);
    if (moment.status === "open") {
      status(myTotal ? `You have ${money(myTotal)} in the pot. Tap more zones until picks close.`
        : "Tap a zone of the goal to stake on it.");
    } else if (moment.status === "locked") {
      status(myTotal ? `Picks closed. You have ${money(myTotal)} in the pot.` : "Picks closed. Watch where it goes.");
    }
    $("sp-close").hidden = true;
  }

  // The centre of the winning zones in goal SVG units; with a column only, the middle of that column.
  function zonesCentre(zones) {
    const xs = zones.map((z) => FRAME.x0 + (z % 3 + 0.5) * CELL_W);
    const ys = zones.map((z) => FRAME.y0 + (((z / 3) | 0) + 0.5) * CELL_H);
    return { x: xs.reduce((a, b) => a + b, 0) / xs.length, y: ys.reduce((a, b) => a + b, 0) / ys.length };
  }

  function revealPot(last) {
    moment.status = "done";
    clearInterval(tickTimer);
    $("goal").classList.remove("locked");
    overlay(null);
    const scored = !!last.scored && last.zones.length > 0;
    const outcome = scored ? "scored" : last.kick === "saved" ? "saved" : last.kick ? "missed" : null;
    if (outcome) animateBall(scored ? zonesCentre(last.zones) : outcome === "saved" ? { x: 60, y: 30 } : { x: 112, y: 4 }, outcome);
    const current = moment;
    setTimeout(() => {
      if (moment !== current) return;
      if (scored) last.zones.forEach((z) => cellEl(z)?.classList.add("hit"));
      const where = scored ? (last.columnOnly ? `the ${["left", "centre", "right"][last.zones[0] % 3]} side` : last.names.join(" & ").toLowerCase()) : "";
      let text, tone = "";
      if (last.status === "void") {
        text = `Pot void: ${last.reason}.${last.myStake ? " Stake returned." : ""}`;
      } else if (!last.myStake) {
        text = scored ? `Scored, ${where}.` : "No goal.";
      } else {
        const net = last.myReturn - last.myStake;
        tone = last.myReturn > 0 ? "win" : "lose";
        text = last.myReturn > 0
          ? `Scored, ${where}. You won ${money(last.myReturn)} from the pot (${net >= 0 ? "+" : ""}${money(net)}).`
          : `Scored, ${where}. Not this time.`;
      }
      if (last.columnOnly) text += " Demo: the column comes from the ball track.";
      status(text, tone);
      overlay(text, tone === "win" ? "win" : null);
      announce(text);
      $("sp-close").hidden = false;
    }, outcome && !matchMedia("(prefers-reduced-motion: reduce)").matches ? 820 : 150);
  }

  /* ---------- goal SVG ---------- */

  function buildGoal() {
    const g = $("goal");
    g.replaceChildren();
    g.classList.remove("locked");
    const defs = svg("defs");
    defs.innerHTML = `
      <linearGradient id="gm-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#00064f"/><stop offset="1" stop-color="#000a8c"/></linearGradient>
      <linearGradient id="gm-grass" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2e7d3b"/><stop offset="1" stop-color="#1f5e2b"/></linearGradient>
      <pattern id="gm-net" width="3" height="3" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <path d="M0 0H3M0 0V3" stroke="rgba(255,255,255,0.32)" stroke-width="0.25" fill="none"/>
      </pattern>
      <radialGradient id="gm-light" cx="50%" cy="0%" r="80%"><stop offset="0" stop-color="#fff" stop-opacity="0.18"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>`;
    g.append(defs);
    g.append(svg("rect", { width: 120, height: 48, fill: "url(#gm-sky)" }));
    g.append(svg("rect", { width: 120, height: 48, fill: "url(#gm-light)" }));
    g.append(svg("rect", { y: 48, width: 120, height: 12, fill: "url(#gm-grass)" }));
    g.append(svg("path", { class: "gm-line", d: "M0 48.4H120" }));
    g.append(svg("path", { class: "gm-line", d: "M30 59.5L36 51H84L90 59.5" }));
    const net = svg("g", { class: "gm-net", id: "gm-net" });
    net.append(svg("rect", { x: FRAME.x0, y: FRAME.y0, width: FRAME.x1 - FRAME.x0, height: FRAME.y1 - FRAME.y0, fill: "url(#gm-net)" }));
    g.append(net);
    const keeper = svg("g", { class: "gm-keeper", id: "gm-keeper" });
    keeper.append(svg("path", { d: "M57.4 28.6a2.6 2.6 0 1 1 5.2 0a2.6 2.6 0 1 1-5.2 0zM55 32.4h10l5.5-3.4 1 1.6-5.6 4.4V40l1.6 8h-3.2L62 41h-4l-2.3 7h-3.2l1.6-8v-5l-5.6-4.4 1-1.6z" }));
    g.append(keeper);
    if (moment.kind === "freekick") {
      const wall = svg("g", { class: "gm-wall" });
      [34, 41, 48, 55].forEach((x) => {
        wall.append(svg("circle", { cx: x + 2.6, cy: 34.4, r: 1.7 }));
        wall.append(svg("path", { d: `M${x} 36.4h5.2l0.6 5.6-0.6 6.4h-1.8l-0.4-5.4h-0.8l-0.4 5.4h-1.8l-0.6-6.4z` }));
      });
      g.append(wall);
    }
    const cells = svg("g", { id: "gm-cells" });
    for (let i = 0; i < 9; i++) {
      const x = FRAME.x0 + (i % 3) * CELL_W, y = FRAME.y0 + ((i / 3) | 0) * CELL_H;
      const c = svg("g", { class: "gm-cell", "data-cell": i, role: "button", tabindex: "0", "aria-label": cellName(i) });
      c.append(svg("rect", { class: "gm-zone", x: x + 0.4, y: y + 0.4, width: CELL_W - 0.8, height: CELL_H - 0.8, rx: 0.6 }));
      const t = svg("text", { class: "gm-label", x: x + CELL_W / 2, y: y + CELL_H / 2 + 0.9, "text-anchor": "middle" });
      t.textContent = cellName(i);
      c.append(t);
      // The money on the zone (peer-to-peer penalty only).
      c.append(svg("text", { class: "gm-money", x: x + CELL_W / 2, y: y + CELL_H / 2 + 4.4, "text-anchor": "middle" }));
      const choose = () => choosePick(i);
      c.addEventListener("click", choose);
      c.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); choose(); } });
      cells.append(c);
    }
    g.append(cells);
    g.append(svg("rect", { class: "gm-post", x: FRAME.x0 - 1.4, y: FRAME.y0 - 1.4, width: 1.4, height: FRAME.y1 - FRAME.y0 + 1.6 }));
    g.append(svg("rect", { class: "gm-post", x: FRAME.x1, y: FRAME.y0 - 1.4, width: 1.4, height: FRAME.y1 - FRAME.y0 + 1.6 }));
    g.append(svg("rect", { class: "gm-post", x: FRAME.x0 - 1.4, y: FRAME.y0 - 1.4, width: FRAME.x1 - FRAME.x0 + 2.8, height: 1.4 }));
    const ball = svg("g", { class: "gm-ball", id: "gm-ball" });
    ball.append(svg("circle", { r: 1.9, class: "gm-ball-body" }));
    ball.append(svg("path", { class: "gm-ball-patch", d: "M0-0.8l0.76 0.55-0.29 0.9h-0.94l-0.29-0.9z" }));
    ball.setAttribute("transform", "translate(60 55)");
    g.append(ball);
  }

  const cellEl = (i) => $("gm-cells")?.querySelector(`[data-cell="${i}"]`);

  function renderOutcomes() {
    // A penalty pays on where it is scored only, so it has no saved or missed pick.
    const opts = moment.kind === "penalty" ? [] : [["saved", "Saved or blocked"], ["missed", "Off target"]];
    $("sp-outcomes").replaceChildren(...opts.map(([key, label]) => {
      const b = el("button", { class: "mtab", "data-outcome": key, "aria-pressed": "false" }, label);
      b.addEventListener("click", () => choosePick(key));
      return b;
    }));
  }

  function choosePick(pick) {
    if (!moment || moment.status !== "open") return;
    if (moment.p2p) {
      const amount = Math.floor(Number($("sp-amount").value));
      if (!(amount >= 1)) { status("Enter a whole stake of £1 or more.", "lose"); return; }
      sendGame({ type: "pen_bet", zone: pick, amount });
      return;
    }
    moment.pick = moment.pick === pick ? null : pick;
    renderPick();
    if (moment.pick != null) announce(`Picked ${pickLabel(moment.pick)}.`);
  }

  const pickLabel = (p) => (typeof p === "number" ? cellName(p).toLowerCase() : $("sp-outcomes").querySelector(`[data-outcome="${p}"]`)?.textContent.toLowerCase() || p);

  function renderPick() {
    if (moment.p2p) { renderPot(); return; }
    document.querySelectorAll("#gm-cells .gm-cell").forEach((c) => {
      const on = moment.pick === Number(c.dataset.cell);
      c.classList.toggle("picked", on);
      c.setAttribute("aria-pressed", String(on));
    });
    $("sp-outcomes").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(moment.pick === b.dataset.outcome)));
    status(moment.pick == null ? "Tap a zone of the goal, or pick an outcome." : `Your pick: ${pickLabel(moment.pick)}. Change it until picks close.`);
    $("sp-close").hidden = true;
  }

  function animateBall(target, outcome) {
    const ball = $("gm-ball");
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const end = `translate(${target.x}px, ${target.y}px) scale(1)`;
    if (reduced || !ball.animate) { ball.style.transform = end; return; }
    const mid = `translate(${(60 + target.x) / 2}px, ${Math.min(55, target.y) - 6}px) scale(1.25)`;
    ball.animate([{ transform: "translate(60px, 55px) scale(1.7)" }, { transform: mid, offset: 0.55 }, { transform: end }],
      { duration: 700, easing: "cubic-bezier(.3,0,.2,1)", fill: "forwards" });
    if (outcome === "saved") {
      const dx = Math.max(-14, Math.min(14, target.x - 60));
      $("gm-keeper").animate([{ transform: "translate(0px, 0px) rotate(0deg)" }, { transform: `translate(${dx}px, ${target.y < 30 ? -6 : 2}px) rotate(${dx > 0 ? 35 : -35}deg)` }],
        { duration: 520, delay: 180, easing: "ease-out", fill: "forwards" });
    } else {
      // Scored: the keeper guesses wrong. Missed: the keeper goes the right way but the ball is beyond reach.
      const toward = target.x > 60 ? 12 : -12;
      const dive = outcome === "scored" ? -toward : toward;
      $("gm-keeper").animate([{ transform: "translate(0px, 0px) rotate(0deg)" }, { transform: `translate(${dive}px, 3px) rotate(${dive > 0 ? 50 : -50}deg)` }],
        { duration: 520, delay: 120, easing: "ease-out", fill: "forwards" });
    }
    if (outcome === "scored") {
      $("gm-net").animate([{ transform: "translate(0px, 0px)" }, { transform: "translate(0px, 0.8px)" }, { transform: "translate(0px, -0.4px)" }, { transform: "translate(0px, 0px)" }],
        { duration: 420, delay: 680, easing: "ease-out" });
    }
  }

  function overlay(text, tone) {
    const box = $("sp-overlay");
    box.hidden = !text;
    if (text) { $("sp-overlay-text").textContent = text; $("sp-overlay-text").dataset.tone = tone || ""; }
  }

  function status(text, tone = "") {
    const box = $("sp-status");
    box.className = `round-status${tone ? ` ${tone}` : ""}`;
    box.textContent = text;
  }

  function announce(text) { $("sp-live").textContent = text; }

  /* ---------- goal takeover ---------- */

  let takeoverTimer = null, takeoverFade = null, takeoverBack = null;
  function hideTakeover() {
    const box = $("goal-takeover");
    clearTimeout(takeoverTimer);
    document.removeEventListener("keydown", onTakeoverKey);
    box.classList.add("out");
    takeoverFade = setTimeout(() => {
      box.hidden = true;
      box.classList.remove("in", "out");
      takeoverBack?.focus?.();
      takeoverBack = null;
    }, 300);
  }
  function onTakeoverKey(ev) { if (ev.key === "Escape") hideTakeover(); }

  function goalTakeover(e, delay = 0) {
    setTimeout(() => {
      const box = $("goal-takeover");
      clearTimeout(takeoverFade);
      clearTimeout(takeoverTimer);
      if (box.hidden) takeoverBack = document.activeElement;
      $("gt-team").textContent = nameOf(e);
      $("gt-sub").textContent = `${e.player || ""}${e.player ? " " : ""}${e.min + 1}'${e.penalty ? " · penalty" : ""}${e.ownGoal ? " · own goal" : ""}`;
      $("gt-kit").style.background = `var(--${e.team || "home"})`;
      box.hidden = false;
      box.classList.remove("in", "out");
      void box.offsetWidth;
      box.classList.add("in");
      document.removeEventListener("keydown", onTakeoverKey);
      document.addEventListener("keydown", onTakeoverKey);
      box.onclick = hideTakeover;
      takeoverTimer = setTimeout(hideTakeover, 3000);
    }, delay);
  }

  /* ---------- toasts ---------- */

  function toast({ kind, title, sub, e }) {
    const box = el("div", { class: `toast toast-${kind}` });
    if (e.x != null) {
      const mini = svg("svg", { class: "toast-pitch", viewBox: "0 0 105 68", "aria-hidden": "true" });
      mini.append(svg("rect", { width: 105, height: 68, class: "tp-grass" }));
      mini.append(svg("path", { class: "tp-line", d: "M1 1H104V67H1ZM52.5 1V67M1 14H17V54H1M104 14H88V54H104" }));
      mini.append(svg("circle", { class: "tp-dot", cx: px(e.x), cy: py(e.y), r: 4 }));
      box.append(mini);
    } else {
      const ic = el("span", { class: "toast-icon" });
      ic.append(icon("card"));
      box.append(ic);
    }
    const text = el("div", { class: "toast-text" });
    text.append(el("strong", {}, title), el("span", {}, sub));
    box.append(text);
    $("toasts").append(box);
    while ($("toasts").children.length > 3) $("toasts").firstElementChild.remove();
    setTimeout(() => { box.classList.add("out"); setTimeout(() => box.remove(), 250); }, 4500);
  }

  /* ---------- demo replay (?demo=penalty|freekick|goal|foul) ---------- */

  // Replays recorded Opta values so the moments can be seen when no match is live.
  function demo(kind) {
    ready = true;
    const at = (min, sec) => ({ periodId: 1, min, sec });
    if (kind === "penalty") {
      const award = { id: -1, typeId: 4, outcome: 1, team: "home", _teamName: "Coventry City", player: "S. Cherif", x: 90.6, y: 37, penalty: true, ...at(3, 55) };
      open("penalty", award, { pickSeconds: 10, voidAfter: 60, replay: "Coventry City v Newcastle United, 12 Oct, 5:40, J. Rudoni" });
      setTimeout(() => {
        demoEvents = [{ id: -2, typeId: 16, outcome: 1, team: "home", _teamName: "Coventry City", player: "J. Rudoni", x: 88.5, y: 50, penalty: true, goalMouthY: 47.4, goalMouthZ: 3.2, ...at(5, 40) }];
        settle();
        goalTakeover(demoEvents[0], 1600);
      }, 12500);
    } else if (kind === "freekick") {
      const award = { id: -3, typeId: 4, outcome: 1, team: "away", _teamName: "Shandong Taishan", player: "V. Qazaishvili", x: 20.4, y: 21.4, ...at(40, 8) };
      open("freekick", award, { pickSeconds: 10, voidAfter: 60, replay: "Chongqing Tonglianglong v Shandong Taishan, 11 Oct, 41:02, Liu Yang" });
      setTimeout(() => {
        demoEvents = [{ id: -4, typeId: 14, outcome: 1, team: "away", _teamName: "Shandong Taishan", player: "Liu Yang", x: 11.5, y: 50.1, goalMouthY: 44.8, goalMouthZ: 15.8, ...at(41, 2) }];
        settle();
      }, 12500);
    } else if (kind === "goal") {
      goalTakeover({ typeId: 16, team: "home", _teamName: "Coventry City", player: "J. Rudoni", min: 5, sec: 40, penalty: true }, 800);
    } else if (kind === "foul") {
      toast({ kind: "foul", title: "Foul by L. Horníček", sub: "Newcastle United · 03:55", e: { x: 90.6, y: 37 } });
      setTimeout(() => toast({ kind: "yellow", title: "Yellow card", sub: "L. Horníček, Newcastle United", e: {} }), 900);
    }
  }

  $("sp-close").addEventListener("click", close);
  const demoKind = new URLSearchParams(location.search).get("demo");
  if (demoKind) setTimeout(() => demo(demoKind), 1500);

  return { snapshot, events, penaltyState, penaltyError };
})();
