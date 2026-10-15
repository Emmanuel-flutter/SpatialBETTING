"use strict";

// Who Where When: a pre-match P2P pot on the first goal. The server (services/www.py) holds the pot and
// settles it; this file draws the picker (scorer, zone grid, minute band) and sends the pick.
// Uses app.js helpers: $, el, money, sendGame, state, teamName, RED.
const WWWUI = (() => {
  const view = { st: null, scorer: null, cell: null, band: null, message: null };

  /* ---------- where: the 36-zone grid, the roulette numbering, home attacking right ---------- */

  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 12; c++) {
      const n = c * 3 + r + 1;
      const b = el("button", { class: `zone ${RED.has(n) ? "red" : "black"}`, "data-cell": n, "aria-pressed": "false" });
      b.append(el("span", { class: "zone-n" }, String(n)), el("span", { class: "zone-v" }, ""));
      b.addEventListener("click", () => { view.cell = view.cell === n ? null : n; render(); });
      $("www-grid").append(b);
    }
  }

  const stake = () => {
    const v = Math.floor(Number($("www-stake").value));
    return Number.isFinite(v) ? v : 0;
  };

  $("www-stake").addEventListener("input", render);
  $("www-join").addEventListener("click", () => {
    if (!view.scorer) { flash("Pick a scorer first."); return; }
    if (view.cell == null) { flash("Pick a zone first."); return; }
    if (view.band == null) { flash("Pick a minute band first."); return; }
    sendGame({ type: "www_bet", scorer: view.scorer.name, team: view.scorer.team, cell: view.cell, band: view.band, amount: stake() });
  });

  let flashTimer = null;
  function flash(text) {
    view.message = text;
    render();
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { view.message = null; render(); }, 3000);
  }

  /* ---------- who: both squads side by side ---------- */

  let scorerKey = "";
  function renderScorers() {
    const teams = state.teams;
    if (!teams) return;
    const key = JSON.stringify([teamName("home"), teamName("away"), teams.home?.starters, teams.home?.subs, teams.away?.starters, teams.away?.subs]);
    if (key !== scorerKey) {
      scorerKey = key;
      const column = (side) => {
        const col = el("div", { class: `vs-team is-${side}` });
        const head = el("div", { class: "vs-head" });
        head.append(el("span", { class: "kit", style: `background:var(--${side})`, "aria-hidden": "true" }), el("strong", {}, teamName(side)));
        col.append(head);
        const group = (title, names) => {
          if (!names.length) return;
          col.append(el("span", { class: "vs-group" }, title));
          const list = el("div", { class: "vs-list" });
          for (const name of names) {
            const b = el("button", { class: "vs-player", "data-name": name, "data-team": side, "aria-pressed": "false" });
            b.append(el("span", { class: "vs-name" }, name), el("span", { class: "vs-money" }));
            b.addEventListener("click", () => {
              view.scorer = view.scorer?.name === name && view.scorer?.team === side ? null : { name, team: side };
              render();
            });
            list.append(b);
          }
          col.append(list);
        };
        // Pre-match, anyone in the squad can score first: the starters and the bench.
        group("Starting XI", teams[side]?.starters || []);
        group("Bench", teams[side]?.subs || []);
        if (!(teams[side]?.starters || []).length && !(teams[side]?.subs || []).length) col.append(el("p", { class: "empty" }, "Line-up not announced yet."));
        return col;
      };
      $("www-scorer").replaceChildren(column("home"), el("span", { class: "vs-badge", "aria-hidden": "true" }, "VS"), column("away"));
    }
    const byScorer = view.st?.byScorer || {};
    $("www-scorer").querySelectorAll(".vs-player").forEach((b) => {
      const on = view.scorer?.name === b.dataset.name && view.scorer?.team === b.dataset.team;
      b.setAttribute("aria-pressed", String(on));
      const v = byScorer[`${b.dataset.team}:${b.dataset.name}`];
      b.querySelector(".vs-money").textContent = v ? money(v) : "";
    });
  }

  /* ---------- where and when ---------- */

  function renderGrid() {
    $("www-dir-home").textContent = teamName("home");
    $("www-dir-away").textContent = teamName("away");
    const byCell = view.st?.byCell || {};
    const mine = new Set((view.st?.mine || []).map((p) => p.cell));
    const won = new Set(view.st?.result?.cells || []);
    $("www-grid").querySelectorAll(".zone").forEach((b) => {
      const n = Number(b.dataset.cell), v = byCell[n] || 0;
      b.setAttribute("aria-pressed", String(view.cell === n));
      b.classList.toggle("has-money", v > 0);
      b.classList.toggle("mine", mine.has(n));
      b.classList.toggle("win", won.has(n));
      b.querySelector(".zone-v").textContent = v ? money(v) : "";
      b.setAttribute("aria-label", `Zone ${n}${v ? `, ${money(v)} in the pot` : ""}`);
    });
  }

  function renderBands() {
    const bands = view.st?.bands || [];
    const box = $("www-bands");
    if (box.children.length !== bands.length) {
      box.replaceChildren(...bands.map((label, i) => {
        const b = el("button", { "data-band": i, "aria-pressed": "false" }, `${label}'`);
        b.addEventListener("click", () => { view.band = view.band === i ? null : i; render(); });
        return b;
      }));
    }
    [...box.children].forEach((b, i) => b.setAttribute("aria-pressed", String(view.band === i)));
  }

  /* ---------- the pot, picks, results ---------- */

  const bandLabel = (i) => `${view.st?.bands[i] ?? "?"}'`;
  const pickText = (p) => `${p.scorer} · zone ${p.cell} · ${bandLabel(p.band)}`;

  // The same rule as the server (services/www.py): the zone, or one next to it, and the band, or the one
  // next to it. Used to show what an exact pick would get from the pot as it is now.
  const near = (a, b) => {
    const ca = (a - 1) / 3 | 0, ra = (a - 1) % 3, cb = (b - 1) / 3 | 0, rb = (b - 1) % 3;
    return a !== b && Math.abs(ca - cb) <= 1 && Math.abs(ra - rb) <= 1;
  };
  function shareIfExact(st, pick, amount) {
    const nearScore = st.rules.nearScore;
    const scoreOf = (p) => {
      if (p.scorer !== pick.scorer || p.team !== pick.team) return 0;
      const where = p.cell === pick.cell ? 1 : near(p.cell, pick.cell) ? nearScore : 0;
      const gap = Math.abs(p.band - pick.band);
      return where * (gap === 0 ? 1 : gap === 1 ? nearScore : 0);
    };
    const others = st.totals.reduce((a, p) => a + p.amount * scoreOf(p), 0);
    return ((st.pot + amount) * (1 - st.rules.houseCut) * amount) / (others + amount);
  }

  function renderPot() {
    const st = view.st;
    $("www-pot").textContent = money(st.pot);
    $("www-players").textContent = String(st.players);
    $("www-mine").textContent = money(st.mine.reduce((a, p) => a + p.amount, 0));
    const ready = view.scorer && view.cell != null && view.band != null;
    const amount = stake();
    $("www-share").textContent = ready && amount > 0
      ? money(shareIfExact(st, { scorer: view.scorer.name, team: view.scorer.team, cell: view.cell, band: view.band }, amount)) : "–";
    $("www-pick").textContent = ready
      ? `Your pick: ${view.scorer.name} · zone ${view.cell} · ${bandLabel(view.band)}. A neighbouring zone or band pays a smaller share.`
      : "Pick a scorer, a zone and a band.";
    $("www-live").replaceChildren(...st.mine.map((p) => {
      const row = el("div", { class: "pool-row is-locked" });
      row.append(el("strong", {}, pickText(p)), el("span", {}, `${money(p.amount)} in the pot`),
        el("small", {}, st.status === "holding" ? "Goal scored: the pot pays when the VAR hold ends" : st.status === "open" ? "Open until kick-off" : "Waiting for the first goal"));
      return row;
    }));
    $("www-history").replaceChildren(...st.history.map((h) => {
      const li = el("li");
      const net = h.payout - h.stake;
      const kind = h.status === "void" ? "v" : h.payout > 0 && net >= 0 ? "w" : "l";
      const text = el("span", {}, h.picks.map(pickText).join("; "));
      text.append(el("small", {}, h.status === "void" && !h.scorer ? `Void: ${h.reason}`
        : `First goal: ${h.scorer}, ${h.minute}', ${h.cells.length ? `zone ${h.cells.join(" & ")}` : "no position"}, ${bandLabel(h.band)}${h.reason ? `. ${h.reason}` : ""}`));
      li.append(el("span", { class: `outcome ${kind}`, "aria-label": { v: "Void", w: "Won", l: "Lost" }[kind] }, kind.toUpperCase()), text,
        el("span", { class: net > 0 ? "net-win" : net < 0 ? "net-lose" : "" }, `${net > 0 ? "+" : ""}${money(net)}`));
      return li;
    }));
  }

  function renderStatus() {
    const st = view.st;
    const phaseBox = $("www-phase");
    phaseBox.className = "round-status";
    const text = {
      open: "Picks are open until kick-off.",
      waiting: st.pot ? "Picks are closed. Waiting for the first goal." : "Who Where When is played before kick-off. Picks are closed for this match.",
      holding: st.holding ? `Goal: ${st.holding.scorer}, ${st.holding.minute}'. The pot pays when the VAR hold ends.` : "",
      settled: st.result?.status === "void" ? `The pot is closed: ${st.result.reason}.` : `The pot is settled: ${st.result?.scorer}, ${st.result?.minute}'.`,
    }[st.status];
    phaseBox.textContent = text || "";
    const box = $("www-status");
    box.className = "round-status error";
    box.hidden = !view.message;
    box.textContent = view.message || "";
    $("www-join").disabled = st.status !== "open";
  }

  function render() {
    renderScorers();
    if (!view.st) return;
    renderGrid();
    renderBands();
    renderPot();
    renderStatus();
  }

  function handle(data) {
    if (data.type === "www_state") {
      view.st = data.state;
      if (data.state.balance != null) $("wallet").textContent = money(data.state.balance);
    } else if (data.type === "www_error") {
      flash(`Sorry, ${data.message}.`);
      return;
    }
    render();
  }

  setInterval(render, 2000);
  return { handle, render };
})();
