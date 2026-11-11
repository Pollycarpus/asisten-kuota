/**
 * File: static/app.js
 * Purpose: Chat UI logic: persona login, send messages, render cards and buttons,
 *          call /api/confirm only from a button tap, show trace and cost meter.
 * D2 component: Chat UI (§2.2)
 * Depends on: our backend API (/api/*)
 */

const state = { sessionId: null };
const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  QUOTA_EXHAUSTED: "Kuota habis",
  FUP_THROTTLED: "Kecepatan dibatasi (FUP)",
  NETWORK_DEGRADED: "Gangguan jaringan di area Anda",
  NO_ISSUE: "Semua normal",
};

/**
 * Create a DOM element with an optional class and text (text is never parsed as HTML).
 * Why: safe rendering of server text (no innerHTML with data).
 * @param {string} tag - element tag.
 * @param {string} [cls] - CSS class.
 * @param {string} [text] - text content.
 * @returns {HTMLElement} the element.
 * Errors: none. Side effects: none.
 */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/**
 * Format a number as Rupiah, e.g. 15000 -> "Rp 15.000".
 * Why: customer-facing amounts in Indonesian format.
 * @param {number} amount - IDR.
 * @returns {string} formatted amount.
 * Errors: none. Side effects: none.
 */
function rupiah(amount) {
  return "Rp " + Number(amount || 0).toLocaleString("id-ID");
}

/**
 * POST JSON to our backend API.
 * Why: one helper for every API call.
 * @param {string} path - API path.
 * @param {object} body - JSON body.
 * @returns {Promise<object>} parsed JSON response.
 * Errors: network errors bubble up to the caller. Side effects: HTTP request.
 */
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return response.json();
}

/**
 * Append a chat bubble.
 * Why: chat bubbles (D4 simple chat UI).
 * @param {string} text - message.
 * @param {"bot"|"user"} who - sender.
 * @returns {void}
 * Errors: none. Side effects: changes the DOM.
 */
function addBubble(text, who) {
  $("chat").appendChild(el("div", "bubble " + who, text));
  $("chat").scrollTop = $("chat").scrollHeight;
}

/**
 * Add a label/value row to a card.
 * Why: keeps card builders short.
 * @param {HTMLElement} card - card element.
 * @param {string} label - left text.
 * @param {string} value - right text.
 * @param {string} [cls] - extra class.
 * @returns {void}
 * Errors: none. Side effects: changes the DOM.
 */
function addRow(card, label, value, cls) {
  const row = el("div", "row" + (cls ? " " + cls : ""));
  row.append(el("span", "", label), el("span", "", value));
  card.appendChild(row);
}

/**
 * Render the diagnosis card.
 * Why: UC1 — show the deterministic diagnosis.
 * @param {object} card - {status, quota, network, incident_ref}.
 * @returns {HTMLElement} card element.
 * Errors: none. Side effects: none.
 */
function diagnosisCard(card) {
  const node = el("div", "card");
  node.appendChild(el("h4", "", "Hasil diagnosis"));
  node.appendChild(el("div", "status", STATUS_LABEL[card.status] || card.status));
  addRow(node, "Sisa kuota internet", (card.quota.internet_remaining_mb / 1024).toFixed(1) + " GB");
  addRow(node, "Status FUP", card.quota.fup_status + (card.quota.fup_speed_kbps ? ` (${card.quota.fup_speed_kbps} kbps)` : ""));
  addRow(node, "Pulsa", rupiah(card.quota.pulsa_idr));
  addRow(node, "Jaringan area Anda", card.network + (card.incident_ref ? ` · ${card.incident_ref}` : ""));
  return node;
}

/**
 * Render the Top-3 package cards with [Pilih paket ini].
 * Why: UC2 — the customer chooses; choosing only asks the agent to prepare a confirmation.
 * @param {object} card - {offers: [...]}.
 * @returns {HTMLElement} card element.
 * Errors: none. Side effects: button sends a chat message.
 */
