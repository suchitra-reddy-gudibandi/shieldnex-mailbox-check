(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const input = $("#emails"), runBtn = $("#run"), rowsEl = $("#rows");
  const ICON = {
    delivered: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    bounce: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    unknown: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 6.3a2.1 2.1 0 1 1 3.2 1.8c-.8.5-1.2.9-1.2 1.8M8 12.4v.2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    pending: ""
  };
  const LABEL = { pending: "Queued", delivered: "Delivered", bounce: "Bounce", unknown: "Unknown" };
  let rows = [], filter = "all", cfg = { max_addresses: 100, chunk: 10 }, runId = 0, started = 0;

  function parse(text) {
    const seen = new Set(), out = [];
    for (let t of text.split(/[\s,;]+/)) {
      t = t.replace(/^[<"'(\[]+|[>"')\]]+$/g, "");
      if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); out.push(t); }
    }
    return out;
  }

  function updateCount() {
    const n = parse(input.value).length;
    $("#count").textContent = n ? `${n} address${n === 1 ? "" : "es"} found${n > cfg.max_addresses ? `, only the first ${cfg.max_addresses} will be checked` : ""}` : "0 addresses";
    runBtn.disabled = n === 0;
  }

  function paint(r) {
    const { li, addr, why, sug, chip } = r.el;
    li.dataset.status = r.status; li.dataset.active = r.active ? "true" : "false";
    addr.textContent = r.email; why.textContent = r.reason;
    sug.hidden = !r.suggestion; sug.textContent = r.suggestion ? `Did you mean ${r.suggestion}?` : "";
    chip.className = "chip " + r.status;
    chip.innerHTML = ICON[r.status];
    chip.append(r.active ? "Checking" : LABEL[r.status]);
    li.hidden = !(filter === "all" || r.status === filter);
  }

  function makeRow(email) {
    const r = { email, status: "pending", reason: "Waiting in the queue.", active: false, suggestion: null };
    const li = document.createElement("li"); li.className = "row";
    const left = document.createElement("div");
    const addr = document.createElement("span"); addr.className = "addr";
    const why = document.createElement("span"); why.className = "why";
    const sug = document.createElement("span"); sug.className = "sug";
    left.append(addr, why, sug);
    const chip = document.createElement("span");
    li.append(left, chip);
    r.el = { li, addr, why, sug, chip };
    rowsEl.append(li); paint(r);
    return r;
  }

  function summarize(done) {
    const c = { delivered: 0, bounce: 0, unknown: 0 };
    rows.forEach((r) => { if (c[r.status] !== undefined) c[r.status]++; });
    const finished = c.delivered + c.bounce + c.unknown;
    $("#n-all").textContent = finished;
    for (const k in c) { $("#n-" + k).textContent = c[k]; document.querySelector(`#bar [data-s=${k}]`).style.flexGrow = c[k]; }
    $("#progress").style.width = (rows.length ? (finished / rows.length) * 100 : 0) + "%";
    $("#status").textContent = done
      ? `Done. ${rows.length} checked in ${Math.max(1, Math.round((Date.now() - started) / 1000))} s.`
      : `Checked ${finished} of ${rows.length}…`;
    $("#export").disabled = finished === 0;
  }

  async function run() {
    const list = parse(input.value).slice(0, cfg.max_addresses);
    if (!list.length) return;
    const id = ++runId; started = Date.now();
    rowsEl.replaceChildren(); filter = "all"; setFilter("all");
    rows = list.map(makeRow);
    $("#empty").hidden = true; $("#live").hidden = false; runBtn.disabled = true; runBtn.textContent = "Checking…";
    summarize(false);
    if (matchMedia("(max-width:899px)").matches) $("#results").scrollIntoView({ behavior: "smooth" });

    const size = 5, chunks = [];
    for (let i = 0; i < rows.length; i += size) chunks.push(rows.slice(i, i + size));
    let next = 0;
    const worker = async () => {
      while (next < chunks.length && id === runId) {
        const chunk = chunks[next++];
        chunk.forEach((r) => { r.active = true; r.reason = "Checking format, domain and mailbox."; paint(r); });
        let results = null, error = "Couldn't reach the checker. Check your connection and try again.";
        try {
          const res = await fetch("/api/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ emails: chunk.map((r) => r.email) }) });
          const data = await res.json();
          if (res.ok) results = data.results; else error = data.error || error;
        } catch (e) { /* keep default error */ }
        chunk.forEach((r, j) => {
          Object.assign(r, results ? results[j] : { status: "unknown", reason: error, suggestion: null }, { active: false });
          paint(r);
        });
        summarize(false);
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    if (id === runId) { summarize(true); runBtn.textContent = "Check addresses"; updateCount(); }
  }

  function setFilter(f) {
    filter = f;
    document.querySelectorAll(".f").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === f)));
    rows.forEach(paint);
  }

  function exportCsv() {
    const q = (v) => { v = String(v ?? ""); if (/^[=+\-@\t\r]/.test(v)) v = "'" + v; return '"' + v.replace(/"/g, '""') + '"'; };
    const lines = ["email,result,reason", ...rows.filter((r) => r.status !== "pending").map((r) => [r.email, r.status, r.reason].map(q).join(","))];
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    a.download = "mailbox-check-results.csv"; a.click(); URL.revokeObjectURL(a.href);
  }

  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !runBtn.disabled) run(); });
  runBtn.addEventListener("click", run);
  $("#export").addEventListener("click", exportCsv);
  document.querySelectorAll(".f").forEach((b) => b.addEventListener("click", () => setFilter(b.dataset.f)));
  $("#clear").addEventListener("click", () => { runId++; input.value = ""; rows = []; rowsEl.replaceChildren(); $("#live").hidden = true; $("#empty").hidden = false; runBtn.textContent = "Check addresses"; updateCount(); input.focus(); });
  $("#sample").addEventListener("click", () => {
    input.value = ["hello@example.com", "someone@gmial.com", "missing-at-sign.com", "john..doe@gmail.com", "team@this-domain-does-not-exist-48213.com", "postmaster@gmail.com"].join("\n");
    updateCount(); input.focus();
  });

  fetch("/api/status").then((r) => r.json()).then((s) => {
    cfg = s; $("#max").textContent = s.max_addresses;
    $("#mode").dataset.on = String(s.mailbox_checks);
    $("#mode-text").textContent = s.mailbox_checks
      ? "Mailbox checks are on. Mail servers are asked about each address."
      : "Mailbox checks are off on this host (outgoing mail connections are blocked). Only format, domain and mail server are checked, so working domains show as unknown.";
    updateCount();
  }).catch(() => { $("#mode-text").textContent = "Couldn't read server capabilities."; });
  updateCount();
})();
