"""Local web UI for the TxDMV appointment booker.

Runs on the operator's own machine and talks to the NemoQ portal through one live
wizard session (see wizard_session.py) - the same way the CLI does, so picking a time
continues the flow instead of restarting it.

    python3 app.py            # http://127.0.0.1:8765, opens the browser

A proxy is used only when one is configured in .env; from a US connection the portal
is reachable directly, which is the normal case for this tool.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import webshare
from nemoq_booking import DEFAULT_OFFICE, DEFAULT_SERVICE, BookingError
from watcher import Watcher
from wizard_session import SessionExpired, WizardSession

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
HOST = os.environ.get("TXDMV_HOST", "127.0.0.1")
PORT = int(os.environ.get("TXDMV_PORT", "8765"))


def build_pool():
    """Resolve a proxy pool. Slow - it calls the Webshare API - so only on demand."""
    try:
        return webshare.load_pool()
    except Exception:
        return None


app = FastAPI(title="TxDMV Appointment Desk", docs_url=None, redoc_url=None)

# Proxy OFF by default: the operator runs this from Houston, where the portal is
# reachable directly. It is a setting, not a startup guess.
USE_PROXY = bool(os.environ.get("TXDMV_USE_PROXY"))
session = WizardSession(pool=build_pool() if USE_PROXY else None)
watcher = Watcher(session)


def configure_proxy(use_proxy):
    """Swap the connection. Drops the wizard session: it is bound to its source IP."""
    global USE_PROXY, session
    use_proxy = bool(use_proxy)
    if use_proxy == USE_PROXY:
        return
    if use_proxy and not webshare.is_configured():
        raise HTTPException(status_code=400, detail="No hay proxy configurado en .env")
    watcher.stop()
    USE_PROXY = use_proxy
    session = WizardSession(pool=build_pool() if use_proxy else None)
    watcher.session = session


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
    office: str = DEFAULT_OFFICE
    service: str = DEFAULT_SERVICE
    from_date: str | None = None
    first_available: bool = False


class BookRequest(BaseModel):
    raw: str
    applicant: Applicant


class WatchRequest(BaseModel):
    office: str = DEFAULT_OFFICE
    service: str = DEFAULT_SERVICE
    auto_book: bool = False
    interval: int = 60
    date_from: str | None = None
    date_to: str | None = None
    time_from: str | None = None
    time_to: str | None = None
    first_available: bool = False
    applicant: Applicant = Applicant()


def guard(call):
    """Turn the flow's own exceptions into clean 4xx/5xx JSON."""
    try:
        return call()
    except SessionExpired as error:
        raise HTTPException(status_code=409, detail=str(error))
    except BookingError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except webshare.NoUsableProxy as error:
        raise HTTPException(status_code=502, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail="{0}: {1}".format(type(error).__name__, error))


@app.get("/api/state")
def state():
    return {"session": session.status(), "watch": watcher.status(), "settings": settings_payload()}


def settings_payload():
    return {"use_proxy": USE_PROXY, "proxy_configured": webshare.is_configured()}


@app.get("/api/settings")
def get_settings():
    return settings_payload()


@app.post("/api/settings")
def set_settings(request: Settings):
    configure_proxy(request.use_proxy)
    return settings_payload()


@app.get("/api/offices")
def offices():
    return {"offices": guard(session.offices)}


@app.get("/api/services")
def services(office: str = DEFAULT_OFFICE):
    return {"services": guard(lambda: session.services(office))}


@app.post("/api/search")
def search(request: SearchRequest):
    return guard(
        lambda: session.search(
            office=request.office,
            service=request.service,
            from_date=request.from_date,
            first_available=request.first_available,
        )
    )


@app.post("/api/book")
def book(request: BookRequest):
    missing = [
        name
        for name in ("first_name", "last_name", "email", "phone")
        if not getattr(request.applicant, name)
    ]
    if missing:
        raise HTTPException(status_code=400, detail="Faltan datos: {0}".format(", ".join(missing)))
    return guard(lambda: session.book(request.raw, request.applicant.model_dump()))


@app.post("/api/watch/start")
def watch_start(request: WatchRequest):
    if request.auto_book:
        missing = [
            name
            for name in ("first_name", "last_name", "email", "phone")
            if not getattr(request.applicant, name)
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail="La reserva automática necesita todos los datos: {0}".format(
                    ", ".join(missing)
                ),
            )
    config = request.model_dump()
    config["applicant"] = request.applicant.model_dump()
    watcher.start(config)
    return watcher.status()


@app.post("/api/watch/stop")
def watch_stop():
    watcher.stop()
    return watcher.status()


@app.get("/api/watch/status")
def watch_status():
    return watcher.status()


@app.get("/api/watch/result")
def watch_result():
    """The last grid the watcher fetched, so the UI renders what it actually saw."""
    return watcher.result() or {"slots": [], "timetable": []}


@app.post("/api/keepalive")
def keepalive():
    return {"ok": guard(session.keepalive)}


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
