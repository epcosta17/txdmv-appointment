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

// -------------------------------------------------------------- day labels

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
  const label = select.closest(".field")?.querySelector("label");
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

// -------------------------------------------------------------------- state

async function refresh() {
  try {
    const state = await api("/api/state");
    applySettings(state.settings);
    syncLanes(state);
    var loaded = state;
    $("conn").textContent = state.settings.use_proxy ? "PROXY" : "DIRECTO";
    $("conn-dot").className = "dot " + (state.settings.use_proxy ? "accent" : "");
    const open = state.lanes.filter((l) => l.session.open).length;
    $("sess").textContent = open ? `${open} sesión${open > 1 ? "es" : ""}` : "sin sesión";
    $("sess-dot").className = "dot " + (open ? "ok" : "");
    return loaded;
  } catch (_) {
    $("conn").textContent = "SIN SERVIDOR";
    $("conn-dot").className = "dot warn";
    return null;
  }
}

function syncLanes(state) {
  const seen = new Set();
  state.lanes.forEach((lane) => {
    seen.add(lane.id);
    let panel = panels.get(lane.id);
    if (!panel) {
      panel = new LanePanel(lane);
      panels.set(lane.id, panel);
      $("lanes").appendChild(panel.root);
    }
    panel.update(lane);
  });
  panels.forEach((panel, id) => {
    if (!seen.has(id)) {
      panel.root.remove();
      panels.delete(id);
    }
  });

  $("lanes").classList.toggle("two", state.lanes.length > 1);
  $("add-lane").disabled = state.lanes.length >= state.max_lanes;
  $("lane-hint").textContent = state.lanes.length
    ? "Cada carril es una oficina con su propia sesión: buscan y reservan en paralelo."
    : "Añade un carril para empezar.";

  // The placeholder is its own element that we show and hide. Writing innerHTML on
  // the container would detach every live panel while `panels` still held them,
  // leaving the Map convinced it had rendered lanes that are no longer in the page.
  $("no-lanes").hidden = state.lanes.length > 0;
}

// -------------------------------------------------------------------- lanes

const panels = new Map();

