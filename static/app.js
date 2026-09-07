"use strict";

// Talks to the local FastAPI server, which owns the one live wizard session.
// Picking a time continues that session rather than restarting the flow.

const $ = (id) => document.getElementById(id);
const STORE_KEY = "txdmv.applicant";
const APPLICANT_FIELDS = ["first_name", "last_name", "email", "phone", "description", "details"];

let lastSearch = null;
let selected = null;
let firstAvailable = false;
let mode = "notify";
let watchTimer = null;
let useProxy = false;
let proxyConfigured = false;
let lastResultSeq = -1;

// ---------------------------------------------------------------- utilities

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Error ${response.status}`);
  return body;
}

let toastTimer = null;
function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 7000 : 3500);
}

function busy(button, on, label) {
  button.disabled = on;
  button.innerHTML = on ? `<span class="spin"></span>${label}` : label;
}

const isoToday = () => new Date().toISOString().slice(0, 10);

function applicant() {
  return Object.fromEntries(APPLICANT_FIELDS.map((f) => [f, $(f).value.trim()]));
}

function saveApplicant() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(applicant()));
  } catch (_) {}
}

function loadApplicant() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    APPLICANT_FIELDS.forEach((f) => {
      if (saved[f]) $(f).value = saved[f];
    });
  } catch (_) {}
}

// --------------------------------------------------------------- dropdowns

// The native <select> stays in the DOM as the source of truth - `.value` and the
// "change" event keep working - and this draws a styled listbox over it.
const CHEVRON =
  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
  'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6.5 8 10.5 12 6.5"/></svg>';

function enhanceSelect(select) {
  const wrap = document.createElement("div");
  wrap.className = "dd";
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);
  select.setAttribute("tabindex", "-1");
  select.setAttribute("aria-hidden", "true");

  const button = document.createElement("button");
  button.type = "button";
  button.className = "dd-btn";
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  const label = document.querySelector(`label[for="${select.id}"]`);
  if (label) button.setAttribute("aria-label", label.textContent);

  const menu = document.createElement("div");
  menu.className = "dd-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;

  wrap.append(button, menu);

  let active = -1;
  const options = () => Array.from(select.options);

  function paint() {
    const current = select.selectedIndex;
    button.innerHTML = `<span>${select.options[current]?.text || "—"}</span>${CHEVRON}`;
    menu.innerHTML = options()
      .map(
        (o, i) =>
          `<div class="dd-opt" role="option" data-i="${i}" aria-selected="${i === current}">${o.text}</div>`
      )
      .join("");
  }

  function open(state) {
    wrap.classList.toggle("open", state);
    menu.hidden = !state;
    button.setAttribute("aria-expanded", String(state));
    if (state) {
      active = select.selectedIndex;
      highlight();
      menu.querySelector(".dd-opt.active")?.scrollIntoView({ block: "nearest" });
    }
  }

  function highlight() {
    menu.querySelectorAll(".dd-opt").forEach((el, i) => el.classList.toggle("active", i === active));
  }

  function choose(index) {
    if (index < 0 || index >= select.options.length) return;
    select.selectedIndex = index;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    paint();
    open(false);
    button.focus();
  }

  button.addEventListener("click", () => open(menu.hidden));
  menu.addEventListener("click", (e) => {
    const option = e.target.closest(".dd-opt");
    if (option) choose(Number(option.dataset.i));
  });
  menu.addEventListener("mousemove", (e) => {
    const option = e.target.closest(".dd-opt");
    if (option) {
      active = Number(option.dataset.i);
      highlight();
    }
  });

  button.addEventListener("keydown", (e) => {
    const isOpen = !menu.hidden;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!isOpen) return open(true);
      active = Math.min(select.options.length - 1, Math.max(0, active + (e.key === "ArrowDown" ? 1 : -1)));
      highlight();
      menu.querySelector(".dd-opt.active")?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      isOpen ? choose(active) : open(true);
    } else if (e.key === "Escape") {
      open(false);
    } else if (e.key.length === 1 && /\S/.test(e.key)) {
      const from = options().findIndex((o, i) => i > active && o.text.toLowerCase().startsWith(e.key.toLowerCase()));
      const index = from >= 0 ? from : options().findIndex((o) => o.text.toLowerCase().startsWith(e.key.toLowerCase()));
      if (index >= 0) {
        if (isOpen) {
          active = index;
          highlight();
          menu.querySelector(".dd-opt.active")?.scrollIntoView({ block: "nearest" });
        } else choose(index);
      }
    }
  });

  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) open(false);
  });

  // Options are replaced asynchronously (offices, then services per office).
  new MutationObserver(paint).observe(select, { childList: true });
  paint();
}

// ------------------------------------------------------------------ header

async function refreshState() {
  try {
    const { session, settings } = await api("/api/state");
    $("conn").textContent = session.direct ? "DIRECTO" : "PROXY";
    $("conn-dot").className = "dot " + (session.direct ? "" : "accent");
    if (settings) applySettings(settings);
    if (session.open) {
      const mins = String(Math.floor(session.expires_in / 60)).padStart(2, "0");
      const secs = String(session.expires_in % 60).padStart(2, "0");
      $("sess").textContent = `SESIÓN ${mins}:${secs}`;
      $("sess-dot").className = "dot ok";
    } else {
      $("sess").textContent = "sin sesión";
      $("sess-dot").className = "dot";
    }
  } catch (_) {
    $("conn").textContent = "SIN SERVIDOR";
    $("conn-dot").className = "dot warn";
  }
}

// ----------------------------------------------------------------- pickers

async function loadOffices() {
  const select = $("office");
  select.innerHTML = `<option>cargando…</option>`;
  try {
    const { offices } = await api("/api/offices");
    select.innerHTML = offices
      .map((o) => `<option value="${o.name}"${o.name === "Houston North" ? " selected" : ""}>${o.name}</option>`)
      .join("");
    await loadServices();
  } catch (error) {
    select.innerHTML = `<option>error</option>`;
    toast(error.message, true);
  }
}

async function loadServices() {
  const select = $("service");
  select.innerHTML = `<option>cargando…</option>`;
  try {
    const { services } = await api(`/api/services?office=${encodeURIComponent($("office").value)}`);
    select.innerHTML = services.map((s) => `<option value="${s.name}">${s.name}</option>`).join("");
    const preferred = services.find((s) => s.name === "Title Companies and Runners");
    if (preferred) select.value = preferred.name;
  } catch (error) {
    select.innerHTML = `<option>error</option>`;
    toast(error.message, true);
  }
}

// ------------------------------------------------------------------- grid

// The portal labels its columns in English ("Sunday 6 September"); the UI is Spanish.
const WEEKDAYS = {sunday:"DOM", monday:"LUN", tuesday:"MAR", wednesday:"MIÉ",
                  thursday:"JUE", friday:"VIE", saturday:"SÁB"};
const MONTHS = {january:"ene", february:"feb", march:"mar", april:"abr", may:"may",
                june:"jun", july:"jul", august:"ago", september:"sep", october:"oct",
                november:"nov", december:"dic"};
const MONTH_INDEX = Object.keys(MONTHS);

function readTitle(title) {
  const [weekday, day, month] = (title || "").trim().split(/\s+/);
  const monthKey = (month || "").toLowerCase();
  const today = new Date();
  return {
    weekday: WEEKDAYS[(weekday || "").toLowerCase()] || (weekday || "").slice(0, 3).toUpperCase(),
    date: day && monthKey ? `${day} ${MONTHS[monthKey] || monthKey}` : "—",
    isToday:
      Number(day) === today.getDate() && MONTH_INDEX.indexOf(monthKey) === today.getMonth(),
  };
}

function renderGrid(result, highlight = []) {
  const hits = new Set(highlight.map((s) => s.raw));
  const grid = $("grid");
  const columns = result.timetable || [];
  $("slot-count").textContent = result.slots.length;
  $("m-at").textContent = result.at || "—";
  $("m-lat").textContent = result.latency ? `${result.latency} s` : "—";

  if (!columns.length) {
    grid.innerHTML = `<div class="empty-state"><b>Sin horarios</b>El portal no devolvió calendario para esa búsqueda.</div>`;
    return;
  }

  grid.innerHTML = columns
    .map((column) => {
      const { weekday, date, isToday } = readTitle(column.title);
      const head = `
        <div class="col-head">
          <div class="day">${weekday}${isToday ? '<span class="tag-today">HOY</span>' : ""}</div>
          <div class="date">${date}</div>
          <div class="n${column.available ? "" : " zero"}">${column.available} libres</div>
        </div>`;

      const body = column.closed
        ? `<div class="cell shut">Cerrado</div>`
        : column.cells
            .map((cell) =>
              cell.state === "free"
                ? `<button class="cell free${hits.has(cell.raw) ? " hit" : ""}" data-raw="${cell.raw}" aria-pressed="false">${cell.time}</button>`
                : `<div class="cell busy">Ocupado</div>`
            )
            .join("");

      return `<div class="col">${head}${body}</div>`;
    })
    .join("");

  grid.querySelectorAll(".cell.free").forEach((cell) => {
    cell.addEventListener("click", () => openConfirm(cell.dataset.raw));
  });
}

async function search() {
  const button = $("search-btn");
  busy(button, true, "Buscando");
  try {
    lastSearch = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        office: $("office").value,
        service: $("service").value,
        from_date: $("from-date").value || null,
        first_available: firstAvailable,
      }),
    });
    renderGrid(lastSearch);
    if (!lastSearch.slots.length) toast("Sin cupos en esa ventana.");
    refreshState();
  } catch (error) {
    toast(error.message, true);
  } finally {
    busy(button, false, "Buscar");
  }
}

// ---------------------------------------------------------------- booking

function summaryRows(rows) {
  return rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v || "—"}</dd></div>`).join("");
}