function offersCard(card) {
  const node = el("div", "card");
  node.appendChild(el("h4", "", "Rekomendasi paket (Top-3)"));
  card.offers.forEach((offer) => {
    const box = el("div", "offer");
    addRow(box, offer.name, rupiah(offer.price_idr));
    const meta = el("div", "muted", `${(offer.quota_mb / 1024).toFixed(0)} GB · ${offer.validity_days} hari · ${offer.offer_id} `);
    meta.appendChild(el("span", offer.affordable ? "badge" : "badge no", offer.affordable ? "pulsa cukup" : "pulsa kurang"));
    box.appendChild(meta);
    const pick = el("button", "", "Pilih paket ini");
    pick.onclick = () => { pick.disabled = true; sendMessage(`Saya pilih paket ${offer.name} (ID: ${offer.offer_id})`); };
    box.appendChild(pick);
    node.appendChild(box);
  });
  return node;
}

/**
 * Render a confirmation card with [Konfirmasi]/[Batal] (purchase) or [Buat laporan]/[Batal] (ticket).
 * Why: AD-4 — the ONLY path to a purchase/ticket is this button; buttons disable after one click.
 * @param {object} card - confirmation details.
 * @returns {HTMLElement} card element.
 * Errors: none. Side effects: buttons call /api/confirm or /api/cancel.
 */
function confirmationCard(card) {
  const node = el("div", "card confirm");
  if (card.kind === "purchase") {
    node.appendChild(el("h4", "", "Konfirmasi pembelian"));
    addRow(node, "Paket", card.offer.name);
    addRow(node, "Harga paket", rupiah(card.price_idr));
    addRow(node, "Pembayaran", card.payment_method === "PAKET_DARURAT" ? "Paket Darurat (pinjaman)" : "Pulsa");
    if (card.payment_method === "PAKET_DARURAT") {
      addRow(node, "Biaya layanan Paket Darurat", rupiah(card.fee_idr));
      addRow(node, "Pelunasan", card.repayment || "-");
    }
    addRow(node, "Total", rupiah(card.total_idr), "total");
  } else {
    node.appendChild(el("h4", "", "Buat laporan gangguan?"));
    addRow(node, "Keluhan", card.symptom);
    addRow(node, "Insiden terkait", card.incident_ref || "-");
  }
  const actions = el("div", "actions");
  const ok = el("button", "primary", card.kind === "purchase" ? "Konfirmasi" : "Buat laporan");
  const cancel = el("button", "", "Batal");
  ok.onclick = () => { ok.disabled = cancel.disabled = true; callAndRender("/api/confirm", { confirmation_id: card.confirmation_id }); };
  cancel.onclick = () => { ok.disabled = cancel.disabled = true; callAndRender("/api/cancel", { confirmation_id: card.confirmation_id }); };
  actions.append(ok, cancel);
  node.appendChild(actions);
  node.appendChild(el("div", "muted", "Berlaku 5 menit. Mengetik \"ya\" tidak memproses pembelian."));
  return node;
}

/**
 * Render a card with a row of buttons.
 * Why: consent, CSAT and handoff-offer cards share this shape.
 * @param {string} title - card title.
 * @param {Array<[string, Function]>} buttons - [label, onClick] pairs.
 * @param {string} [cls] - extra card class.
 * @param {boolean} [highlightFirst=true] - style the first button as primary.
 * @returns {HTMLElement} card element.
 * Errors: none. Side effects: none.
 */
function buttonCard(title, buttons, cls, highlightFirst = true) {
  const node = el("div", "card" + (cls ? " " + cls : ""));
  node.appendChild(el("h4", "", title));
  const actions = el("div", "actions");
  const all = buttons.map(([label, onClick], i) => {
    const button = el("button", i === 0 && highlightFirst ? "primary" : "", label);
    button.onclick = () => { all.forEach((b) => (b.disabled = true)); onClick(); };
    return button;
  });
  actions.append(...all);
  node.appendChild(actions);
  return node;
}

