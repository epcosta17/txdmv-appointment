"""Local web UI for the TxDMV appointment booker.

Runs on the operator's own machine. The work happens in *lanes*: each lane is one
office/service target with its own portal session, grid and watcher, so two offices
can be chased at once and either one can book without disturbing the other
(see lanes.py for why the isolation is necessary rather than merely tidy).

    python3 app.py            # http://127.0.0.1:8765, opens the browser

The proxy is off by default: from a US connection the portal is reachable directly.
"""

from __future__ import annotations

import os
import random
import sys
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import webshare
from lanes import LaneLimit, LaneManager, UnknownLane
from nemoq_booking import DEFAULT_OFFICE, DEFAULT_SERVICE, BookingError
from wizard_session import SessionExpired, WizardSession

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
HOST = os.environ.get("TXDMV_HOST", "127.0.0.1")
PORT = int(os.environ.get("TXDMV_PORT", "8765"))

app = FastAPI(title="TxDMV Appointment Desk", docs_url=None, redoc_url=None)

# Proxy OFF by default: the operator runs this from Houston, where the portal is
# reachable directly. It is a setting, not a startup guess.
USE_PROXY = bool(os.environ.get("TXDMV_USE_PROXY"))
_proxy_cache = None
_picker_session = None
_picker_lock = threading.Lock()


def proxy_pool():
    """A *fresh* pool per lane, so lanes do not share one sticky exit IP.

    The proxy list is fetched once and reshuffled per lane; building a pool from
    scratch each time would call the Webshare API again for no benefit.
    """
    global _proxy_cache
    if not USE_PROXY:
        return None
    if _proxy_cache is None:
        try:
            pool = webshare.load_pool()
        except Exception:
            pool = None
        _proxy_cache = list(pool.proxies) if pool else []
    if not _proxy_cache:
        return None
    proxies = list(_proxy_cache)
    random.shuffle(proxies)
    return webshare.ProxyPool(proxies)


def new_session():
    return WizardSession(pool=proxy_pool())


lanes = LaneManager(new_session)

# The pair is fixed: the two Houston offices this desk actually works. Lanes are not
# created or closed from the UI, so there is no empty state and no lane bookkeeping.
FIXED_LANES = [("Houston North", DEFAULT_SERVICE), ("Houston South", DEFAULT_SERVICE)]


def build_lanes():
    lanes.close_all()
    for office, service in FIXED_LANES:
        lanes.create(office, service)


# Cheap at import: a lane holds a session object but opens nothing until it is used.
build_lanes()


def picker_session():
    """A session apart from the lanes, used only to read the dropdowns.

    Listing an office's services can fall back to a search, which would replace
    whatever grid a lane is showing - so the pickers get their own session.
    """
    global _picker_session
    with _picker_lock:
        if _picker_session is None:
            _picker_session = new_session()
        return _picker_session


def reset_connections():
    """Rebuild every session. The fixed lanes come back immediately."""
    global _proxy_cache, _picker_session
    _proxy_cache = None
    _picker_session = None
    build_lanes()


# ------------------------------------------------------------------- schemas


class Applicant(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    description: str = "Title Services"
    details: str = ""


class Settings(BaseModel):
    use_proxy: bool = False


class SearchRequest(BaseModel):
    office: str | None = None
    service: str | None = None
    from_date: str | None = None
    first_available: bool = False


class BookRequest(BaseModel):
    raw: str
    applicant: Applicant


class WatchRequest(BaseModel):
    auto_book: bool = False
    interval: int = 60
    applicant: Applicant = Applicant()
    # The watch runs against whatever the form currently shows, not against the
    # target of the last search - otherwise changing the office and pressing watch
    # would quietly keep watching (and auto-booking at) the previous one.
    office: str | None = None
    service: str | None = None
    first_available: bool | None = None
    # The portal only ever shows the current day and the next two, so a date range
    # or time window buys almost nothing and the UI no longer offers them. The
    # filters stay here because the watcher still honours them and they are the
    # guardrail that keeps auto-booking inside what the caller asked for.
    date_from: str | None = None
    date_to: str | None = None
    time_from: str | None = None
    time_to: str | None = None


def guard(call):
    """Turn the flow's own exceptions into clean 4xx/5xx JSON."""
    try:
        return call()
    except UnknownLane as error:
        raise HTTPException(status_code=404, detail=str(error))
    except LaneLimit as error:
        raise HTTPException(status_code=409, detail=str(error))
    except SessionExpired as error:
        raise HTTPException(status_code=409, detail=str(error))
    except BookingError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except webshare.NoUsableProxy as error:
        raise HTTPException(status_code=502, detail=str(error))
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="{0}: {1}".format(type(error).__name__, error))