function openConfirm(raw) {
  const slot = (lastSearch?.slots || []).find((s) => s.raw === raw);
  if (!slot) return toast("Ese horario ya no está en la lista. Busca de nuevo.", true);

  const person = applicant();
  const missing = ["first_name", "last_name", "email", "phone"].filter((f) => !person[f]);
  if (missing.length) {
    $("settings-scrim").hidden = false;
    return toast("Completa los datos del solicitante en Ajustes.", true);
  }

  selected = slot;
  document.querySelectorAll(".cell.free").forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.raw === raw))
  );
  $("cf-summary").innerHTML = summaryRows([
    ["Horario", slot.label || `${slot.date} ${slot.time}`],
    ["Oficina", $("office").value],
    ["Trámite", $("service").value],
    ["Nombre", `${person.first_name} ${person.last_name}`],
    ["Correo", person.email],
    ["Teléfono", person.phone],
  ]);
  $("confirm-scrim").hidden = false;
}

async function confirmBooking() {
  const button = $("cf-ok");
  busy(button, true, "Reservando");
  try {
    const details = await api("/api/book", {
      method: "POST",
      body: JSON.stringify({ raw: selected.raw, applicant: applicant() }),
    });
    $("confirm-scrim").hidden = true;
    showDone(details);
  } catch (error) {
    toast(error.message, true);
  } finally {
    busy(button, false, "Reservar ahora");
  }
}