/**
 * Render the handoff card (ref ID + masked summary the human receives).
 * Why: §2.10 / §2.8 — handoff with masked summary + ref ID only.
 * @param {object} card - {ref_id, summary}.
 * @returns {HTMLElement} card element.
 * Errors: none. Side effects: none.
 */
function handoffCard(card) {
  const node = el("div", "card handoff");
  node.appendChild(el("h4", "", "Dialihkan ke petugas · " + card.ref_id));
  node.appendChild(el("div", "muted", "Ringkasan (tersamarkan) yang diterima petugas:"));
  node.appendChild(el("pre", "muted", JSON.stringify(card.summary, null, 2)));
  return node;
}

/**
 * Render any card from the backend.
 * Why: one dispatcher for all card types.
 * @param {object} card - card with a "type" field.
 * @returns {void}
 * Errors: unknown types are ignored. Side effects: changes the DOM.
 */
function renderCard(card) {
  let node = null;
  if (card.type === "diagnosis") node = diagnosisCard(card);
  if (card.type === "offers") node = offersCard(card);
  if (card.type === "confirmation") node = confirmationCard(card);
  if (card.type === "handoff") node = handoffCard(card);
  if (card.type === "result") { node = el("div", "card ok"); node.appendChild(el("div", "", "✓ " + card.text)); }
  if (card.type === "consent") {
    node = buttonCard("Persetujuan pemrosesan data (UU PDP)", [
      ["Setuju", () => callAndRender("/api/consent", { decision: "granted" })],
      ["Tolak", () => callAndRender("/api/consent", { decision: "refused" })],
    ]);
  }
  if (card.type === "handoff_offer") node = buttonCard("Butuh bantuan petugas?", [["Hubungi petugas", () => callAndRender("/api/handoff", {})]]);
  if (card.type === "csat") {
    node = buttonCard("Seberapa puas Anda? (1–5)", [1, 2, 3, 4, 5].map((s) => [String(s), () => callAndRender("/api/csat", { score: s })]), "", false);
  }
  if (node) { $("chat").appendChild(node); $("chat").scrollTop = $("chat").scrollHeight; }
}

/**
 * Render a standard backend response: replies, cards, cost, ended flag.
 * Why: every endpoint returns the same shape.
 * @param {object} data - backend response.
 * @returns {void}
 * Errors: none. Side effects: changes the DOM; refreshes the trace.
 */
function renderResponse(data) {
  if (data.error) { addBubble("Error: " + data.error, "bot"); return; }
  const cardTexts = (data.cards || []).filter((c) => c.type === "result").map((c) => c.text);
  (data.replies || []).filter((text) => !cardTexts.includes(text)).forEach((text) => addBubble(text, "bot"));  // no duplicate of the green result card
  (data.cards || []).forEach(renderCard);
  updateCost(data.cost);
  $("provider").textContent = "LLM: " + data.provider;
  setComposer(!data.ended);
  refreshTrace();
}

/**
 * Call an API action for the current session and render the result.
 * Why: buttons (confirm, cancel, consent, CSAT, handoff) all follow this flow.
 * @param {string} path - API path.
 * @param {object} body - extra JSON fields.
 * @returns {Promise<void>}
 * Errors: shows a bubble on network failure. Side effects: HTTP request; DOM.
 */
async function callAndRender(path, body) {
  try {
    renderResponse(await post(path, { session_id: state.sessionId, ...body }));
  } catch (error) {
    addBubble("Koneksi ke server gagal. Coba lagi.", "bot");
  }
}

/**
 * Send a chat message (typed or from [Pilih paket ini]).
 * Why: UC1–UC5 entry point.
 * @param {string} text - message.
 * @returns {Promise<void>}
 * Errors: shown as a bubble. Side effects: HTTP request; DOM.
 */