class LanePanel {
  constructor(lane) {
    this.id = lane.id;
    this.mode = "notify";
    this.firstAvailable = false;
    this.result = null;
    this.resultSeq = -1;
    this.searching = false;
    this.watching = false;

    this.root = $("lane-tpl").content.firstElementChild.cloneNode(true);
    const q = (sel) => this.root.querySelector(sel);
    this.el = {
      office: q(".js-office"), service: q(".js-service"), from: q(".js-from"),
      search: q(".js-search"), close: q(".js-close"), fa: q(".js-fa"),
      session: q(".js-session"), count: q(".js-count"), grid: q(".js-grid"),
      at: q(".js-at"), lat: q(".js-lat"), banner: q(".js-banner"), mode: q(".js-mode"),
      interval: q(".js-interval"), dateTo: q(".js-date-to"),
      timeFrom: q(".js-time-from"), timeTo: q(".js-time-to"),
      watch: q(".js-watch"), log: q(".js-log"),
    };

    this.el.from.value = isoToday();
    fillOffices(this.el.office, lane.office);
    fillServices(this.el.service, lane.office, lane.service);
    [this.el.office, this.el.service, this.el.interval].forEach(enhanceSelect);

    this.el.office.addEventListener("change", () =>
      fillServices(this.el.service, this.el.office.value)
    );
    this.el.search.addEventListener("click", () => this.search());
    this.el.close.addEventListener("click", () => this.close());
    this.el.watch.addEventListener("click", () => this.toggleWatch());
    this.el.mode.addEventListener("click", (e) => {
      const button = e.target.closest("button[data-mode]");
      if (!button) return;
      this.mode = button.dataset.mode;
      this.el.mode.querySelectorAll("button").forEach((b) =>
        b.setAttribute("aria-selected", String(b === button))
      );
    });
    const toggleFa = () => {
      this.firstAvailable = !this.firstAvailable;
      this.el.fa.setAttribute("aria-checked", String(this.firstAvailable));
    };
    this.el.fa.addEventListener("click", toggleFa);
    this.el.fa.addEventListener("keydown", (e) => {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        toggleFa();
      }
    });
  }

  async search() {
    // A second click while one is in flight would leave the button stuck on the
    // first request's finally, and race two grids onto the same panel.
    if (this.searching) return;
    this.searching = true;
    busy(this.el.search, true, "Buscando");
    try {
      const result = await api(`/api/lanes/${this.id}/search`, {
        method: "POST",
        body: JSON.stringify({
          office: this.el.office.value,
          service: this.el.service.value,
          from_date: this.el.from.value || null,
          first_available: this.firstAvailable,
        }),
      });
      this.result = result;
      this.renderGrid(result);
      if (!result.slots.length) toast(`${this.el.office.value}: sin cupos en esa ventana.`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      this.searching = false;
      busy(this.el.search, false, "Buscar");
      refresh();
    }
  }

  async close() {
    if (this.watching && !confirm("Ese carril está vigilando. ¿Cerrarlo igual?")) return;
    try {
      await api(`/api/lanes/${this.id}`, { method: "DELETE" });
      refresh();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async toggleWatch() {
    try {
      if (this.watching) {
        await api(`/api/lanes/${this.id}/watch`, { method: "DELETE" });
        return refresh();
      }
      if (this.mode === "auto") {
        const person = applicant();
        if (["first_name", "last_name", "email", "phone"].some((f) => !person[f])) {
          $("settings-scrim").hidden = false;
          return toast("La reserva automática necesita todos los datos (Ajustes).", true);
        }
        const window = [this.el.timeFrom.value, this.el.timeTo.value].filter(Boolean).join(" – ");
        const ok = confirm(
          `Reservará una cita REAL sin preguntar.\n\n` +
            `Oficina: ${this.el.office.value}\nTrámite: ${this.el.service.value}\n` +
            `Horario permitido: ${window || "cualquiera"}\n` +
            `A nombre de: ${person.first_name} ${person.last_name}\n\n¿Continuar?`
        );
        if (!ok) return;
      }
      await api(`/api/lanes/${this.id}/watch`, {
        method: "POST",
        body: JSON.stringify({
          auto_book: this.mode === "auto",
          interval: Number(this.el.interval.value),
          date_from: this.el.from.value || null,
          date_to: this.el.dateTo.value || null,
          time_from: this.el.timeFrom.value || null,
          time_to: this.el.timeTo.value || null,
          applicant: applicant(),
        }),
      });
      refresh();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async update(lane) {
    const watch = lane.watch;
    this.watching = watch.state === "watching";
    this.office = lane.office;

    this.el.session.textContent = lane.session.open
      ? `sesión viva · expira en ${Math.floor(lane.session.expires_in / 60)} min`
      : "sin sesión abierta";

    this.el.watch.textContent = this.watching ? "Detener" : "Vigilar";
    this.el.watch.classList.toggle("danger", this.watching);
    this.root.classList.toggle("armed", this.watching && watch.auto_book);
    [this.el.office, this.el.service, this.el.interval].forEach((select) =>
      select.closest(".field")?.classList.toggle("locked", this.watching)
    );

    this.el.banner.innerHTML = bannerFor(watch, this.el.timeFrom.value, this.el.timeTo.value);
    this.el.log.innerHTML = watch.log
      .slice()
      .reverse()
      .map((e) => `<div><time>${e.at}</time><span class="${e.level}">${e.text}</span></div>`)
      .join("");

    if (watch.result_seq !== undefined && watch.result_seq !== this.resultSeq) {
      this.resultSeq = watch.result_seq;
      if (watch.result_seq > 0) {
        const result = await api(`/api/lanes/${this.id}/result`);
        if (result.timetable?.length) {
          this.result = result;
          this.renderGrid(result, watch.found || []);
        }
      }
    }

    if (watch.state === "booked" && watch.booking && $("done-scrim").hidden) {
      showDone(watch.booking);
      toast(`${lane.office}: la vigilancia reservó una cita.`);
    }
  }

  renderGrid(result, highlight = []) {
    const hits = new Set(highlight.map((s) => s.raw));
    this.el.count.textContent = result.slots.length;
    this.el.at.textContent = result.at || "—";
    this.el.lat.textContent = result.latency ? `${result.latency} s` : "—";

    const columns = result.timetable || [];
    if (!columns.length) {
      this.el.grid.innerHTML =
        '<div class="empty-state"><b>Sin horarios</b>El portal no devolvió calendario.</div>';
      return;
    }

    this.el.grid.innerHTML = columns
      .map((column) => {
        const { weekday, date, isToday } = readTitle(column.title);
        const head = `
          <div class="col-head">
            <div class="day">${weekday}${isToday ? '<span class="tag-today">HOY</span>' : ""}</div>
            <div class="date">${date}</div>
            <div class="n${column.available ? "" : " zero"}">${column.available} libres</div>
          </div>`;
        const body = column.closed
          ? '<div class="cell shut">Cerrado</div>'
          : column.cells
              .map((cell) =>
                cell.state === "free"
                  ? `<button class="cell free${hits.has(cell.raw) ? " hit" : ""}" data-raw="${cell.raw}" aria-pressed="false">${cell.time}</button>`
                  : '<div class="cell busy">Ocupado</div>'
              )
              .join("");
        return `<div class="col">${head}${body}</div>`;
      })
      .join("");

    this.el.grid.querySelectorAll(".cell.free").forEach((cell) =>
      cell.addEventListener("click", () => openConfirm(this, cell.dataset.raw))
    );
  }
}

function bannerFor(watch, from, to) {
  if (watch.state === "watching" && watch.auto_book) {
    const window = [from, to].filter(Boolean).join(" y ");
    return `<div class="banner"><span class="dot warn live"></span>VIGILANDO — reservará el primer cupo${
      window ? ` entre ${window}` : ""
    }</div>`;
  }
  if (watch.state === "watching") {
    return `<div class="banner ok"><span class="dot ok live"></span>Vigilando${
      watch.next_in ? ` — reintenta en ${watch.next_in} s` : ""
    }</div>`;
  }
  if (watch.state === "booked" && watch.booking) {
    return `<div class="banner ok">Cita ${watch.booking.appointment_number} reservada</div>`;
  }
  if (watch.state === "found") return '<div class="banner ok">Encontró cupo — mira la grilla</div>';
  if (watch.state === "error") return '<div class="banner">Detenida por errores</div>';
  return "";
}

// ------------------------------------------------------------------ pickers

let OFFICES = [];
const SERVICE_CACHE = new Map();

function fillOffices(select, selected) {
  select.innerHTML = OFFICES.length
    ? OFFICES.map((o) => `<option${o.name === selected ? " selected" : ""}>${o.name}</option>`).join("")
    : "<option>cargando…</option>";
}

async function fillServices(select, office, selected) {
  const cached = SERVICE_CACHE.get(office);
  const paint = (list) => {
    select.innerHTML = list.map((s) => `<option>${s.name}</option>`).join("");
    const want = list.find((s) => s.name === (selected || "Title Companies and Runners"));
    if (want) select.value = want.name;
    select.dispatchEvent(new Event("change", { bubbles: false }));
  };
  if (cached) return paint(cached);

  select.innerHTML = "<option>cargando…</option>";
  try {
    const { services } = await api(`/api/services?office=${encodeURIComponent(office)}`);
    SERVICE_CACHE.set(office, services);
    paint(services);
  } catch (error) {
    select.innerHTML = "<option>error</option>";
    toast(error.message, true);
  }
}

async function loadOffices() {
  try {
    OFFICES = (await api("/api/offices")).offices;
    panels.forEach((p) => fillOffices(p.el.office, p.office));
  } catch (error) {
    toast(error.message, true);
  }
}

async function addLane() {
  try {
    const office = OFFICES.length ? OFFICES[8]?.name || OFFICES[0].name : "Houston North";
    await api("/api/lanes", {
      method: "POST",
      body: JSON.stringify({ office, service: "Title Companies and Runners" }),
    });
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

// ---------------------------------------------------------------- booking

let pending = null;

function summaryRows(rows) {
  return rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v || "—"}</dd></div>`).join("");
}

function openConfirm(panel, raw) {
  const slot = (panel.result?.slots || []).find((s) => s.raw === raw);
  if (!slot) return toast("Ese horario ya no está en la lista. Busca de nuevo.", true);

  const person = applicant();
  if (["first_name", "last_name", "email", "phone"].some((f) => !person[f])) {
    $("settings-scrim").hidden = false;
    return toast("Completa los datos del solicitante en Ajustes.", true);
  }

  pending = { panel, slot };
  panel.el.grid.querySelectorAll(".cell.free").forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.raw === raw))
  );
  $("cf-summary").innerHTML = summaryRows([
    ["Horario", slot.label || `${slot.date} ${slot.time}`],
    ["Oficina", panel.el.office.value],
    ["Trámite", panel.el.service.value],
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
    const details = await api(`/api/lanes/${pending.panel.id}/book`, {
      method: "POST",
      body: JSON.stringify({ raw: pending.slot.raw, applicant: applicant() }),
    });
    $("confirm-scrim").hidden = true;
    showDone(details);
  } catch (error) {
    toast(error.message, true);
  } finally {
    busy(button, false, "Reservar ahora");
    refresh();
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
    refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

// ------------------------------------------------------------------- wiring

async function init() {
  loadApplicant();
  APPLICANT_FIELDS.forEach((f) => $(f).addEventListener("change", saveApplicant));

  $("add-lane").addEventListener("click", addLane);
  $("settings-btn").addEventListener("click", () => ($("settings-scrim").hidden = false));
  $("st-close").addEventListener("click", () => ($("settings-scrim").hidden = true));
  $("st-save").addEventListener("click", saveSettings);
  $("cf-cancel").addEventListener("click", () => ($("confirm-scrim").hidden = true));
  $("cf-ok").addEventListener("click", confirmBooking);
  $("dn-close").addEventListener("click", () => ($("done-scrim").hidden = true));
  $("dn-copy").addEventListener("click", () =>
    navigator.clipboard?.writeText($("dn-number").textContent).then(() => toast("Número copiado."))
  );

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

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input,select,textarea")) return;
    if (e.key === "s") $("settings-scrim").hidden = false;
    if (e.key === "n") addLane();
    if (e.key === "Escape") {
      ["confirm-scrim", "done-scrim", "settings-scrim"].forEach((id) => ($(id).hidden = true));
    }
  });

  await loadOffices();
  const state = await refresh();
  if (state && !state.lanes.length) await addLane();

  setInterval(refresh, 2500);
  setInterval(() => fetch("/api/keepalive", { method: "POST" }).catch(() => {}), 5 * 60 * 1000);
}

document.addEventListener("DOMContentLoaded", init);