function showDone(details) {
  $("dn-number").textContent = details.appointment_number || "—";
  $("dn-summary").innerHTML = summaryRows([
    ["Horario", details.time],
    ["Oficina", details.office],
    ["Dependencia", "Texas Department of Motor Vehicles"],
    ["Personas", "1"],
  ]);
  $("done-scrim").hidden = false;
  refreshState();
}

// ---------------------------------------------------------------- watching

function watchConfig() {
  return {
    office: $("office").value,
    service: $("service").value,
    auto_book: mode === "auto",
    interval: Number($("w-interval").value),
    date_from: $("w-date-from").value || null,
    date_to: $("w-date-to").value || null,
    time_from: $("w-time-from").value || null,
    time_to: $("w-time-to").value || null,
    first_available: firstAvailable,
    applicant: applicant(),
  };
}

function renderWatch(status) {
  const card = $("watch-card");
  const button = $("watch-btn");
  const running = status.state === "watching";

  card.classList.toggle("armed", running && status.auto_book);
  button.textContent = running ? "Detener vigilancia" : "Iniciar vigilancia";
  button.classList.toggle("danger", running);

  const banner = $("watch-banner");
  if (running && status.auto_book) {
    const window = [$("w-time-from").value, $("w-time-to").value].filter(Boolean).join(" y ");
    banner.innerHTML = `<div class="banner"><span class="dot warn live"></span>VIGILANDO — reservará automáticamente el primer cupo${
      window ? ` entre ${window}` : ""
    }</div>`;
  } else if (running) {
    banner.innerHTML = `<div class="banner ok"><span class="dot ok live"></span>Vigilando — solo avisará${
      status.next_in ? `, reintenta en ${status.next_in} s` : ""
    }</div>`;
  } else if (status.state === "booked" && status.booking) {
    banner.innerHTML = `<div class="banner ok">Cita ${status.booking.appointment_number} reservada</div>`;
  } else if (status.state === "found") {
    banner.innerHTML = `<div class="banner ok">Encontró cupo — revisa la grilla</div>`;
  } else if (status.state === "error") {
    banner.innerHTML = `<div class="banner">Vigilancia detenida por errores</div>`;
  } else {
    banner.innerHTML = "";
  }

  $("watch-log").innerHTML = status.log
    .slice()
    .reverse()
    .map((e) => `<div><time>${e.at}</time><span class="${e.level}">${e.text}</span></div>`)
    .join("");

  if (running && !watchTimer) watchTimer = setInterval(pollWatch, 2000);
  if (!running && watchTimer) {
    clearInterval(watchTimer);
    watchTimer = null;
  }
}

