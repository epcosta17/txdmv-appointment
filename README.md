# TxDMV appointment booker

Drives the NemoQ wizard at `nqa3.nemoqappointment.com` over plain HTTP requests -
no browser. Replays the same Post/Redirect/Get flow the site's own UI performs.

```bash
pip install -r requirements.txt
cp .env.example .env      # add your Webshare credentials
```

## La app (lo normal)

```bash
./run.fish          # macOS / Linux
run.bat             # Windows: doble clic
```

Levanta un servidor local y abre `http://127.0.0.1:8765/` en el navegador. Necesita
Python 3.10+; la primera vez crea el entorno e instala las dependencias solo.

- **Carriles** — dos fijos, Houston North y Houston South, lado a lado. No se
  añaden ni se cierran. Cada uno tiene su **propia sesión del portal** (y su propia
  IP si el proxy está encendido), así que buscan y reservan en paralelo sin
  pisarse. Con una sesión compartida se estorbarían: el wizard es un flujo con
  estado, y la búsqueda de un carril reemplazaría la página desde la que el otro
  va a reservar. Puedes cambiar la oficina o el trámite de cualquiera de los dos.
- **Buscar** — elige oficina y trámite (se leen en vivo del portal) y pulsa Buscar.
  La grilla muestra la semana igual que el sitio: verde libre, gris ocupado, bloque
  cerrado. Clic en un horario → confirmar → reservado, en la sesión de ese carril.
- **Ajustes** (engrane, o tecla `s`) — datos del solicitante, que se guardan en este
  equipo y se leen desde ahí en cada reserva; y el interruptor del proxy, **apagado
  por defecto**.
- **Vigilancia** — cada carril vigila por su cuenta: reconsulta cada X segundos y
  avisa cuando aparece un hueco. La grilla se pinta con lo que vio el vigilante, sin
  gastar una segunda búsqueda. En *Reservar sola* toma el primer cupo que aparezca y
  se detiene; hay que armarlo a propósito y confirmar, nunca arranca así. Los dos
  carriles pueden vigilar y reservar al mismo tiempo.

  El único filtro es **desde qué hora**: aunque la ventana sean tres días, una
  cita a las 9:00 no sirve si no llegas antes de las 10. Vacío significa cualquier
  hora. Acotar por fecha o por hora tope no aportaba dentro de una lista de tres
  días, así que no está en la interfaz; el watcher sigue aceptándolos por API
  (`date_from`, `date_to`, `time_to`).
- Atajos: `s` ajustes, `Esc` cerrar.

La sesión del portal se mantiene viva entre peticiones, igual que en el CLI: elegir
un horario continúa el flujo en vez de reiniciarlo, así que cada búsqueda extra
cuesta un request en vez de cinco.

## El CLI

```bash
# what offices exist
python3 nemoq_booking.py --list-offices

# what is free at Houston North, book nothing
python3 nemoq_booking.py --list-slots

# pick interactively, then book
python3 nemoq_booking.py --menu --run \
  --first-name Er --last-name Lol --email you@example.com --phone 2312312333

# book a specific slot
python3 nemoq_booking.py --date 2026-09-08 --time 09:00 --run \
  --first-name Er --last-name Lol --email you@example.com --phone 2312312333

# everything except the reservation
python3 nemoq_booking.py --dry-run
```

Nothing is booked without `--run`.

## Choosing the slot

Three ways, checked in this order:

1. `--menu` - prints the available slots and asks which one.
2. `--date` / `--time` - an exact match. Either alone works (`--date` takes the
   earliest slot that day; `--time` the earliest day offering it). If the slot
   is not free the run stops and lists what actually was - it never books a
   nearby time instead.
3. Neither - the first slot the calendar shows.

## The search window

The site only renders *"Current Day and Next 2 Days"*, and the current day is
never bookable - so the default search starts from today and shows exactly the
bookable window (tomorrow and the day after).

To look past it, `--first-available` presses the site's own *See First
Available* button, which jumps to the next day that has openings:

```bash
python3 nemoq_booking.py --first-available --list-slots
```

That button needs a concrete date and a preceding plain search - pressed cold,
or with an empty date, it renders a page with no calendar at all. `find_slots`
handles both, which is also why the captured browser trace searched twice.

## Offices and appointment types

Both are given by name and resolved against the live dropdowns:

```bash
python3 nemoq_booking.py --list-offices
python3 nemoq_booking.py --office "Houston South" --service "All other transactions" --list-slots
```

Appointment-type ids are **scoped to the office**. "Title Companies and
Runners" is `3784` at Houston South but `1275` at Houston North, and the two
offices do not even offer the same queues - so ids are looked up per office at
runtime instead of being hardcoded.

## Proxies

**Off by default** - from a US connection (Houston) the portal is reachable
directly, which is the normal case. Turn it on in Ajustes, or with `--proxy` /
`WEBSHARE_*` for the CLI.

It matters from outside the US: **the site 403s non-US addresses outright**,
every request including the homepage. That is the only reason this exists.

The wizard's ASP.NET session is pinned to the IP that started it. So the proxy
is resolved and verified **once**, before step 1, and then used for every
request of the run; rotating mid-flow would drop the session and lose the held
slot. If a proxy dies before a slot is reserved the whole flow restarts on a
fresh one (nothing is held yet, so it costs nothing); after `reserve()` a dead
proxy is a hard failure.

Residential plans are supported: they answer the API with `proxy_address: null`
and are reached through the `p.webshare.io` gateway, where the exit is chosen by
the country suffix on the username (`user-US-7`) plus the port. Each such entry
is a **sticky** IP - verified here to stay put across requests - which is what
lets one entry carry a whole booking session. The plan mode (`direct` for
datacenter, `backbone` for residential) is detected automatically.

Configure it in `.env` (see `.env.example`), or:

```bash
python3 nemoq_booking.py --proxy 1.2.3.4:8080:user:pass --list-slots
python3 nemoq_booking.py --no-proxy --list-slots
```

Sources are tried in order: `--proxy`, `proxies.txt` (the Webshare download
format, `host:port:user:pass`), `WEBSHARE_API_KEY`, then `WEBSHARE_PROXY_*`.
Candidates that fail a live country check are skipped before the flow starts.

## Layout

| file | role |
|---|---|
| `nemoq_booking.py` | HTTP client for the wizard, plus the CLI |
| `nemoq_parsing.py` | pure HTML -> data (slots, dropdowns, tokens, confirmation) |
| `slot_picker.py` | menu / flags / default selection |
| `webshare.py` | proxy loading, country filter, verification |
| `app.py` | FastAPI: the local server and its JSON API |
| `wizard_session.py` | the live portal session shared by the UI |
| `lanes.py` | independent lanes: one session + watcher each |
| `watcher.py` | polling and guarded auto-booking |
| `static/` | the page (plain HTML/CSS/JS, no build step) |
| `design.pen` | the Halo design the UI was built from |
| `fixtures/` | real captured pages, used as test fixtures |

## Tests

```bash
python3 -m pytest -q
```

142 tests, no network - they run against the captured pages in `fixtures/`.