async function sendMessage(text) {
  if (!text.trim() || !state.sessionId) return;
  addBubble(text, "user");
  setComposer(false);
  await callAndRender("/api/chat", { message: text });
}

/**
 * Enable or disable the message box.
 * Why: no input after handoff/consent refusal or while a turn is running.
 * @param {boolean} enabled - true to enable.
 * @returns {void}
 * Errors: none. Side effects: DOM.
 */
function setComposer(enabled) {
  $("message").disabled = !enabled;
  $("send").disabled = !enabled;
  if (enabled) $("message").focus();
}

/**
 * Update the cost meter (projected production cost vs warn/cap).
 * Why: §2.9 — show cost in the UI; cap Rp 250.
 * @param {object} cost - {projected_idr, actual_idr, warn_idr, cap_idr, estimated}.
 * @returns {void}
 * Errors: none. Side effects: DOM.
 */
function updateCost(cost) {
  if (!cost) return;
  const percent = Math.min(100, (cost.projected_idr / cost.cap_idr) * 100);
  $("cost-value").textContent = `Rp ${cost.projected_idr.toFixed(2)} / Rp ${cost.cap_idr}`;
  $("cost-actual").textContent = `· aktual Rp ${cost.actual_idr.toFixed(2)}` + (cost.estimated ? " · token diestimasi" : "");
  $("cost-bar").style.width = percent + "%";
  $("cost-bar").className = "meter-fill" + (cost.projected_idr >= cost.cap_idr ? " cap" : cost.projected_idr >= cost.warn_idr ? " warn" : "");
  $("cost-warn").style.left = (cost.warn_idr / cost.cap_idr) * 100 + "%";
}

/**
 * Reload and render the masked trace for the current session.
 * Why: D4 — log/trace of every agent step, visible to judges.
 * @returns {Promise<void>}
 * Errors: ignored (trace is informational). Side effects: HTTP request; DOM.
 */
async function refreshTrace() {
  if (!state.sessionId) return;
  const data = await (await fetch("/api/trace/" + state.sessionId)).json();
  const box = $("trace");
  box.replaceChildren();
  (data.events || []).forEach((event) => {
    const row = el("div", "ev");
    row.append(el("span", "t", `${event.ts} #${event.seq} `), el("span", "ty ty-" + event.type, event.type), el("span", "", " " + event.summary));
    if (event.data && Object.keys(event.data).length) row.appendChild(el("pre", "", JSON.stringify(event.data, null, 2)));
    row.onclick = () => row.classList.toggle("open");
    box.appendChild(row);
  });
  box.scrollTop = box.scrollHeight;
}

/**
 * Start a new session for the selected persona (simulated login).
 * Why: session = persona; MSISDN stays on the server.
 * @returns {Promise<void>}
 * Errors: shown as a bubble. Side effects: HTTP request; DOM reset.
 */
async function startSession() {
  $("chat").replaceChildren();
  $("trace").replaceChildren();
  const data = await post("/api/session", { persona_id: $("persona").value });
  state.sessionId = data.session_id;
  renderResponse(data);
}

/**
 * Initialise the page: load personas and wire up buttons.
 * Why: page entry point.
 * @returns {Promise<void>}
 * Errors: none. Side effects: HTTP request; event listeners.
 */
async function init() {
  const personas = await (await fetch("/api/personas")).json();
  personas.forEach((p) => { const option = el("option", "", p.label); option.value = p.persona_id; $("persona").appendChild(option); });
  $("start").onclick = startSession;
  $("reset").onclick = async () => { await post("/api/reset"); state.sessionId = null; $("chat").replaceChildren(el("div", "hint", "Demo direset. Pilih persona lalu Mulai sesi.")); $("trace").replaceChildren(); setComposer(false); updateCost({ projected_idr: 0, actual_idr: 0, warn_idr: 200, cap_idr: 250 }); };
  $("composer").onsubmit = (event) => { event.preventDefault(); const text = $("message").value; $("message").value = ""; sendMessage(text); };
}

init();