async function pollWatch() {
  try {
    const status = await api("/api/watch/status");
    renderWatch(status);

    // Show the grid the watcher itself pulled - no second search, so nothing is
    // missed between its poll and ours.
    if (status.result_seq !== undefined && status.result_seq !== lastResultSeq) {
      lastResultSeq = status.result_seq;
      if (status.result_seq > 0) {
        const result = await api("/api/watch/result");
        if (result.timetable?.length) {
          lastSearch = result;
          renderGrid(result, status.found || []);
        }
      }
    }

    if (status.state === "found" && status.found?.length) {
      const hit = status.found[0];
      toast(`Cupo encontrado: ${hit.label || hit.raw}`);
    }
    if (status.state === "booked" && status.booking && $("done-scrim").hidden) {
      showDone(status.booking);
      toast("La vigilancia reservó una cita.");
    }
  } catch (_) {}
}

async function toggleWatch() {
  const button = $("watch-btn");
  const running = button.textContent.startsWith("Detener");
  try {
    if (running) {
      renderWatch(await api("/api/watch/stop", { method: "POST" }));
      return;
    }
    if (mode === "auto") {
      const person = applicant();
      const missing = ["first_name", "last_name", "email", "phone"].filter((f) => !person[f]);
      if (missing.length) {
        $("settings-scrim").hidden = false;
        return toast("La reserva automática necesita todos los datos (Ajustes).", true);
      }
      const window = [$("w-time-from").value, $("w-time-to").value].filter(Boolean).join(" – ");
      const ok = confirm(
        `Reservará una cita REAL sin preguntar.\n\n` +
          `Oficina: ${$("office").value}\nTrámite: ${$("service").value}\n` +
          `Horario permitido: ${window || "cualquiera"}\nA nombre de: ${person.first_name} ${person.last_name}\n\n` +
          `¿Continuar?`
      );
      if (!ok) return;
    }
    renderWatch(
      await api("/api/watch/start", { method: "POST", body: JSON.stringify(watchConfig()) })
    );
  } catch (error) {
    toast(error.message, true);
  }
}