def missing_fields(applicant):
    return [
        name
        for name in ("first_name", "last_name", "email", "phone")
        if not getattr(applicant, name)
    ]


# ------------------------------------------------------------------ settings


def settings_payload():
    return {"use_proxy": USE_PROXY, "proxy_configured": webshare.is_configured()}


@app.get("/api/state")
def state():
    return {"settings": settings_payload(), **lanes.status()}


@app.get("/api/settings")
def get_settings():
    return settings_payload()


@app.post("/api/settings")
def set_settings(request: Settings):
    """Changing the connection drops every lane: sessions are bound to their IP."""
    global USE_PROXY
    if request.use_proxy != USE_PROXY:
        if request.use_proxy and not webshare.is_configured():
            raise HTTPException(status_code=400, detail="No hay proxy configurado en .env")
        USE_PROXY = request.use_proxy
        reset_connections()
    return settings_payload()


# ------------------------------------------------------------------- pickers


@app.get("/api/offices")
def offices():
    return {"offices": guard(picker_session().offices)}


@app.get("/api/services")
def services(office: str = DEFAULT_OFFICE):
    return {"services": guard(lambda: picker_session().services(office))}


# --------------------------------------------------------------------- lanes


@app.get("/api/lanes")
def list_lanes():
    return lanes.status()


@app.post("/api/lanes/{lane_id}/search")
def lane_search(lane_id: int, request: SearchRequest):
    lane = guard(lambda: lanes.get(lane_id))
    return guard(
        lambda: lane.search(
            office=request.office,
            service=request.service,
            from_date=request.from_date,
            first_available=request.first_available,
        )
    )


@app.get("/api/lanes/{lane_id}/result")
def lane_result(lane_id: int):
    lane = guard(lambda: lanes.get(lane_id))
    return lane.result() or {"slots": [], "timetable": []}


@app.post("/api/lanes/{lane_id}/book")
def lane_book(lane_id: int, request: BookRequest):
    lane = guard(lambda: lanes.get(lane_id))
    missing = missing_fields(request.applicant)
    if missing:
        raise HTTPException(status_code=400, detail="Faltan datos: {0}".format(", ".join(missing)))
    return guard(lambda: lane.session.book(request.raw, request.applicant.model_dump()))


@app.post("/api/lanes/{lane_id}/watch")
def lane_watch_start(lane_id: int, request: WatchRequest):
    lane = guard(lambda: lanes.get(lane_id))
    if request.auto_book and missing_fields(request.applicant):
        raise HTTPException(
            status_code=400,
            detail="La reserva automática necesita todos los datos: {0}".format(
                ", ".join(missing_fields(request.applicant))
            ),
        )
    lane.retarget(
        office=request.office,
        service=request.service,
        from_date=request.date_from,
        first_available=request.first_available,
    )
    config = request.model_dump()
    config.update(
        {
            "office": lane.office,
            "service": lane.service,
            "first_available": lane.first_available,
            "date_from": lane.from_date,
            "applicant": request.applicant.model_dump(),
        }
    )
    lane.watcher.start(config)
    return lane.status()


@app.delete("/api/lanes/{lane_id}/watch")
def lane_watch_stop(lane_id: int):
    lane = guard(lambda: lanes.get(lane_id))
    lane.watcher.stop()
    return lane.status()


@app.post("/api/keepalive")
def keepalive():
    alive = [lane.session.keepalive() for lane in lanes.list()]
    return {"ok": any(alive) if alive else False, "lanes": len(alive)}


# ---------------------------------------------------------------------- page


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn

    url = "http://{0}:{1}/".format(HOST, PORT)
    print("TxDMV Appointment Desk -> {0}".format(url))
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