// ------------------------------------------------------------------ settings

function applySettings(settings) {
  useProxy = !!settings.use_proxy;
  proxyConfigured = !!settings.proxy_configured;
  const sw = $("proxy-switch");
  sw.setAttribute("aria-checked", String(useProxy));
  sw.classList.toggle("disabled", !proxyConfigured);
  $("proxy-note").textContent = !proxyConfigured
    ? "No hay proxy configurado en .env — se usa la conexión directa."
    : useProxy
    ? "Activado. El portal se abre a través de una IP de Estados Unidos."
    : "Desactivado. El portal se abre directo desde tu conexión.";
}

async function saveSettings() {
  saveApplicant();
  try {
    applySettings(
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ use_proxy: useProxy }),
      })
    );
    toast("Ajustes guardados.");
    $("settings-scrim").hidden = true;
    refreshState();
  } catch (error) {
    toast(error.message, true);
  }
}

// ------------------------------------------------------------------- wiring

function init() {
  ["office", "service", "w-interval"].forEach((id) => enhanceSelect($(id)));
  $("from-date").value = isoToday();
  $("w-date-from").value = isoToday();
  loadApplicant();
  APPLICANT_FIELDS.forEach((f) => $(f).addEventListener("change", saveApplicant));

  $("search-btn").addEventListener("click", search);
  $("settings-btn").addEventListener("click", () => ($("settings-scrim").hidden = false));
  $("st-close").addEventListener("click", () => ($("settings-scrim").hidden = true));
  $("st-save").addEventListener("click", saveSettings);

  const proxySwitch = $("proxy-switch");
  const toggleProxy = () => {
    if (!proxyConfigured) return toast("Añade tus credenciales de Webshare al .env primero.", true);
    useProxy = !useProxy;
    applySettings({ use_proxy: useProxy, proxy_configured: proxyConfigured });
  };
  proxySwitch.addEventListener("click", toggleProxy);
  proxySwitch.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      toggleProxy();
    }
  });
  $("office").addEventListener("change", loadServices);
  $("watch-btn").addEventListener("click", toggleWatch);
  $("cf-cancel").addEventListener("click", () => ($("confirm-scrim").hidden = true));
  $("cf-ok").addEventListener("click", confirmBooking);
  $("dn-close").addEventListener("click", () => {
    $("done-scrim").hidden = true;
    search();
  });
  $("dn-copy").addEventListener("click", () => {
    navigator.clipboard?.writeText($("dn-number").textContent).then(() => toast("Número copiado."));
  });

  const fa = $("fa-switch");
  const toggleFa = () => {
    firstAvailable = !firstAvailable;
    fa.setAttribute("aria-checked", String(firstAvailable));
  };
  fa.addEventListener("click", toggleFa);
  fa.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      toggleFa();
    }
  });

  $("mode-seg").addEventListener("click", (e) => {
    const button = e.target.closest("button[data-mode]");
    if (!button) return;
    mode = button.dataset.mode;
    $("mode-seg")
      .querySelectorAll("button")
      .forEach((b) => b.setAttribute("aria-selected", String(b === button)));
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input,select,textarea")) return;
    if (e.key === "r") search();
    if (e.key === "v") toggleWatch();
    if (e.key === "s") $("settings-scrim").hidden = false;
    if (e.key === "Escape") {
      $("confirm-scrim").hidden = true;
      $("done-scrim").hidden = true;
      $("settings-scrim").hidden = true;
    }
  });

  loadOffices();
  refreshState();
  pollWatch();
  setInterval(refreshState, 5000);
  setInterval(() => fetch("/api/keepalive", { method: "POST" }).catch(() => {}), 5 * 60 * 1000);
}

document.addEventListener("DOMContentLoaded", init);
