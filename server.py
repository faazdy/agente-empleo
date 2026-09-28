import asyncio
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.database import JobDatabase
from core.models import JobOffer
from extractors.computrabajo import ComputrabajoExtractor
from extractors.indeed import IndeedExtractor
from services.matcher import MatcherEngine
from core.guard import WafBlocked, VerificationRequired, guard, in_alert_window, BOGOTA_TZ
from core.database import STAGES
from core.isolated import run_isolated, describe_error
import traceback
from services.skills_gap import analyze as analyze_skills
import json as _json
import random
from pathlib import Path as _Path

# ---- Registro persistente: todo lo que se imprime también queda en data/radar.log ----
import logging as _logging, logging.handlers as _lh, sys as _sys, os as _os
_os.makedirs("data", exist_ok=True)
_log_handler = _lh.RotatingFileHandler("data/radar.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_log_handler.setFormatter(_logging.Formatter("%(asctime)s %(message)s"))
radar_log = _logging.getLogger("radar")
radar_log.setLevel(_logging.INFO)
if not radar_log.handlers:
    radar_log.addHandler(_log_handler)


class _Tee:
    """Duplica stdout/stderr en data/radar.log para poder diagnosticar sin ver la consola."""
    def __init__(self, stream):
        self.stream = stream
    def write(self, msg):
        try:
            self.stream.write(msg)
        except Exception:
            pass
        m = msg.rstrip()
        if m:
            radar_log.info(m)
    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self.stream, name)


if not isinstance(_sys.stdout, _Tee):
    _sys.stdout = _Tee(_sys.stdout)
    _sys.stderr = _Tee(_sys.stderr)


app = FastAPI(title="Radar de Empleo - Miguel Vásquez", version="2.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = JobDatabase("data/jobs.db")
extractor = ComputrabajoExtractor()
indeed_extractor = IndeedExtractor()
matcher = MatcherEngine()


class StatusUpdateRequest(BaseModel):
    status: str


@app.get("/api/offers", response_model=List[JobOffer])
def get_offers(
    status: Optional[str] = Query(None, description="Filtrar por estado (matched, applied, favorite, discarded, all)"),
    min_match: Optional[float] = Query(None, description="Score mínimo de afinidad"),
    sort_by: Optional[str] = Query("smart", description="smart | recent | score"),
):
    actual_status = None if status in (None, "all", "") else status
    return db.get_offers(status=actual_status, min_match=min_match, sort_by=sort_by or "smart")


@app.get("/api/stats")
def get_stats():
    return db.get_stats()


from services.qa_bank import match_question, get_all_entries


class QARequest(BaseModel):
    question: str


@app.get("/api/qa/entries")
def get_qa_entries():
    return get_all_entries()


@app.post("/api/qa/match")
def match_qa(req: QARequest):
    return match_question(req.question)


from core.browser import BrowserController, managed_browser, cdp_alive, find_browser, CDP_PORT
BraveController = BrowserController  # compatibilidad


class ApplicationItem(BaseModel):
    title: str
    company: Optional[str] = ""
    external_id: Optional[str] = None


class SyncApplicationsPayload(BaseModel):
    applications: List[ApplicationItem]


@app.post("/api/sync_applications")
def sync_applications_manual(payload: SyncApplicationsPayload):
    synced = 0
    for app_item in payload.applications:
        if db.mark_as_applied(
            external_id=app_item.external_id,
            title=app_item.title,
            company=app_item.company
        ):
            synced += 1
    return {"success": True, "received": len(payload.applications), "synced": synced}


@app.post("/api/brave/sync")
@app.post("/api/browser/sync-applications")
async def sync_applications_browser():
    """Lee 'Mis postulaciones' de Computrabajo con el navegador del radar."""
    if guard.lock.locked():
        raise HTTPException(status_code=409, detail="Hay un escaneo en curso; intenta cuando termine.")
    async def _job():
        controller = BrowserController()
        try:
            return await controller.sync_computrabajo_applications(db)
        finally:
            await controller.close()

    async with guard.lock:
        try:
            return await run_isolated(_job)
        except ConnectionError as ce:
            raise HTTPException(status_code=503, detail=describe_error(ce))
        except ImportError:
            raise HTTPException(status_code=501, detail="Playwright no está instalado en el entorno.")
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Error durante sincronización: {describe_error(e)}")


@app.get("/api/browser/status")
async def browser_status():
    found = find_browser()
    return {
        "browser": found[0] if found else None,
        "installed": bool(found),
        "running": await cdp_alive(),
        "visible": managed_browser.visible,
        "port": CDP_PORT,
    }


class BrowserOpenRequest(BaseModel):
    url: Optional[str] = "https://co.indeed.com"


@app.post("/api/browser/open")
async def browser_open(req: BrowserOpenRequest):
    """
    Abre el navegador del radar VISIBLE, con el mismo perfil que usa para Indeed.
    Sirve para iniciar sesión (Indeed / Computrabajo) o resolver una verificación.
    Las sesiones quedan guardadas en el perfil del radar.
    """
    if guard.lock.locked():
        raise HTTPException(status_code=409, detail="Hay un escaneo en curso; ábrelo cuando termine.")
    url = req.url or "https://co.indeed.com"

    async def _close_hidden():
        ctl = BrowserController(auto_launch=False)
        try:
            await ctl.connect()
            session = await ctl._browser.new_browser_cdp_session()
            await session.send("Browser.close")
        except Exception:
            pass
        finally:
            if ctl._playwright:
                await ctl._playwright.stop()

    async def _new_tab():
        ctl = BrowserController(auto_launch=False)
        try:
            await ctl.open_job_page(url)
        finally:
            if ctl._playwright:
                await ctl._playwright.stop()

    if await cdp_alive():
        if not managed_browser.visible:
            # Está abierto oculto (fuera de pantalla): se cierra y se reabre visible
            await run_isolated(_close_hidden)
            managed_browser.launched_by_app = False
            await asyncio.sleep(1.5)
        else:
            try:
                await run_isolated(_new_tab)
            except Exception as e:
                raise HTTPException(status_code=500, detail=describe_error(e))
            return {"success": True, "message": "Navegador del radar ya abierto: abrí una pestaña nueva."}
    try:
        await managed_browser.ensure(visible=True, start_url=url)
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    return {"success": True, "message": f"Navegador del radar abierto ({managed_browser.browser_name}). Inicia sesión si hace falta y ciérralo cuando termines."}


# ---------------------------------------------------------------------------
# Seguimiento de postulaciones
# ---------------------------------------------------------------------------

class TrackingRequest(BaseModel):
    stage: Optional[str] = None
    notes: Optional[str] = None


@app.post("/api/offers/{offer_id}/tracking")
def update_tracking(offer_id: str, req: TrackingRequest):
    try:
        ok = db.update_tracking(offer_id, stage=req.stage, notes=req.notes)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    if not ok:
        raise HTTPException(status_code=404, detail="La vacante no existe o no está marcada como postulada.")
    return {"success": True}


@app.get("/api/tracking")
def get_tracking():
    return db.get_tracking_summary()


# ---------------------------------------------------------------------------
# Qué me falta aprender
# ---------------------------------------------------------------------------

@app.get("/api/skills-gap")
def get_skills_gap(scope: str = Query("relevant", description="applied | relevant (compatibles+guardadas+postuladas) | all")):
    if scope == "applied":
        offers = db.get_offers(status="applied")
    elif scope == "all":
        offers = db.get_offers()
    else:
        offers = [o for o in db.get_offers() if o.status in ("matched", "favorite", "applied")]
    result = analyze_skills(offers)
    result["scope"] = scope
    return result


@app.post("/api/offers/{offer_id}/status")
def update_offer_status(offer_id: str, req: StatusUpdateRequest):
    db.update_status(offer_id, req.status)
    return {"success": True, "id": offer_id, "status": req.status}


sync_state = {
    "is_running": False,
    "current_query": "",
    "current_index": 0,
    "total_queries": 0,
    "new_offers": 0,
    "total_scanned": 0,
    "total_in_db": 0,
    "last_error": None,
}

# max_pages es un TOPE: el extractor ordena por fecha y pagina mientras haya "Siguiente".
# Verificado en vivo (2026-09-25): "desarrollador" en Bogotá con pubdate=2 tenía 48 vacantes
# (3 páginas); con el tope anterior de 2 páginas y orden por relevancia se perdían 8.
SEARCH_TARGETS = [
    # Consultas amplias (cubren casi todo; paginación completa)
    {"query": "desarrollador", "location": "bogota-dc", "max_pages": 6},
    {"query": "programador", "location": "bogota-dc", "max_pages": 4},
    {"query": "ingeniero de sistemas", "location": "bogota-dc", "max_pages": 4},
    {"query": "analista de datos", "location": "bogota-dc", "max_pages": 3},
    # Consultas específicas: títulos que no dicen "desarrollador" ni "programador"
    {"query": "full stack", "location": "bogota-dc", "max_pages": 2},
    {"query": "backend", "location": "bogota-dc", "max_pages": 2},
    {"query": "frontend", "location": "bogota-dc", "max_pages": 2},
    {"query": "python", "location": "bogota-dc", "max_pages": 2},
    {"query": "react", "location": "bogota-dc", "max_pages": 2},
    {"query": "software", "location": "bogota-dc", "max_pages": 3},
    {"query": "developer", "location": "bogota-dc", "max_pages": 2},
    {"query": "ingeniero de datos", "location": "bogota-dc", "max_pages": 2},
    {"query": "data analyst", "location": "bogota-dc", "max_pages": 2},
    {"query": "power bi", "location": "bogota-dc", "max_pages": 2},
    {"query": "analista bi", "location": "bogota-dc", "max_pages": 2},
    {"query": "sql", "location": "bogota-dc", "max_pages": 2},
    {"query": "analista de sistemas", "location": "bogota-dc", "max_pages": 2},
    {"query": "soporte ti", "location": "bogota-dc", "max_pages": 2},
    {"query": "qa", "location": "bogota-dc", "max_pages": 2},
    {"query": "inteligencia artificial", "location": "bogota-dc", "max_pages": 2},
    {"query": "junior", "location": "bogota-dc", "max_pages": 3},
    # 100% remoto en todo el país. El slug correcto es "remoto": "teletrabajo" devolvía
    # 1 vacante contra 4 reales en la prueba del 2026-09-25.
    {"query": "desarrollador", "location": "remoto", "max_pages": 2},
    {"query": "programador", "location": "remoto", "max_pages": 2},
    {"query": "analista de datos", "location": "remoto", "max_pages": 2},
    {"query": "python", "location": "remoto", "max_pages": 2},
    {"query": "full stack", "location": "remoto", "max_pages": 2},
]

# Indeed Colombia (vía navegador real por CDP: Edge o Brave). Cada página es una navegación
# real con pausas de 6-10 s; se pagina con el enlace "Siguiente" (máx. max_pages) y el
# detalle se pide solo de vacantes prometedoras, con tope por sesión.
INDEED_TARGETS = [
    {"query": "desarrollador",          "location": "Bogotá, D.C.", "max_pages": 3},
    {"query": "programador",            "location": "Bogotá, D.C.", "max_pages": 2},
    {"query": "ingeniero de sistemas",  "location": "Bogotá, D.C.", "max_pages": 2},
    {"query": "software",               "location": "Bogotá, D.C.", "max_pages": 2},
    {"query": "analista de datos",      "location": "Bogotá, D.C.", "max_pages": 2},
    {"query": "python",                 "location": "Bogotá, D.C.", "max_pages": 1},
    {"query": "power bi",               "location": "Bogotá, D.C.", "max_pages": 1},
    {"query": "junior",                 "location": "Bogotá, D.C.", "max_pages": 2},
    # Remoto: se busca "<consulta> remoto" en toda Colombia (el filtro de Indeed no funciona)
    {"query": "desarrollador",          "location": "Colombia", "max_pages": 2, "remote": True},
    {"query": "programador",            "location": "Colombia", "max_pages": 1, "remote": True},
    {"query": "analista de datos",      "location": "Colombia", "max_pages": 1, "remote": True},
    {"query": "python",                 "location": "Colombia", "max_pages": 1, "remote": True},
]
INDEED_ENABLED = True

# Pausa extra ENTRE consultas (además de las pausas internas del extractor).
INTER_QUERY_PAUSE = (6.0, 12.0)


_last_store = {"guardadas": 0, "descartadas_filtro": 0}


def _store_offers(offers, existing_ids) -> int:
    """Guarda y evalúa ofertas frescas. Retorna cuántas quedaron como 'matched'."""
    matched = 0
    for offer in offers:
        if not JobDatabase.is_fresh(offer.posted_date):
            continue
        if db.save_offer(offer):
            score, reason, status = matcher.evaluate(offer)
            db.update_match(offer.id, score, reason, status)
            _last_store["guardadas"] += 1
            if status == "matched":
                matched += 1
            else:
                _last_store["descartadas_filtro"] += 1
            existing_ids.add(offer.id)
            if offer.external_id:
                existing_ids.add(offer.external_id)
    return matched


PORTALS = ("computrabajo", "indeed")


async def _run_sync_worker(portals=PORTALS, interactive: bool = True):
    """Escaneo: Computrabajo y/o Indeed (por defecto ambos). Siempre bajo el candado único."""
    do_ct = "computrabajo" in portals
    do_indeed = INDEED_ENABLED and "indeed" in portals
    global sync_state
    async with guard.lock:
        sync_state["is_running"] = True
        sync_state["new_offers"] = 0
        sync_state["total_scanned"] = 0
        sync_state["total_queries"] = (len(SEARCH_TARGETS) if do_ct else 0) + (len(INDEED_TARGETS) if do_indeed else 0)
        sync_state["portals"] = [p for p in PORTALS if (p == "computrabajo" and do_ct) or (p == "indeed" and do_indeed)]
        sync_state["last_error"] = None
        sync_state["portal_errors"] = {}
        sync_state["note"] = ""

        existing_ids = db.get_existing_ids()
        new_count = 0
        extractor.new_session()  # una sola huella de navegador para todo el escaneo
        report = {"started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "portales": {}}
        print(f"[SyncWorker] Inicio de escaneo: {', '.join(sync_state['portals'])}")

        try:
            # --- Fase 1: Computrabajo --------------------------------------
            try:
                for idx, target in enumerate(SEARCH_TARGETS if do_ct else [], 1):
                    sync_state["current_index"] = idx
                    sync_state["current_query"] = f"[Computrabajo] {target['query']} ({target['location']})"
                    if idx > 1:
                        await asyncio.sleep(random.uniform(*INTER_QUERY_PAUSE))
                    offers = await extractor.search_jobs(
                        query=target["query"],
                        location=target["location"],
                        max_pages=target.get("max_pages", 1),
                        existing_ids=existing_ids,
                        pubdate=7,  # semana completa (recortado a MAX_AGE_DAYS por el filtro) para cubrir fin de semana
                    )
                    sync_state["total_scanned"] += len(offers)
                    new_count += _store_offers(offers, existing_ids)
                    sync_state["new_offers"] = new_count
            except WafBlocked as wb:
                # Corte global: se guardan las ofertas parciales y NO se sigue golpeando el portal
                new_count += _store_offers(wb.partial, existing_ids)
                sync_state["new_offers"] = new_count
                sync_state["portal_errors"]["Computrabajo"] = str(wb)
                print(f"[SyncWorker] {wb}")

            if do_ct:
                report["portales"]["Computrabajo"] = {
                    **extractor.stats, "compatibles": new_count, **_last_store,
                    "error": sync_state["portal_errors"].get("Computrabajo"),
                }
                print(f"[SyncWorker] Computrabajo: {report['portales']['Computrabajo']}")
            _last_store.update(guardadas=0, descartadas_filtro=0)
            ct_matched = new_count

            # --- Fase 2: Indeed (navegador real) ---------------------------
            if do_indeed:
                base = len(SEARCH_TARGETS) if do_ct else 0

                def _progress(i, t):
                    sync_state["current_index"] = base + i
                    sync_state["current_query"] = f"[Indeed] {t['query']} ({t['location']}{', remoto' if t.get('remote') else ''})"

                try:
                    offers = await indeed_extractor.search_many(
                        INDEED_TARGETS, existing_ids, on_progress=_progress, interactive=interactive,
                        on_status=lambda m: sync_state.__setitem__("note", m))
                    sync_state["total_scanned"] += len(offers)
                    new_count += _store_offers(offers, existing_ids)
                    ie = indeed_extractor
                    if ie.detail_attempts >= 3 and ie.detail_failures == ie.detail_attempts:
                        # Sin descripción no se puede aplicar el filtro de años de experiencia
                        sync_state["portal_errors"]["Indeed"] = (
                            "Indeed: no se pudo leer la descripción de las vacantes (¿cambió la página?). "
                            "Se guardaron solo con el resumen y sin filtro de experiencia."
                        )
                except WafBlocked as wb:
                    new_count += _store_offers(wb.partial, existing_ids)
                    sync_state["portal_errors"]["Indeed"] = str(wb)
                except ConnectionError as ce:
                    sync_state["portal_errors"]["Indeed"] = f"Indeed: {describe_error(ce)}"
                except ImportError:
                    sync_state["portal_errors"]["Indeed"] = "Playwright no está instalado en el entorno."
                except Exception as e:
                    traceback.print_exc()  # detalle completo en la consola del servidor
                    sync_state["portal_errors"]["Indeed"] = f"Error en Indeed: {describe_error(e)}"
                sync_state["new_offers"] = new_count

            if do_indeed:
                report["portales"]["Indeed"] = {
                    **(indeed_extractor.stats or {}), "compatibles": new_count - ct_matched, **_last_store,
                    "error": sync_state["portal_errors"].get("Indeed"),
                }
                print(f"[SyncWorker] Indeed: {report['portales']['Indeed']}")
            _last_store.update(guardadas=0, descartadas_filtro=0)

            if sync_state["portal_errors"]:
                sync_state["last_error"] = " | ".join(sync_state["portal_errors"].values())

        except Exception as e:
            traceback.print_exc()
            sync_state["last_error"] = describe_error(e)
            report["error_general"] = describe_error(e)
        finally:
            sync_state["is_running"] = False
            sync_state["total_in_db"] = len(db.get_offers())
            report["finished_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            report["compatibles_nuevas"] = new_count
            sync_state["report"] = report
            _save_state({"last_full_sync_at": report["finished_at"], "last_report": report})
            print(f"[SyncWorker] Fin: {new_count} nuevas compatibles")


@app.post("/api/sync")
async def sync_offers(portal: str = Query("all", description="all | computrabajo | indeed")):
    portal = (portal or "all").lower()
    if portal not in ("all", *PORTALS):
        raise HTTPException(status_code=400, detail="portal debe ser all, computrabajo o indeed")
    selected = PORTALS if portal == "all" else (portal,)
    if sync_state.get("is_running") or guard.lock.locked():
        return {"success": True, "message": "Ya hay un escaneo en curso (sync o alerta). Se evita correr dos a la vez.", "running": True}

    asyncio.create_task(_run_sync_worker(selected))
    names = " y ".join(p.capitalize() for p in selected)
    return {"success": True, "message": f"Sincronización de {names} iniciada en segundo plano", "running": True}


@app.get("/api/sync/status")
def get_sync_status():
    return sync_state


@app.get("/api/sync/report")
def get_sync_report():
    """Resumen del último escaneo (persistente: sobrevive reinicios)."""
    return _load_state().get("last_report") or {}


@app.get("/api/log")
def get_log(lines: int = Query(80, ge=1, le=1000)):
    """Últimas líneas de data/radar.log (diagnóstico)."""
    try:
        with open("data/radar.log", encoding="utf-8", errors="replace") as f:
            return {"lines": f.read().splitlines()[-lines:]}
    except FileNotFoundError:
        return {"lines": []}


@app.post("/api/guard/clear")
def clear_guard(portal: str = Query(..., description="computrabajo | indeed")):
    """Levanta a mano una pausa de seguridad (úsalo solo si estás seguro de que el portal ya funciona en tu navegador)."""
    if portal.lower() not in PORTALS:
        raise HTTPException(status_code=400, detail="portal debe ser computrabajo o indeed")
    guard.clear(portal)
    return {"success": True}


@app.get("/api/guard/status")
def get_guard_status():
    """Estado anti-baneo: escaneo en curso, enfriamientos por portal y ventana horaria de alerta."""
    return guard.status()


# ---------------------------------------------------------------------------
# SINCRONIZACIÓN DIARIA AUTOMÁTICA
# Una sincronización completa (Computrabajo + Indeed) al día, a partir de DAILY_SYNC_TIME
# (hora Bogotá). Si el servidor se enciende más tarde y hoy no se ha hecho, la hace apenas
# puede (dentro de la ventana 07:00-21:00). El estado sobrevive reinicios (data/state.json).
# ---------------------------------------------------------------------------

import datetime as _dt

DAILY_SYNC_ENABLED = True
DAILY_SYNC_TIME = (7, 30)  # 07:30 hora Bogotá
DAILY_CHECK_SECONDS = 10 * 60
STATE_PATH = _Path("data/state.json")


def _load_state() -> dict:
    try:
        return _json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(update: dict) -> None:
    data = _load_state()
    data.update(update)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_sync_due(now: Optional[_dt.datetime] = None, state: Optional[dict] = None) -> bool:
    """¿Toca la sincronización diaria? (no hecha hoy, ya pasó la hora y estamos en ventana)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    state = _load_state() if state is None else state
    local = now.astimezone(BOGOTA_TZ)
    if (local.hour, local.minute) < DAILY_SYNC_TIME or not in_alert_window(now):
        return False
    last = state.get("last_full_sync_at")
    if not last:
        return True
    try:
        last_local = _dt.datetime.fromisoformat(last).astimezone(BOGOTA_TZ)
    except Exception:
        return True
    return last_local.date() < local.date()


daily_state = {"enabled": DAILY_SYNC_ENABLED, "time": "%02d:%02d" % DAILY_SYNC_TIME, "last_trigger_at": None}


async def _daily_loop():
    await asyncio.sleep(90)  # deja arrancar el servidor
    while True:
        try:
            if DAILY_SYNC_ENABLED and daily_sync_due() and not guard.lock.locked() and not sync_state.get("is_running"):
                daily_state["last_trigger_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
                print("[DailySync] Iniciando sincronización diaria completa")
                await _run_sync_worker(interactive=False)  # sin supervisión: no abre ventanas
        except Exception as e:
            print(f"[DailySync] Error: {e}")
        await asyncio.sleep(DAILY_CHECK_SECONDS)


@app.get("/api/daily/status")
def get_daily_status():
    st = _load_state()
    return {**daily_state, "last_full_sync_at": st.get("last_full_sync_at"), "due_now": daily_sync_due(state=st)}


# ---------------------------------------------------------------------------
# ALERT SCANNER — Modo "Primero en Llegar" (cada 45 minutos, pubdate=1)
# Solo rastrea página 1 de las consultas más activas buscando vacantes
# publicadas en las ÚLTIMAS 24 HORAS. Ligero y anti-ban.
# ---------------------------------------------------------------------------

ALERT_INTERVAL_SECONDS = 45 * 60  # 45 minutos

ALERT_TARGETS = [
    # Desarrollo de software (Bogotá)
    {"query": "desarrollador",           "location": "bogota-dc",  "max_pages": 1},
    {"query": "desarrollador junior",    "location": "bogota-dc",  "max_pages": 1},
    {"query": "desarrollador full stack","location": "bogota-dc",  "max_pages": 1},
    {"query": "desarrollador backend",   "location": "bogota-dc",  "max_pages": 1},
    {"query": "desarrollador frontend",  "location": "bogota-dc",  "max_pages": 1},
    {"query": "desarrollador react",     "location": "bogota-dc",  "max_pages": 1},
    {"query": "programador",             "location": "bogota-dc",  "max_pages": 1},
    # Ingeniería y Calidad (Bogotá)
    {"query": "ingeniero de sistemas",   "location": "bogota-dc",  "max_pages": 1},
    {"query": "ingeniero de software",   "location": "bogota-dc",  "max_pages": 1},
    {"query": "qa software",             "location": "bogota-dc",  "max_pages": 1},
    {"query": "soporte ti",              "location": "bogota-dc",  "max_pages": 1},
    # Datos y BI (Bogotá)
    {"query": "analista de datos",       "location": "bogota-dc",  "max_pages": 1},
    {"query": "analista bi",             "location": "bogota-dc",  "max_pages": 1},
    # 100% Remoto / Teletrabajo Colombia
    {"query": "desarrollador",           "location": "remoto",      "max_pages": 1},
    {"query": "programador",             "location": "remoto",      "max_pages": 1},
]

alert_state = {
    "is_running": False,
    "last_scan_at": None,
    "next_scan_at": None,
    "last_new_count": 0,
    "total_found_today": 0,
    "last_error": None,
}


async def _run_alert_scan() -> int:
    """
    Escaneo ligero (solo Computrabajo, página 1, hoy y ayer). Retorna nuevas vacantes compatibles.
    Indeed no entra en el modo alerta para no abrir pestañas en el navegador cada 45 minutos.
    """
    if guard.lock.locked():
        print("[AlertScanner] Hay otro escaneo en curso; se omite este ciclo.")
        return 0
    async with guard.lock:
        existing_ids = db.get_existing_ids()
        new_count = 0
        extractor.new_session()
        for idx, target in enumerate(ALERT_TARGETS):
            if idx:
                await asyncio.sleep(random.uniform(*INTER_QUERY_PAUSE))
            try:
                offers = await extractor.search_jobs(
                    query=target["query"],
                    location=target["location"],
                    max_pages=1,
                    existing_ids=existing_ids,
                    pubdate=7,  # semana completa (recortado a MAX_AGE_DAYS por el filtro)
                )
                new_count += _store_offers(offers, existing_ids)
            except WafBlocked as wb:
                new_count += _store_offers(wb.partial, existing_ids)
                alert_state["last_error"] = str(wb)
                print(f"[AlertScanner] {wb}")
                break  # corte global
            except Exception as e:
                print(f"[AlertScanner] Error en '{target['query']}': {e}")
        return new_count


async def _alert_loop():
    """Bucle infinito que corre el escaneo de alerta cada ALERT_INTERVAL_SECONDS."""
    await asyncio.sleep(5 * 60)  # espera 5 min antes del primer ciclo
    while True:
        now = _dt.datetime.now(_dt.timezone.utc)
        alert_state["is_running"] = True
        alert_state["last_scan_at"] = now.isoformat()
        alert_state["last_error"] = None
        print(f"[AlertScanner] Ciclo automático a las {now.strftime('%H:%M')} UTC")
        if not in_alert_window(now):
            alert_state["is_running"] = False
            alert_state["last_error"] = "Fuera de horario (el modo alerta corre de 07:00 a 21:00, hora Bogotá)"
            nxt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ALERT_INTERVAL_SECONDS)
            alert_state["next_scan_at"] = nxt.isoformat()
            await asyncio.sleep(ALERT_INTERVAL_SECONDS)
            continue
        try:
            found = await _run_alert_scan()
            alert_state["last_new_count"] = found
            alert_state["total_found_today"] = (alert_state.get("total_found_today") or 0) + found
            print(f"[AlertScanner] {'✅ ' + str(found) + ' nuevas compatibles' if found else 'Sin novedades'}")
        except Exception as e:
            alert_state["last_error"] = str(e)
            print(f"[AlertScanner] Error: {e}")
        finally:
            alert_state["is_running"] = False
            nxt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ALERT_INTERVAL_SECONDS)
            alert_state["next_scan_at"] = nxt.isoformat()
        await asyncio.sleep(ALERT_INTERVAL_SECONDS)


@app.on_event("startup")
async def start_alert_loop():
    nxt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=5 * 60)
    alert_state["next_scan_at"] = nxt.isoformat()
    asyncio.create_task(_alert_loop())
    asyncio.create_task(_daily_loop())
    print("[AlertScanner] 🔔 Sistema de alerta activado. Primer scan en 5 min.")


@app.get("/api/alert/status")
def get_alert_status():
    return alert_state


@app.post("/api/alert/scan-now")
async def alert_scan_now():
    """Fuerza un escaneo de alerta inmediato."""
    if alert_state.get("is_running") or guard.lock.locked():
        return {"success": False, "message": "Ya hay un escaneo en curso (sync o alerta)"}

    async def _immediate():
        alert_state["is_running"] = True
        alert_state["last_error"] = None
        try:
            found = await _run_alert_scan()
            alert_state["last_new_count"] = found
            alert_state["total_found_today"] = (alert_state.get("total_found_today") or 0) + found
            alert_state["last_scan_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        except Exception as e:
            alert_state["last_error"] = str(e)
        finally:
            alert_state["is_running"] = False
            nxt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ALERT_INTERVAL_SECONDS)
            alert_state["next_scan_at"] = nxt.isoformat()

    asyncio.create_task(_immediate())
    return {"success": True, "message": "Escaneo de alerta iniciado"}


@app.get("/", response_class=HTMLResponse)
def get_dashboard_html():
    return """<!DOCTYPE html>
<html lang="es" class="h-full">
<head>
  <meta charset="UTF-8">
  <meta name="referrer" content="no-referrer">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Radar de Empleo · Miguel Vásquez</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'system-ui', 'sans-serif'],
            mono: ['"JetBrains Mono"', 'monospace'],
          },
          colors: {
            brand: {
              DEFAULT: '#e11d48',
              hot: '#be123c',
              soft: '#ffe4e6',
              on: '#ffffff',
            },
            surface: {
              bg: '#f8fafc',
              1: '#ffffff',
              2: '#f1f5f9',
              border: '#e2e8f0',
            },
            muted: {
              DEFAULT: '#64748b',
              foreground: '#64748b',
            }
          }
        }
      }
    }
  </script>
  <style>
    :root {
      --brand: #e11d48;
      --brand-hot: #be123c;
      --surface-bg: #f8fafc;
      --surface-1: #ffffff;
      --surface-2: #f1f5f9;
      --border: #e2e8f0;
      --foreground: #0f172a;
      --muted: #64748b;
    }
    body {
      background-color: var(--surface-bg);
      color: var(--foreground);
      font-family: 'Inter', system-ui, sans-serif;
      -webkit-tap-highlight-color: transparent;
      min-height: 100vh;
    }
    .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
    .custom-scrollbar::-webkit-scrollbar-track { background: var(--surface-bg); }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: var(--brand); }

    /* Retícula del banner con movimiento sutil */
    .grid-lines {
      position: absolute;
      inset: -40px;
      pointer-events: none;
      background-image:
        linear-gradient(to right, rgba(225, 29, 72, 0.05) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(225, 29, 72, 0.05) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: radial-gradient(ellipse 80% 60% at 50% 10%, #000 15%, transparent 80%);
      opacity: 0.8;
      z-index: 0;
    }

    .spotlight-card {
      background: var(--surface-1);
      border: 1px solid var(--border);
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.25s ease, box-shadow 0.25s ease;
    }
    .spotlight-card:hover {
      transform: translateY(-2px);
      border-color: rgba(225, 29, 72, 0.45);
      box-shadow: 0 10px 30px -10px rgba(225, 29, 72, 0.15);
    }

    .link-underline {
      background-image: linear-gradient(var(--brand), var(--brand));
      background-size: 0% 1.5px;
      background-position: 0 100%;
      background-repeat: no-repeat;
      transition: background-size 0.35s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .link-underline:hover {
      background-size: 100% 1.5px;
    }
  </style>
</head>
<body class="min-h-screen flex flex-col antialiased selection:bg-[#e11d48] selection:text-white relative overflow-x-hidden">

  <!-- Retícula de fondo sutil -->
  <div class="grid-lines" aria-hidden="true"></div>

  <!-- Toast Container -->
  <div id="toastContainer" class="fixed bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none"></div>

  <!-- Header Superior Identidad Carmesí -->
  <header class="border-b border-[#e2e8f0] bg-[#f8fafc]/90 backdrop-blur sticky top-0 z-40 px-4 py-3 sm:px-6 relative">
    <div class="max-w-6xl mx-auto flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      
      <!-- Brand & Profile -->
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3.5">
          <a href="https://miguxld.site" target="_blank" class="w-10 h-10 rounded-xl bg-[#ffffff] border border-[#e2e8f0] hover:border-[#e11d48]/60 flex items-center justify-center text-[#e11d48] transition group shadow-sm">
            <span class="font-mono font-extrabold text-sm tracking-tighter">MV</span>
          </a>
          <div>
            <div class="flex items-center gap-2">
              <h1 class="text-base font-bold text-[#0f172a] tracking-tight flex items-center gap-2">
                Radar de Empleo
                <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono font-bold bg-[#ffe4e6] text-[#be123c] border border-[#e11d48]/30">
                  <span class="w-1.5 h-1.5 rounded-full bg-[#e11d48] animate-pulse"></span>
                  RADAR ACTIVO
                </span>
              </h1>
            </div>
            <p class="text-xs text-[#64748b] font-medium flex items-center gap-1.5 mt-0.5">
              <span>Miguel Vásquez</span>
              <span class="text-[#e2e8f0]">·</span>
              <span class="text-[#0f172a]">Full Stack & Data</span>
              <span class="text-[#e2e8f0]">·</span>
              <a href="https://miguxld.site" target="_blank" class="text-[#be123c] hover:text-[#e11d48] transition underline underline-offset-2">
                miguxld.site
              </a>
            </p>
          </div>
        </div>

        <!-- Botones móvil -->
        <div class="flex items-center gap-1.5 sm:hidden">
          <button onclick="toggleMoreMenu(event)" class="p-2.5 rounded-lg bg-[#ffffff] border border-[#e2e8f0] text-[#64748b] hover:bg-[#f1f5f9] active:scale-95 transition" title="Más opciones" aria-label="Más opciones">
            <i class="fa-solid fa-ellipsis text-xs"></i>
          </button>
          <button onclick="syncJobs()" id="btnSyncMobile" class="p-2.5 rounded-lg bg-[#e11d48] text-white hover:bg-[#be123c] active:scale-95 transition" title="Escanear ahora" aria-label="Escanear ahora">
            <i class="fa-solid fa-arrows-rotate text-xs"></i>
          </button>
        </div>
      </div>

      <!-- Contadores, estado y acciones (escritorio) -->
      <div class="flex items-center gap-3 justify-between sm:justify-end">
        <div class="flex items-center gap-2 text-xs font-mono">
          <div class="px-3 py-1.5 rounded-lg bg-[#ffffff] border border-[#e2e8f0] flex items-center gap-2">
            <span class="w-1.5 h-1.5 rounded-full bg-[#e11d48]"></span>
            <span class="text-[#64748b]">Compatibles:</span>
            <span id="statMatched" class="font-bold text-[#0f172a]">--</span>
          </div>
          <div class="px-3 py-1.5 rounded-lg bg-[#ffffff] border border-[#e2e8f0] flex items-center gap-2">
            <span class="text-[#0ea5e9]"><i class="fa-solid fa-paper-plane text-[10px]"></i></span>
            <span class="text-[#64748b]">Postuladas:</span>
            <span id="statApplied" class="font-bold text-[#0ea5e9]">--</span>
          </div>
          <!-- Alert countdown chip -->
          <div id="alertChip" class="hidden sm:flex px-3 py-1.5 rounded-lg bg-[#ffffff] border border-[#e2e8f0] items-center gap-2 cursor-pointer hover:border-[#e11d48]/40 transition" onclick="triggerAlertScan()" title="Escaneo rápido de vacantes nuevas. Clic para ejecutarlo ahora.">
            <i id="alertIcon" class="fa-solid fa-bell text-[#e8a765] text-[10px] animate-pulse"></i>
            <span class="text-[#64748b]">Alerta:</span>
            <span id="alertCountdown" class="font-bold text-[#e8a765]">--</span>
          </div>
        </div>

        <!-- Estado de sincronización -->
        <div id="syncInfo" class="hidden lg:block text-[11px] leading-tight text-[#64748b] text-right max-w-[190px]"></div>

        <!-- Menú "Más" -->
        <div class="relative hidden sm:block">
          <button onclick="toggleMoreMenu(event)" id="btnMore" class="inline-flex whitespace-nowrap items-center gap-2 px-3 py-2 rounded-lg bg-[#ffffff] hover:bg-[#f1f5f9] text-[#0f172a] border border-[#e2e8f0] font-bold text-xs shadow-sm active:scale-95 transition-all" aria-haspopup="true" aria-expanded="false">
            <i class="fa-solid fa-ellipsis text-[#64748b]"></i>
            <span>Más</span>
          </button>
        </div>

        <button onclick="syncJobs()" id="btnSync" class="hidden sm:inline-flex whitespace-nowrap items-center gap-2 px-4 py-2 rounded-lg bg-[#e11d48] hover:bg-[#be123c] text-white font-bold text-xs tracking-wide shadow-[0_2px_14px_-2px_rgba(225,29,72,0.45)] active:scale-95 transition-all">
          <i class="fa-solid fa-arrows-rotate text-[11px]"></i>
          <span>Escanear Ahora</span>
        </button>
      </div>

    </div>

    <!-- Menú desplegable (fuera del flujo para no recortarse) -->
    <div id="moreMenu" class="hidden absolute right-4 sm:right-6 top-full mt-1 w-72 rounded-xl bg-white border border-[#e2e8f0] shadow-xl z-50 p-1.5 text-xs">
      <button onclick="openRadarBrowser(); closeMoreMenu()" class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#f1f5f9] flex items-start gap-3">
        <i class="fa-solid fa-globe text-[#0ea5e9] mt-0.5"></i>
        <span><b class="text-[#0f172a]">Navegador del radar</b><br><span class="text-[#64748b]">Abre el navegador que usa Indeed, para iniciar sesión o resolver una verificación.</span></span>
      </button>
      <button onclick="syncJobs('indeed'); closeMoreMenu()" class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#f1f5f9] flex items-start gap-3">
        <i class="fa-solid fa-magnifying-glass text-[#2557a7] mt-0.5"></i>
        <span><b class="text-[#0f172a]">Escanear solo Indeed</b><br><span class="text-[#64748b]">Búsqueda completa en Indeed, sin pasar por Computrabajo.</span></span>
      </button>
      <button onclick="syncJobs('computrabajo'); closeMoreMenu()" class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#f1f5f9] flex items-start gap-3">
        <i class="fa-solid fa-magnifying-glass text-[#e11d48] mt-0.5"></i>
        <span><b class="text-[#0f172a]">Escanear solo Computrabajo</b><br><span class="text-[#64748b]">Sin abrir el navegador del radar.</span></span>
      </button>
      <button onclick="syncApplicationsFromPortal(); closeMoreMenu()" class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#f1f5f9] flex items-start gap-3">
        <i class="fa-solid fa-rotate text-[#16a34a] mt-0.5"></i>
        <span><b class="text-[#0f172a]">Traer mis postulaciones</b><br><span class="text-[#64748b]">Lee "Mis postulaciones" de Computrabajo y las marca aquí.</span></span>
      </button>
      <button onclick="openQAModal(); closeMoreMenu()" class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#f1f5f9] flex items-start gap-3">
        <i class="fa-solid fa-bolt text-[#e11d48] mt-0.5"></i>
        <span><b class="text-[#0f172a]">Respuestas ATS</b><br><span class="text-[#64748b]">Respuestas listas para copiar en preguntas de filtro.</span></span>
      </button>
      <button onclick="triggerAlertScan(); closeMoreMenu()" class="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#f1f5f9] flex items-start gap-3">
        <i class="fa-solid fa-bell text-[#e8a765] mt-0.5"></i>
        <span><b class="text-[#0f172a]">Escaneo rápido</b><br><span class="text-[#64748b]">Solo lo publicado en Computrabajo, primera página.</span></span>
      </button>
    </div>

    <!-- Avisos del sistema (bloqueos, sesiones, navegador) -->
    <div id="statusBanner" class="hidden max-w-6xl mx-auto mt-3 space-y-2"></div>
  </header>

  <!-- Barra de Filtros y Búsqueda -->
  <section class="border-b border-[#e2e8f0] bg-[#f8fafc]/95 backdrop-blur px-4 py-3 sm:px-6 sticky top-[65px] sm:top-[65px] z-30 relative">
    <div class="max-w-6xl mx-auto flex flex-col md:flex-row gap-3 items-stretch md:items-center justify-between">
      
      <!-- Tabs Selector Carmesí -->
      <div class="flex items-center gap-1.5 overflow-x-auto custom-scrollbar pb-1 md:pb-0">
        <button onclick="switchTab('matched')" id="tabMatched" class="tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition bg-[#e11d48] text-white shadow-[0_2px_10px_-2px_rgba(225,29,72,0.5)]">
          <i class="fa-solid fa-crosshairs text-[11px]"></i>
          <span>Compatibles</span>
          <span id="badgeMatched" class="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-black/10 text-white">0</span>
        </button>
        <button onclick="switchTab('applied')" id="tabApplied" class="tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition text-[#64748b] hover:text-[#0f172a] border border-transparent hover:border-[#e2e8f0]">
          <i class="fa-solid fa-paper-plane text-[11px] text-[#0ea5e9]"></i>
          <span>Postuladas</span>
          <span id="badgeApplied" class="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-[#f1f5f9] text-[#64748b]">0</span>
        </button>
        <button onclick="switchTab('favorite')" id="tabFavorite" class="tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition text-[#64748b] hover:text-[#0f172a] border border-transparent hover:border-[#e2e8f0]">
          <i class="fa-solid fa-star text-[11px] text-[#e8a765]"></i>
          <span>Guardadas</span>
          <span id="badgeFavorite" class="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-[#f1f5f9] text-[#64748b]">0</span>
        </button>
        <button onclick="switchTab('all')" id="tabAll" class="tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition text-[#64748b] hover:text-[#0f172a] border border-transparent hover:border-[#e2e8f0]">
          <i class="fa-solid fa-layer-group text-[11px]"></i>
          <span>Todas</span>
          <span id="badgeAll" class="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-[#f1f5f9] text-[#64748b]">0</span>
        </button>
        <button onclick="switchTab('discarded')" id="tabDiscarded" class="tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition text-[#64748b] hover:text-[#0f172a] border border-transparent hover:border-[#e2e8f0]">
          <i class="fa-solid fa-trash-can text-[11px]"></i>
          <span>Descartadas</span>
          <span id="badgeDiscarded" class="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-[#f1f5f9] text-[#64748b]">0</span>
        </button>
        <button onclick="switchTab('skills')" id="tabSkills" class="tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition text-[#64748b] hover:text-[#0f172a] border border-transparent hover:border-[#e2e8f0]">
          <i class="fa-solid fa-graduation-cap text-[11px] text-[#a855f7]"></i>
          <span class="whitespace-nowrap">Qué aprender</span>
        </button>
      </div>

      <!-- Buscador y Modalidad -->
      <div class="flex items-center gap-2.5">
        <div class="relative flex-1 md:w-64">
          <i class="fa-solid fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-xs text-[#64748b]"></i>
          <input 
            type="text" 
            id="searchInput" 
            oninput="handleSearch()" 
            placeholder="Buscar cargo, empresa, tech..." 
            class="w-full pl-8 pr-7 py-1.5 rounded-lg bg-[#ffffff] border border-[#e2e8f0] text-xs text-[#0f172a] placeholder-[#64748b] focus:outline-none focus:border-[#e11d48]/60 transition font-sans"
          >
          <button onclick="clearSearch()" id="btnClearSearch" class="hidden absolute right-2.5 top-1/2 -translate-y-1/2 text-[#64748b] hover:text-white text-xs">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>

        <select id="filterModality" onchange="applyFilters()" class="py-1.5 px-3 rounded-lg bg-[#ffffff] border border-[#e2e8f0] text-xs text-[#0f172a] focus:outline-none focus:border-[#e11d48]/60 font-sans">
          <option value="">Modalidad</option>
          <option value="remoto">Remoto</option>
          <option value="híbrido">Híbrido</option>
          <option value="presencial">Presencial</option>
        </select>

        <select id="sortOrder" onchange="loadOffers()" class="py-1.5 px-3 rounded-lg bg-[#ffffff] border border-[#e2e8f0] text-xs text-[#0f172a] focus:outline-none focus:border-[#e11d48]/60 font-sans font-medium">
          <option value="smart" selected>⚡ Oportunidad (Afinidad + Frescura)</option>
          <option value="recent">⏱️ Recién Publicadas</option>
          <option value="score">🎯 Mayor Match Técnico</option>
        </select>
      </div>

    </div>
  </section>

  <!-- Contenido Principal -->
  <main class="flex-1 max-w-6xl w-full mx-auto px-4 py-6 sm:px-6 relative z-10">
    
    <!-- Info Banner -->
    <div class="mb-5 p-3.5 rounded-xl bg-[#ffffff] border border-[#e2e8f0] flex items-center justify-between text-xs">
      <div class="flex items-center gap-2.5">
        <span class="text-[#e11d48]"><i class="fa-solid fa-sliders"></i></span>
        <span id="viewBannerText" class="text-[#64748b]">Vacantes indexadas con análisis de compatibilidad técnica con tu CV.</span>
      </div>
      <span id="resultsCount" class="font-mono text-[#0f172a] font-semibold">0 vacantes</span>
    </div>

    <!-- Panel de seguimiento (pestaña Postuladas) -->
    <div id="trackingPanel" class="hidden mb-5"></div>

    <!-- Panel Qué aprender -->
    <div id="skillsPanel" class="hidden"></div>

    <!-- Grid de Vacantes -->
    <div id="offersGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></div>

    <!-- Empty State -->
    <div id="emptyState" class="hidden py-20 text-center">
      <div class="w-14 h-14 rounded-2xl bg-[#ffffff] border border-[#e2e8f0] flex items-center justify-center mx-auto mb-3.5 text-[#e11d48] text-xl">
        <i class="fa-solid fa-inbox"></i>
      </div>
      <h3 class="text-sm font-bold text-[#0f172a] mb-1">No hay vacantes en esta sección</h3>
      <div id="lastScanSummary" class="hidden max-w-xl mx-auto mb-3 p-3 rounded-lg bg-white border border-[#e2e8f0] text-[11px] text-[#64748b] text-left leading-relaxed"></div>
      <p class="text-xs text-[#64748b] max-w-sm mx-auto mb-4">Probá cambiando de pestaña o realizando un nuevo escaneo.</p>
      <button onclick="syncJobs()" class="px-4 py-2 rounded-lg bg-[#e11d48] hover:bg-[#be123c] text-white font-bold text-xs transition shadow-lg shadow-[#e11d48]/20">
        Escanear Ofertas Ahora
      </button>
    </div>

  </main>

  <!-- Modal / Drawer Lateral de Detalle -->
  <div id="detailModal" class="fixed inset-0 z-50 hidden">
    <div onclick="closeModal()" class="absolute inset-0 bg-black/80 backdrop-blur-xs transition-opacity"></div>
    
    <div class="absolute inset-y-0 right-0 max-w-xl w-full bg-[#ffffff] border-l border-[#e2e8f0] shadow-2xl flex flex-col z-10 transform transition-transform duration-200 translate-x-full" id="drawerPanel">
      
      <!-- Header Modal -->
      <div class="p-5 border-b border-[#e2e8f0] flex items-start justify-between gap-3 bg-[#f8fafc]">
        <div class="flex-1">
          <div class="flex items-center gap-2 mb-2 flex-wrap text-[11px] font-mono">
            <span id="modalPortal" class="px-2 py-0.5 rounded bg-[#f1f5f9] text-[#be123c] border border-[#e2e8f0] uppercase font-semibold">Computrabajo</span>
            <span id="modalScoreBadge" class="px-2.5 py-0.5 rounded-full bg-[#e11d48] text-white font-bold shadow-[0_2px_8px_-2px_rgba(225,29,72,0.4)]">95% Match</span>
            <span id="modalTimeBadge" class="px-2 py-0.5 rounded bg-[#f1f5f9] text-[#64748b] border border-[#e2e8f0]">Publicado hoy</span>
          </div>
          <h2 id="modalTitle" class="text-base font-bold text-[#0f172a] leading-snug">Título de la Vacante</h2>
          <p id="modalCompany" class="text-xs text-[#be123c] font-semibold mt-1">Empresa</p>
        </div>
        <button onclick="closeModal()" class="w-8 h-8 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] border border-[#e2e8f0] flex items-center justify-center text-[#64748b] hover:text-white transition">
          <i class="fa-solid fa-xmark text-sm"></i>
        </button>
      </div>

      <!-- Body Modal -->
      <div class="flex-1 overflow-y-auto p-5 custom-scrollbar space-y-4 text-xs">
        
        <!-- Info Strip -->
        <div class="grid grid-cols-2 gap-2.5">
          <div class="p-3 rounded-xl bg-[#f1f5f9] border border-[#e2e8f0]">
            <span class="text-[10px] font-mono text-[#64748b] uppercase block mb-0.5">Rango Salarial</span>
            <span id="modalSalary" class="font-bold text-[#0f172a]">A convenir</span>
          </div>
          <div class="p-3 rounded-xl bg-[#f1f5f9] border border-[#e2e8f0]">
            <span class="text-[10px] font-mono text-[#64748b] uppercase block mb-0.5">Ubicación / Modalidad</span>
            <span id="modalLocation" class="font-bold text-[#0f172a]">Bogotá</span>
          </div>
        </div>

        <!-- Diagnóstico de Afinidad -->
        <div class="p-4 rounded-xl bg-[#ffe4e6]/50 border border-[#e11d48]/30">
          <div class="flex items-center gap-2 mb-2">
            <i class="fa-solid fa-wand-magic-sparkles text-[#be123c] text-xs"></i>
            <span class="text-[11px] font-mono uppercase text-[#be123c] font-bold tracking-wider">Diagnóstico de Afinidad</span>
          </div>
          <p id="modalMatchReason" class="text-[#0f172a] leading-relaxed"></p>
        </div>

        <!-- Requisitos Detectados -->
        <div>
          <span class="text-[11px] font-mono uppercase text-[#64748b] block mb-2 font-bold tracking-wider">Tecnologías & Requisitos</span>
          <div id="modalRequirements" class="flex flex-wrap gap-1.5"></div>
        </div>

        <!-- Descripción Completa -->
        <div>
          <span class="text-[11px] font-mono uppercase text-[#64748b] block mb-2 font-bold tracking-wider">Descripción Completa del Puesto</span>
          <div id="modalDescription" class="p-4 rounded-xl bg-[#f1f5f9] border border-[#e2e8f0] text-[#0f172a] leading-relaxed whitespace-pre-line"></div>
        </div>

      </div>

      <!-- Footer Modal -->
      <div class="p-4 border-t border-[#e2e8f0] bg-[#f8fafc] flex items-center justify-between gap-3">
        <div class="flex items-center gap-1.5">
          <button id="modalBtnFav" class="p-2.5 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#64748b] hover:text-[#e8a765] border border-[#e2e8f0] transition" title="Guardar">
            <i class="fa-regular fa-star"></i>
          </button>
          <button id="modalBtnDiscard" class="p-2.5 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#64748b] hover:text-[#e5484d] border border-[#e2e8f0] transition" title="Descartar">
            <i class="fa-solid fa-trash-can"></i>
          </button>
        </div>

        <div class="flex items-center gap-2">
          <button id="modalBtnApplied" class="px-4 py-2 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#0f172a] border border-[#e2e8f0] font-bold text-xs flex items-center gap-1.5 transition">
            <i class="fa-solid fa-check text-[11px]"></i>
            <span>Marcar Postulada</span>
          </button>
          <a id="modalBtnApplyLink" href="#" target="_blank" rel="noreferrer noopener" class="px-4 py-2 rounded-lg bg-[#e11d48] hover:bg-[#be123c] text-white font-bold text-xs flex items-center gap-1.5 transition shadow-[0_2px_12px_-2px_rgba(225,29,72,0.4)]">
            <span>Abrir en App</span>
            <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
          </a>
        </div>
      </div>

    </div>
  </div>

  <!-- Modal Banco de Respuestas Rápidas (ATS / Screening) -->
  <div id="qaModal" class="fixed inset-0 z-50 bg-[#000000]/80 backdrop-blur-sm hidden flex items-center justify-center p-3 sm:p-4 overflow-y-auto">
    <div class="relative w-full max-w-3xl bg-[#ffffff] border border-[#e2e8f0] rounded-2xl shadow-2xl overflow-hidden my-6 flex flex-col max-h-[90vh]">
      
      <!-- QA Modal Header -->
      <div class="p-4 sm:p-5 border-b border-[#e2e8f0] bg-[#ffffff] flex items-center justify-between gap-3 shrink-0">
        <div>
          <h2 class="text-base font-bold text-[#0f172a] flex items-center gap-2">
            <span class="p-1.5 rounded-lg bg-[#ffe4e6] text-[#be123c] border border-[#e11d48]/30">
              <i class="fa-solid fa-bolt text-xs"></i>
            </span>
            <span>Banco de Respuestas Rápidas (ATS / Screening)</span>
          </h2>
          <p class="text-xs text-[#64748b] mt-0.5">Respuestas calibradas de tu vault para copiar en 1 clic (todas &lt;500 caracteres).</p>
        </div>
        <button onclick="closeQAModal()" class="w-8 h-8 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#64748b] hover:text-[#0f172a] border border-[#e2e8f0] flex items-center justify-center transition">
          <i class="fa-solid fa-xmark text-sm"></i>
        </button>
      </div>

      <!-- QA Search & Match Bar -->
      <div class="p-4 border-b border-[#e2e8f0] bg-[#ffffff] shrink-0">
        <div class="relative">
          <i class="fa-solid fa-wand-magic-sparkles text-xs text-[#be123c] absolute left-3.5 top-1/2 -translate-y-1/2"></i>
          <input 
            type="text" 
            id="qaSearchInput" 
            placeholder="¿Te hicieron una pregunta en la postulación? Pegala acá para auto-matchear..." 
            oninput="handleQASearch(this.value)"
            class="w-full pl-10 pr-4 py-2.5 rounded-xl bg-[#f8fafc] border border-[#e2e8f0] focus:border-[#e11d48]/60 text-xs text-[#0f172a] placeholder-[#64748b]/60 focus:outline-none transition shadow-inner font-sans"
          />
        </div>
      </div>

      <!-- QA Cards Container -->
      <div id="qaCardsContainer" class="p-4 sm:p-5 overflow-y-auto space-y-3.5 custom-scrollbar grow">
        <!-- Rendered dinámicamente -->
      </div>

    </div>
  </div>

  <script>
    let allOffers = [];
    let currentTab = 'matched';

    function getRelativeTime(postedDate, scrapedAt) {
      if (postedDate && postedDate.trim() && !postedDate.includes('None')) {
        return postedDate.trim();
      }
      if (!scrapedAt) return 'Reciente';

      const now = new Date();
      const scraped = new Date(scrapedAt);
      const diffMs = now - scraped;
      const diffMinutes = Math.floor(diffMs / (1000 * 60));
      const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
      const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

      if (diffMinutes < 60) {
        return diffMinutes <= 5 ? 'Hace unos minutos' : `Hace ${diffMinutes} min`;
      }
      if (diffHours < 24) {
        return diffHours === 1 ? 'Hace 1 hora' : `Hace ${diffHours} horas`;
      }
      if (diffDays === 1) return 'Ayer';
      if (diffDays < 7) return `Hace ${diffDays} días`;
      if (diffDays < 30) {
        const weeks = Math.floor(diffDays / 7);
        return weeks === 1 ? 'Hace 1 semana' : `Hace ${weeks} semanas`;
      }
      const months = Math.floor(diffDays / 30);
      return months === 1 ? 'Hace 1 mes' : `Hace ${months} meses`;
    }

    async function loadStats() {
      try {
        const res = await fetch('/api/stats');
        const stats = await res.json();
        document.getElementById('statMatched').textContent = stats.matched || 0;
        document.getElementById('statApplied').textContent = stats.applied || 0;
        document.getElementById('badgeMatched').textContent = stats.matched || 0;
        document.getElementById('badgeApplied').textContent = stats.applied || 0;
        document.getElementById('badgeFavorite').textContent = stats.favorite || 0;
        document.getElementById('badgeAll').textContent = stats.total || 0;
        document.getElementById('badgeDiscarded').textContent = stats.discarded || 0;
      } catch (err) {
        console.error(err);
      }
    }

    async function loadOffers() {
      if (currentTab === 'skills') { await loadStats(); return; }
      try {
        const sort = document.getElementById('sortOrder') ? document.getElementById('sortOrder').value : 'smart';
        const url = currentTab === 'all' 
          ? `/api/offers?sort_by=${sort}` 
          : `/api/offers?status=${currentTab}&sort_by=${sort}`;
        const res = await fetch(url);
        allOffers = await res.json();
        applyFilters();
        await loadStats();
      } catch (err) {
        showToast('Error cargando vacantes', 'error');
      }
    }

    function switchTab(tab) {
      currentTab = tab;
      
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.className = 'tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition text-[#64748b] hover:text-[#0f172a] border border-transparent hover:border-[#e2e8f0]';
      });

      const activeBtn = document.getElementById(`tab${tab.charAt(0).toUpperCase() + tab.slice(1)}`);
      if (activeBtn) {
        activeBtn.className = 'tab-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 transition bg-[#e11d48] text-white shadow-[0_2px_10px_-2px_rgba(225,29,72,0.5)]';
      }

      const bannerText = document.getElementById('viewBannerText');
      const isSkills = tab === 'skills';
      document.getElementById('skillsPanel').classList.toggle('hidden', !isSkills);
      document.getElementById('offersGrid').classList.toggle('hidden', isSkills);
      document.getElementById('trackingPanel').classList.toggle('hidden', tab !== 'applied');
      if (isSkills) {
        document.getElementById('emptyState').classList.add('hidden');
        document.getElementById('resultsCount').textContent = '';
        bannerText.textContent = 'Tecnologías que más piden las vacantes que el radar considera afines, contra tu stack del Perfil Maestro.';
        loadSkills();
        return;
      }
      if (tab === 'applied') loadTracking();
      if (tab === 'matched') {
        bannerText.textContent = 'Vacantes analizadas con alta afinidad hacia tu CV (desarrollo, frontend/backend, datos, soporte L2).';
      } else if (tab === 'applied') {
        bannerText.textContent = 'Historial de vacantes a las que te has postulado activamente.';
      } else if (tab === 'favorite') {
        bannerText.textContent = 'Vacantes guardadas para postularte más adelante.';
      } else if (tab === 'discarded') {
        bannerText.textContent = 'Vacantes descartadas del radar.';
      } else {
        bannerText.textContent = 'Todas las ofertas indexadas en la base de datos local.';
      }

      loadOffers();
    }

    function applyFilters() {
      const searchInputEl = document.getElementById('searchInput');
      const modalityEl = document.getElementById('filterModality');
      const search = searchInputEl ? searchInputEl.value.toLowerCase().trim() : '';
      const modality = modalityEl ? modalityEl.value.toLowerCase().trim() : '';

      const filtered = (allOffers || []).filter(job => {
        if (!job) return false;
        const title = (job.title || '').toLowerCase();
        const company = (job.company || '').toLowerCase();
        const desc = (job.description || '').toLowerCase();
        const loc = (job.location || '').toLowerCase();
        const mod = (job.modality || '').toLowerCase();

        const matchesSearch = !search || 
          title.includes(search) || 
          company.includes(search) ||
          desc.includes(search) ||
          (Array.isArray(job.requirements) && job.requirements.some(r => typeof r === 'string' && r.toLowerCase().includes(search)));

        // Buscar modalidad en tags, ubicación o en el texto real de la descripción
        const matchesModality = !modality || 
          mod.includes(modality) ||
          loc.includes(modality) ||
          desc.includes(modality) ||
          title.includes(modality);

        return matchesSearch && matchesModality;
      });

      renderOffers(filtered);
    }

    function handleSearch() {
      const val = document.getElementById('searchInput').value;
      document.getElementById('btnClearSearch').classList.toggle('hidden', !val);
      applyFilters();
    }

    function clearSearch() {
      document.getElementById('searchInput').value = '';
      document.getElementById('btnClearSearch').classList.add('hidden');
      applyFilters();
    }

    function renderOffers(offers) {
      const grid = document.getElementById('offersGrid');
      const empty = document.getElementById('emptyState');
      const count = document.getElementById('resultsCount');

      count.textContent = `${offers.length} vacantes`;

      if (offers.length === 0) {
        grid.innerHTML = '';
        empty.classList.remove('hidden');
        return;
      }

      empty.classList.add('hidden');
      grid.innerHTML = offers.map(job => {
        const score = job.match_score ? Math.round(job.match_score) : 0;
        const timeLabel = getRelativeTime(job.posted_date, job.scraped_at);
        const isApplied = job.status === 'applied';
        const isFav = job.status === 'favorite';
        // "NUEVA" badge: scraped in the last 2 hours
        const isHot = job.scraped_at && (Date.now() - new Date(job.scraped_at + 'Z').getTime()) < 2 * 60 * 60 * 1000;

        let scoreBadge = 'bg-[#f1f5f9] text-[#64748b] border-[#e2e8f0]';
        if (score >= 80) scoreBadge = 'bg-[#e11d48] text-white shadow-[0_2px_8px_-2px_rgba(225,29,72,0.5)]';
        else if (score >= 60) scoreBadge = 'bg-[#0ea5e9] text-white shadow-[0_2px_8px_-2px_rgba(14,165,233,0.5)]';

        return `
          <div class="spotlight-card rounded-xl p-4 flex flex-col justify-between">
            
            <div>
              <!-- Top Row: Portal, Time & Match -->
              <div class="flex items-center justify-between gap-2 mb-2.5 text-[11px] font-mono border-b border-[#e2e8f0] pb-2.5">
                <div class="flex items-center gap-1.5 flex-wrap">
                  <span class="font-mono text-[11px] uppercase tracking-[0.12em] text-[#be123c] font-bold">${job.portal}</span>
                  <span class="text-[#e2e8f0]">·</span>
                  <span class="text-[#64748b] flex items-center gap-1 font-mono text-[11px]">
                    <i class="fa-regular fa-clock text-[10px]"></i>
                    ${timeLabel}
                  </span>
                  ${isApplied ? '<span class="inline-flex items-center gap-1 rounded-full bg-[#0ea5e9]/20 text-[#38bdf8] border border-[#0ea5e9]/40 px-2 py-0.2 font-mono text-[10px] font-bold uppercase"><span class="w-1.5 h-1.5 rounded-full bg-[#38bdf8]"></span>Postulado</span>' : ''}
                  ${isFav ? '<span class="inline-flex items-center gap-1 rounded-full bg-[#e8a765]/20 text-[#e8a765] border border-[#e8a765]/40 px-2 py-0.2 font-mono text-[10px] font-bold uppercase"><i class="fa-solid fa-star text-[9px]"></i>Guardada</span>' : ''}
                  ${isHot ? '<span class="inline-flex items-center gap-1 rounded-full bg-[#e11d48]/20 text-[#be123c] border border-[#e11d48]/40 px-2 py-0.2 font-mono text-[10px] font-bold uppercase animate-pulse"><i class="fa-solid fa-fire text-[9px]"></i>NUEVA</span>' : ''}
                </div>
                <button onclick="openModal('${job.id}')" class="px-2 py-0.5 rounded-full text-[11px] font-mono font-bold flex items-center gap-1 transition ${scoreBadge}" title="Ver análisis de afinidad">
                  <span>${score}%</span>
                </button>
              </div>

              <!-- Title & Company (Enlace directo nativo a la app) -->
              <a href="${job.url}" target="_blank" rel="noreferrer noopener" class="block group mb-1">
                <h3 class="text-sm font-bold text-[#0f172a] group-hover:text-[#be123c] leading-snug line-clamp-2 transition flex items-start justify-between gap-2">
                  <span class="link-underline">${escapeHtml(job.title)}</span>
                  <i class="fa-solid fa-arrow-up-right-from-square text-[10px] text-[#64748b] group-hover:text-[#be123c] mt-1 shrink-0 transition"></i>
                </h3>
              </a>
              <p class="text-xs text-[#64748b] font-medium mb-3 truncate flex items-center gap-1.5">
                <i class="fa-regular fa-building text-[10px] text-[#e11d48]"></i>
                <span>${escapeHtml(job.company)}</span>
              </p>

              <!-- Tags de Información -->
              <div class="flex flex-wrap items-center gap-1.5 mb-3 text-[11px]">
                <span class="px-2 py-0.5 rounded-md bg-[#f1f5f9] border border-[#e2e8f0] text-[#0f172a] font-mono">
                  ${escapeHtml(job.salary || 'A convenir')}
                </span>
                <span class="px-2 py-0.5 rounded-md bg-[#f1f5f9] border border-[#e2e8f0] text-[#64748b]">
                  ${escapeHtml(job.location || 'Bogotá')}
                </span>
                <span class="px-2 py-0.5 rounded-md ${
                  (job.modality || '').toLowerCase().includes('remoto') ? 'bg-[#0ea5e9]/15 text-[#38bdf8] border border-[#0ea5e9]/30' :
                  (job.modality || '').toLowerCase().includes('híbrido') || (job.modality || '').toLowerCase().includes('hibrido') ? 'bg-[#a855f7]/15 text-[#c084fc] border border-[#a855f7]/30' :
                  'bg-[#f1f5f9] border border-[#e2e8f0] text-[#64748b]'
                }">
                  <i class="fa-solid ${(job.modality || '').toLowerCase().includes('remoto') ? 'fa-house-laptop' : (job.modality || '').toLowerCase().includes('híbrido') || (job.modality || '').toLowerCase().includes('hibrido') ? 'fa-building-user' : 'fa-building'} text-[10px] mr-1"></i>
                  ${escapeHtml(job.modality || 'Presencial')}
                </span>
              </div>

              <!-- Match Reason Clickable -->
              ${job.match_reason ? `
                <div onclick="openModal('${job.id}')" class="cursor-pointer text-[11px] text-[#64748b] hover:text-[#0f172a] line-clamp-2 mb-3 bg-[#f1f5f9]/80 hover:bg-[#f1f5f9] p-2.5 rounded-lg border border-[#e2e8f0] leading-relaxed transition">
                  <span class="text-[#be123c] font-mono font-bold">Match:</span> ${escapeHtml(job.match_reason)}
                </div>
              ` : ''}
            </div>

            ${isApplied ? renderTrackingControls(job) : ''}

            <!-- Card Actions -->
            <div class="pt-3 border-t border-[#e2e8f0] flex items-center justify-between gap-2">
              <div class="flex items-center gap-1">
                <button onclick="setJobStatus('${job.id}', '${isFav ? 'matched' : 'favorite'}')" class="p-2 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#64748b] hover:text-[#e8a765] border border-[#e2e8f0] transition" title="Guardar">
                  <i class="fa-${isFav ? 'solid text-[#e8a765]' : 'regular'} fa-star text-xs"></i>
                </button>
                <button onclick="setJobStatus('${job.id}', 'discarded')" class="p-2 rounded-lg bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#64748b] hover:text-[#e5484d] border border-[#e2e8f0] transition" title="Descartar">
                  <i class="fa-solid fa-trash-can text-xs"></i>
                </button>
              </div>

              <div class="flex items-center gap-1.5">
                <button onclick="setJobStatus('${job.id}', '${isApplied ? 'matched' : 'applied'}')" class="px-3 py-1.5 rounded-lg ${isApplied ? 'bg-[#0ea5e9]/20 text-[#38bdf8] border border-[#0ea5e9]/40' : 'bg-[#f1f5f9] hover:bg-[#e2e8f0] text-[#0f172a] border border-[#e2e8f0]'} text-xs font-semibold transition flex items-center gap-1.5" title="${isApplied ? 'Toca para desmarcar postulación' : 'Marcar como postulado'}">
                  <i class="fa-solid ${isApplied ? 'fa-check text-[#38bdf8]' : 'fa-plus text-[10px] text-[#e11d48]'}"></i>
                  <span>${isApplied ? 'Postulado' : 'Postular'}</span>
                </button>
                <a href="${job.url}" target="_blank" rel="noreferrer noopener" class="px-3 py-1.5 rounded-lg bg-[#e11d48] hover:bg-[#be123c] text-white font-bold text-xs transition flex items-center gap-1.5 shadow-[0_2px_10px_-2px_rgba(225,29,72,0.4)]" title="Abrir en App">
                  <span>Abrir</span>
                  <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
                </a>
              </div>
            </div>

          </div>
        `;
      }).join('');
    }

    function openModal(id) {
      const job = allOffers.find(j => j.id === id);
      if (!job) return;

      document.getElementById('modalPortal').textContent = job.portal;
      document.getElementById('modalTitle').textContent = job.title;
      document.getElementById('modalCompany').textContent = job.company;
      document.getElementById('modalSalary').textContent = job.salary || 'A convenir';
      document.getElementById('modalLocation').textContent = `${job.location || 'Bogotá'} · ${job.modality || 'No especificada'}`;
      
      const score = job.match_score ? Math.round(job.match_score) : 0;
      document.getElementById('modalScoreBadge').textContent = `${score}% Match`;
      document.getElementById('modalTimeBadge').textContent = getRelativeTime(job.posted_date, job.scraped_at);
      document.getElementById('modalMatchReason').textContent = job.match_reason || 'Sin análisis detallado.';
      document.getElementById('modalDescription').textContent = job.description;

      const reqsContainer = document.getElementById('modalRequirements');
      if (job.requirements && job.requirements.length > 0) {
        reqsContainer.innerHTML = job.requirements.map(r => `
          <span class="px-2.5 py-1 rounded-lg text-xs font-mono bg-[#f1f5f9] border border-[#e2e8f0] text-[#0f172a]">${escapeHtml(r)}</span>
        `).join('');
      } else {
        reqsContainer.innerHTML = '<span class="text-xs text-[#64748b] italic">No se especificaron requisitos detallados.</span>';
      }

      document.getElementById('modalBtnApplyLink').href = job.url;
      
      document.getElementById('modalBtnFav').onclick = () => {
        const next = job.status === 'favorite' ? 'matched' : 'favorite';
        setJobStatus(job.id, next);
        closeModal();
      };
      
      document.getElementById('modalBtnDiscard').onclick = () => {
        setJobStatus(job.id, 'discarded');
        closeModal();
      };

      const btnApplied = document.getElementById('modalBtnApplied');
      btnApplied.innerHTML = job.status === 'applied' 
        ? '<i class="fa-solid fa-arrow-rotate-left text-[11px]"></i><span>Desmarcar Postulación</span>' 
        : '<i class="fa-solid fa-check text-[11px]"></i><span>Marcar Postulada</span>';

      btnApplied.onclick = () => {
        const next = job.status === 'applied' ? 'matched' : 'applied';
        setJobStatus(job.id, next);
        closeModal();
      };

      const modal = document.getElementById('detailModal');
      const panel = document.getElementById('drawerPanel');
      modal.classList.remove('hidden');
      setTimeout(() => {
        panel.classList.remove('translate-x-full');
      }, 10);
    }

    function closeModal() {
      const modal = document.getElementById('detailModal');
      const panel = document.getElementById('drawerPanel');
      panel.classList.add('translate-x-full');
      setTimeout(() => {
        modal.classList.add('hidden');
      }, 200);
    }

    async function setJobStatus(id, newStatus) {
      try {
        const res = await fetch(`/api/offers/${id}/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: newStatus })
        });
        if (res.ok) {
          const msg = newStatus === 'applied' ? 'Marcada como Postulada 🚀' : 
                      newStatus === 'matched' ? 'Devuelta a Compatibles' :
                      newStatus === 'favorite' ? 'Guardada en Favoritas ⭐' :
                      newStatus === 'discarded' ? 'Vacante Descartada 🗑️' : 'Estado actualizado';
          showToast(msg, 'success');
          loadOffers();
        }
      } catch (err) {
        showToast('Error al actualizar estado', 'error');
      }
    }

    let syncPollInterval = null;

    async function syncJobs(portal) {
      const btn = document.getElementById('btnSync');
      const btnMob = document.getElementById('btnSyncMobile');
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-[11px]"></i> <span>Iniciando...</span>';
      btn.disabled = true;
      if (btnMob) btnMob.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-xs"></i>';

      try {
        const res = await fetch('/api/sync?portal=' + encodeURIComponent(portal || 'all'), { method: 'POST' });
        const data = await res.json();
        showToast(data.message || 'Rastreo iniciado en segundo plano. Monitoreando...', 'info');
        startSyncPolling();
      } catch (err) {
        showToast('Error conectando al servidor', 'error');
        btn.innerHTML = '<i class="fa-solid fa-arrows-rotate text-[11px]"></i> <span>Escanear Ahora</span>';
        btn.disabled = false;
        if (btnMob) btnMob.innerHTML = '<i class="fa-solid fa-arrows-rotate text-xs"></i>';
      }
    }

    function startSyncPolling() {
      if (syncPollInterval) clearInterval(syncPollInterval);
      const btn = document.getElementById('btnSync');
      const btnMob = document.getElementById('btnSyncMobile');
      btn.disabled = true;

      syncPollInterval = setInterval(async () => {
        try {
          const res = await fetch('/api/sync/status');
          const status = await res.json();

          if (status.is_running) {
            btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin text-[11px]"></i> <span>Escaneando (${status.current_index}/${status.total_queries})...</span>`;
            if (btnMob) btnMob.innerHTML = `<i class="fa-solid fa-spinner fa-spin text-xs"></i>`;
            const banner = document.getElementById('viewBannerText');
            if (banner && status.current_query) {
              banner.innerHTML = `<span class="text-[#be123c] font-mono font-bold animate-pulse">Rastreando (${status.current_index}/${status.total_queries}):</span> ${escapeHtml(status.current_query)} · <span class="text-[#0f172a] font-semibold">+${status.new_offers} nuevas ofertas</span>` + (status.note ? ` · <span class="font-bold text-[#b45309]">${escapeHtml(status.note)}</span>` : '');
            }
          } else {
            clearInterval(syncPollInterval);
            syncPollInterval = null;
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-arrows-rotate text-[11px]"></i> <span>Escanear Ahora</span>';
            if (btnMob) btnMob.innerHTML = '<i class="fa-solid fa-arrows-rotate text-xs"></i>';
            const banner = document.getElementById('viewBannerText');
            if (banner) {
              banner.textContent = 'Vacantes analizadas con alta afinidad hacia tu CV (desarrollo, frontend/backend, datos, soporte L2).';
            }

            if (status.last_error) {
              showToast(`Aviso: ${status.last_error}`, 'warning');
            } else if (status.new_offers > 0) {
              showToast(`¡Rastreo finalizado! +${status.new_offers} nuevas ofertas compatibles`, 'success');
            } else {
              showToast('Rastreo finalizado: no hay vacantes nuevas compatibles desde el último escaneo', 'info');
            }
            loadOffers();
            loadStats();
            refreshStatusBanner();
          }
        } catch (e) {
          console.error("Error polling sync status:", e);
        }
      }, 1800);
    }

    // -----------------------------------------------------------------------
    // ALERT SCANNER — Lógica de "Primero en Llegar"
    // -----------------------------------------------------------------------

    let alertCountdownInterval = null;
    let alertPrevNewCount = 0;

    function formatCountdown(ms) {
      if (ms <= 0) return 'ahora';
      const totalSec = Math.floor(ms / 1000);
      const m = Math.floor(totalSec / 60);
      const s = totalSec % 60;
      return m > 0 ? `${m}m ${s}s` : `${s}s`;
    }

    async function requestNotificationPermission() {
      if ('Notification' in window && Notification.permission === 'default') {
        await Notification.requestPermission();
      }
    }

    function sendBrowserNotification(title, body) {
      if ('Notification' in window && Notification.permission === 'granted') {
        const n = new Notification(title, {
          body,
          icon: 'https://miguxld.site/favicon.ico',
          badge: 'https://miguxld.site/favicon.ico',
          tag: 'radar-empleo-alert',
          requireInteraction: true,
        });
        n.onclick = () => { window.focus(); n.close(); loadOffers(); };
      }
    }

    async function pollAlertStatus() {
      try {
        const res = await fetch('/api/alert/status');
        const st = await res.json();

        const chip = document.getElementById('alertChip');
        const countdown = document.getElementById('alertCountdown');
        const icon = document.getElementById('alertIcon');

        if (!chip) return;
        chip.classList.remove('hidden');

        if (st.is_running) {
          icon.className = 'fa-solid fa-bell-ring text-[#e11d48] text-[10px] animate-spin';
          countdown.textContent = 'escaneando...';
          countdown.className = 'font-bold text-[#be123c]';
        } else {
          icon.className = 'fa-solid fa-bell text-[#e8a765] text-[10px] animate-pulse';
          const nextMs = st.next_scan_at ? new Date(st.next_scan_at).getTime() - Date.now() : null;
          countdown.textContent = nextMs !== null ? `próx. ${formatCountdown(nextMs)}` : '--';
          countdown.className = 'font-bold text-[#e8a765]';
        }

        // Si hay nuevas vacantes detectadas por el alert, notificar
        const currentNew = st.last_new_count || 0;
        if (currentNew > 0 && currentNew !== alertPrevNewCount && !st.is_running) {
          alertPrevNewCount = currentNew;
          showToast(`🔥 ¡${currentNew} vacante${currentNew > 1 ? 's nuevas' : ' nueva'} detectada${currentNew > 1 ? 's' : ''}!`, 'success');
          sendBrowserNotification(
            `🔥 ${currentNew} vacante${currentNew > 1 ? 's nuevas' : ' nueva'} — Radar de Empleo`,
            `Se detectaron ${currentNew} nueva${currentNew > 1 ? 's vacantes compatibles' : ' vacante compatible'} con tu perfil. ¡Postulate ahora!`
          );
          loadOffers();
          loadStats();
        }
      } catch (e) {
        console.error('[AlertScanner] Poll error:', e);
      }
    }

    async function triggerAlertScan() {
      const countdown = document.getElementById('alertCountdown');
      if (countdown) countdown.textContent = 'iniciando...';
      try {
        const res = await fetch('/api/alert/scan-now', { method: 'POST' });
        const data = await res.json();
        showToast(data.message || 'Escaneo de alerta iniciado', 'info');
      } catch (e) {
        showToast('Error al iniciar alerta manual', 'error');
      }
    }

    function startAlertPolling() {
      // Poll cada 15 segundos para actualizar el countdown y detectar nuevas vacantes
      pollAlertStatus();
      setInterval(pollAlertStatus, 15000);
    }

    function showToast(message, type = 'info') {
      const container = document.getElementById('toastContainer');
      const toast = document.createElement('div');
      
      let border = 'border-[#e2e8f0] bg-[#ffffff] text-[#0f172a]';
      if (type === 'success') border = 'border-[#e11d48]/50 bg-[#ffffff] text-[#be123c]';
      if (type === 'error') border = 'border-[#e5484d]/50 bg-[#ffffff] text-[#e5484d]';

      toast.className = `pointer-events-auto px-4 py-2.5 rounded-xl border ${border} shadow-2xl text-xs font-semibold flex items-center gap-2 transition-all transform duration-200 translate-y-2 opacity-0 font-sans`;
      toast.innerHTML = `<span>${escapeHtml(message)}</span>`;
      container.appendChild(toast);

      setTimeout(() => {
        toast.classList.remove('translate-y-2', 'opacity-0');
      }, 10);

      setTimeout(() => {
        toast.classList.add('translate-y-2', 'opacity-0');
        setTimeout(() => toast.remove(), 250);
      }, 3000);
    }

    // ---------------- Menú "Más" y avisos del sistema ----------------
    function toggleMoreMenu(ev) {
      if (ev) ev.stopPropagation();
      const m = document.getElementById('moreMenu');
      const open = m.classList.toggle('hidden') === false;
      const b = document.getElementById('btnMore');
      if (b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function closeMoreMenu() {
      document.getElementById('moreMenu').classList.add('hidden');
      const b = document.getElementById('btnMore');
      if (b) b.setAttribute('aria-expanded', 'false');
    }
    document.addEventListener('click', (e) => {
      const m = document.getElementById('moreMenu');
      if (m && !m.classList.contains('hidden') && !m.contains(e.target)) closeMoreMenu();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMoreMenu(); });

    function hhmm(iso) {
      return new Date(iso).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Bogota' });
    }
    function relTime(iso) {
      const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
      if (min < 1) return 'hace un momento';
      if (min < 60) return `hace ${min} min`;
      const h = Math.floor(min / 60);
      if (h < 24) return `hace ${h} h`;
      const d = Math.floor(h / 24);
      return d === 1 ? 'ayer' : `hace ${d} días`;
    }

    function reportSummary(rep) {
      if (!rep || !rep.portales) return '';
      const parts = Object.entries(rep.portales).map(([p, r]) => {
        if (r.error && !r.vistas) return `<b>${p}</b>: <span class="text-[#b45309]">${escapeHtml(r.error)}</span>`;
        return `<b>${p}</b>: ${r.vistas || 0} revisadas · ${r.ya_conocidas || 0} ya conocidas · ${r.viejas || 0} de más de 2 días · ${r.titulo_no_afin || 0} no afines · <b>${r.compatibles || 0} nuevas compatibles</b>`;
      });
      return parts.join('<br>');
    }

    async function refreshStatusBanner() {
      const get = (u) => fetch(u).then(r => r.json()).catch(() => null);
      const [g, s, d, b, rep] = await Promise.all([
        get('/api/guard/status'), get('/api/sync/status'), get('/api/daily/status'), get('/api/browser/status'), get('/api/sync/report')
      ]);
      const summaryEl = document.getElementById('lastScanSummary');
      if (summaryEl) {
        const html = reportSummary(rep);
        summaryEl.innerHTML = html ? `<div class="font-semibold text-[#0f172a] mb-1">Último escaneo (${relTime(rep.finished_at)})</div>${html}` : '';
        summaryEl.classList.toggle('hidden', !html);
      }
      // --- estado de sincronización (encabezado)
      const info = document.getElementById('syncInfo');
      if (info && d) {
        const bogotaDay = (dt) => dt.toLocaleDateString('en-CA', { timeZone: 'America/Bogota' });
        let last = 'Sin sincronizar aún';
        let nextTxt = '';
        if (d.last_full_sync_at) {
          last = 'Sincronizado ' + relTime(d.last_full_sync_at);
          const doneToday = bogotaDay(new Date(d.last_full_sync_at)) === bogotaDay(new Date());
          nextTxt = `Próxima automática: ${doneToday ? 'mañana' : 'hoy'} ${d.time}`;
        } else {
          nextTxt = `Próxima automática: ${d.time}`;
        }
        info.innerHTML = `<div class="font-semibold text-[#0f172a]">${last}</div><div>${nextTxt}</div>`;
        info.classList.remove('hidden');
      }
      // --- avisos
      const items = [];
      const openBtn = { label: 'Abrir navegador del radar', fn: 'openRadarBrowser()' };
      if (g && g.portals) {
        Object.entries(g.portals).forEach(([portal, st]) => {
          if (st.needs_verification) {
            const name = portal.charAt(0).toUpperCase() + portal.slice(1);
            items.push({
              level: 'warn',
              title: `${name} pide una verificación`,
              text: 'No es un bloqueo: Cloudflare quiere confirmar que eres una persona. Abre el navegador del radar, resuélvela una vez y vuelve a escanear.',
              action: openBtn
            });
          } else if (st.blocked) {
            const name = portal.charAt(0).toUpperCase() + portal.slice(1);
            items.push({
              level: 'error',
              title: `${name} está en pausa de seguridad hasta las ${hhmm(st.until)}`,
              text: 'El portal rechazó las peticiones. El radar no lo consultará hasta entonces para proteger tu IP.',
              action: { label: 'Levantar pausa', fn: `clearPause('${portal}')` }
            });
          }
        });
      }
      if (s && s.portal_errors) {
        Object.entries(s.portal_errors).forEach(([portal, msg]) => {
          const gp = g && g.portals && g.portals[portal.toLowerCase()];
          if (gp && (gp.blocked || gp.needs_verification)) return; // ya avisado arriba
          items.push({
            level: 'warn', title: `Última sincronización: problema con ${portal}`, text: msg,
            action: portal === 'Indeed' ? openBtn : null
          });
        });
      }
      if (b && !b.installed) {
        items.push({ level: 'warn', title: 'No se encontró Microsoft Edge ni Brave',
          text: 'Sin uno de los dos, el radar no puede consultar Indeed. Instala Edge o define RADAR_BROWSER_PATH.', action: null });
      }
      const box = document.getElementById('statusBanner');
      if (!items.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
      box.classList.remove('hidden');
      box.innerHTML = items.map(i => `
        <div class="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 rounded-xl border text-xs ${i.level === 'error' ? 'bg-[#fef2f2] border-[#e5484d]/40 text-[#991b1b]' : 'bg-[#fffbeb] border-[#e8a765]/50 text-[#92400e]'}">
          <div class="flex items-start gap-2.5">
            <i class="fa-solid ${i.level === 'error' ? 'fa-shield-halved' : 'fa-triangle-exclamation'} mt-0.5"></i>
            <div><b>${escapeHtml(i.title)}</b><div class="opacity-90">${escapeHtml(i.text)}</div></div>
          </div>
          ${i.action ? `<button onclick="${i.action.fn}" class="px-3 py-1.5 rounded-lg bg-white border border-current/30 font-bold hover:bg-[#f1f5f9]">${i.action.label}</button>` : ''}
        </div>`).join('');
    }

    async function clearPause(portal) {
      if (!confirm('¿Levantar la pausa de seguridad de ' + portal + '? Hazlo solo si ya abriste el portal en tu navegador y funciona con normalidad.')) return;
      try {
        await fetch('/api/guard/clear?portal=' + encodeURIComponent(portal), { method: 'POST' });
        showToast('Pausa levantada', 'success');
        refreshStatusBanner();
      } catch (e) { showToast('No se pudo levantar la pausa', 'error'); }
    }

    // ---------------- Seguimiento de postulaciones ----------------
    let trackingCache = null;
    const STAGE_STYLE = {
      postulado: 'text-[#64748b]', en_proceso: 'text-[#0ea5e9]', entrevista: 'text-[#a855f7]',
      prueba_tecnica: 'text-[#e8a765]', oferta: 'text-[#16a34a]', rechazado: 'text-[#e5484d]', cerrada: 'text-[#94a3b8]'
    };

    function daysSince(iso) {
      if (!iso) return null;
      const d = new Date(iso);
      if (isNaN(d)) return null;
      return Math.floor((Date.now() - d.getTime()) / 86400000);
    }

    function renderTrackingControls(job) {
      const labels = (trackingCache && trackingCache.stage_labels) || {
        postulado: 'Postulado (sin respuesta)', en_proceso: 'Te contactaron / En proceso', entrevista: 'Entrevista',
        prueba_tecnica: 'Prueba técnica', oferta: 'Oferta', rechazado: 'Rechazado', cerrada: 'Cerrada / Sin respuesta'
      };
      const stage = job.stage || 'postulado';
      const days = daysSince(job.stage_updated_at || job.applied_at);
      const stale = stage === 'postulado' && days !== null && days >= 21;
      const opts = Object.entries(labels).map(([k, v]) => `<option value="${k}" ${k === stage ? 'selected' : ''}>${v}</option>`).join('');
      return `
        <div class="mb-3 p-2.5 rounded-lg border ${stale ? 'border-[#e5484d]/40 bg-[#e5484d]/5' : 'border-[#e2e8f0] bg-[#f8fafc]'} text-[11px]">
          <div class="flex items-center justify-between gap-2">
            <select onchange="setStage('${job.id}', this.value)" class="flex-1 py-1 px-2 rounded-md bg-white border border-[#e2e8f0] font-semibold ${STAGE_STYLE[stage] || ''}">${opts}</select>
            <span class="font-mono ${stale ? 'text-[#e5484d] font-bold' : 'text-[#64748b]'}" title="Días desde la postulación o el último cambio de etapa">${days !== null ? days + ' d' : ''}${stale ? ' ⚠' : ''}</span>
          </div>
          <textarea onchange="setNotes('${job.id}', this.value)" rows="1" placeholder="Notas (quién te contactó, fecha de entrevista...)" class="mt-2 w-full py-1 px-2 rounded-md bg-white border border-[#e2e8f0] text-[11px] resize-y">${escapeHtml(job.notes || '')}</textarea>
        </div>`;
    }

    async function postTracking(id, payload, okMsg) {
      try {
        const res = await fetch(`/api/offers/${id}/tracking`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error();
        if (okMsg) showToast(okMsg, 'success');
        const job = (allOffers || []).find(o => o.id === id);
        if (job) Object.assign(job, payload, payload.stage ? { stage_updated_at: new Date().toISOString() } : {});
        loadTracking();
      } catch (e) {
        showToast('No se pudo guardar el seguimiento', 'error');
      }
    }
    function setStage(id, stage) { postTracking(id, { stage }, 'Etapa actualizada'); }
    function setNotes(id, notes) { postTracking(id, { notes }, 'Nota guardada'); }

    async function loadTracking() {
      const panel = document.getElementById('trackingPanel');
      try {
        const [tr, daily] = await Promise.all([
          fetch('/api/tracking').then(r => r.json()),
          fetch('/api/daily/status').then(r => r.json()).catch(() => null)
        ]);
        trackingCache = tr;
        const chips = Object.entries(tr.by_stage).map(([k, n]) =>
          `<div class="px-3 py-2 rounded-lg bg-white border border-[#e2e8f0]"><div class="font-mono text-base font-bold ${STAGE_STYLE[k] || ''}">${n}</div><div class="text-[10px] text-[#64748b]">${escapeHtml(tr.stage_labels[k])}</div></div>`
        ).join('');
        const portals = Object.entries(tr.by_portal).map(([p, n]) => `${escapeHtml(p)}: ${n}`).join(' · ');
        const staleList = tr.stale.slice(0, 8).map(x =>
          `<li class="flex justify-between gap-2"><a href="${x.url}" target="_blank" rel="noreferrer noopener" class="truncate hover:text-[#be123c]">${escapeHtml(x.title)} — ${escapeHtml(x.company)}</a><span class="font-mono text-[#e5484d] shrink-0">${x.days} d</span></li>`
        ).join('');
        const lastSync = daily && daily.last_full_sync_at ? new Date(daily.last_full_sync_at).toLocaleString('es-CO') : 'nunca';
        panel.innerHTML = `
          <div class="p-4 rounded-xl bg-[#ffffff] border border-[#e2e8f0] text-xs">
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
              <div class="font-bold text-[#0f172a]"><i class="fa-solid fa-route text-[#0ea5e9] mr-1.5"></i>Seguimiento de ${tr.total} postulaciones</div>
              <div class="flex items-center gap-3 text-[#64748b]">
                <span>Tasa de respuesta: <b class="text-[#0f172a]">${tr.response_rate}%</b></span>
                <span>${portals}</span>
                <button onclick="syncApplicationsFromPortal()" class="px-2.5 py-1 rounded-md bg-[#f1f5f9] hover:bg-[#e2e8f0] border border-[#e2e8f0] text-[#0f172a] font-semibold" title="Lee 'Mis postulaciones' de Computrabajo con el navegador del radar">
                  <i class="fa-solid fa-rotate text-[10px] mr-1"></i>Traer postulaciones de Computrabajo
                </button>
              </div>
            </div>
            <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2 mb-3">${chips}</div>
            ${tr.stale_count ? `<div class="p-3 rounded-lg bg-[#e5484d]/5 border border-[#e5484d]/30"><div class="font-semibold text-[#e5484d] mb-1.5">${tr.stale_count} postulaciones llevan más de ${tr.stale_days} días sin respuesta. Considera marcarlas como cerradas o hacer seguimiento.</div><ul class="space-y-1 text-[#0f172a]">${staleList}</ul></div>` : `<div class="text-[#64748b]">Ninguna postulación lleva más de ${tr.stale_days} días sin respuesta.</div>`}
            <div class="mt-3 text-[10px] text-[#94a3b8]">Sincronización diaria automática a las ${daily ? daily.time : '--'} (hora Bogotá). Última sincronización completa: ${lastSync}.</div>
          </div>`;
      } catch (e) {
        panel.innerHTML = '';
      }
    }

    async function syncApplicationsFromPortal() {
      showToast('Leyendo tus postulaciones de Computrabajo…');
      try {
        const res = await fetch('/api/browser/sync-applications', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Error');
        if (data.needs_login) { showToast(data.message, 'error'); return; }
        showToast(`Encontradas ${data.found_applications}, actualizadas ${data.synced_to_db}`, 'success');
        loadOffers();
      } catch (e) {
        showToast(e.message || 'No se pudo sincronizar', 'error');
      }
    }

    // ---------------- Navegador del radar ----------------
    async function openRadarBrowser() {
      try {
        const res = await fetch('/api/browser/open', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: 'https://co.indeed.com' })
        });
        const data = await res.json();
        showToast(res.ok ? data.message : (data.detail || 'No se pudo abrir'), res.ok ? 'success' : 'error');
      } catch (e) {
        showToast('No se pudo abrir el navegador del radar', 'error');
      }
    }

    // ---------------- Qué aprender ----------------
    async function loadSkills(scope) {
      const panel = document.getElementById('skillsPanel');
      scope = scope || 'relevant';
      panel.innerHTML = '<div class="text-xs text-[#64748b]">Analizando vacantes…</div>';
      try {
        const data = await fetch(`/api/skills-gap?scope=${scope}`).then(r => r.json());
        const max = Math.max(1, ...data.skills.map(x => x.count));
        const row = x => `
          <div class="flex items-center gap-3 py-1.5" title="Ej.: ${escapeHtml(x.examples.join(' · '))}">
            <div class="w-40 shrink-0 text-xs font-semibold text-[#0f172a] truncate">${escapeHtml(x.skill)}</div>
            <div class="flex-1 h-2.5 rounded-full bg-[#f1f5f9] overflow-hidden">
              <div class="h-full rounded-full ${x.have ? (x.level === 'básico' ? 'bg-[#e8a765]' : 'bg-[#0ea5e9]') : 'bg-[#e11d48]'}" style="width:${(100 * x.count / max).toFixed(1)}%"></div>
            </div>
            <div class="w-24 shrink-0 text-right font-mono text-[11px] text-[#64748b]">${x.count} · ${x.pct}%</div>
            <div class="w-36 shrink-0 whitespace-nowrap text-[10px] font-bold uppercase ${x.have ? (x.level === 'básico' ? 'text-[#e8a765]' : 'text-[#0ea5e9]') : 'text-[#e11d48]'}">${x.have ? 'Tengo · ' + x.level : 'Me falta'}</div>
          </div>`;
        const gaps = data.top_gaps.map(x => `<li><b>${escapeHtml(x.skill)}</b> — ${x.pct}% de las vacantes</li>`).join('');
        const reinforce = data.reinforce.map(x => `<li><b>${escapeHtml(x.skill)}</b> — ${x.pct}% (hoy: básico)</li>`).join('');
        const btn = (v, label) => `<button onclick="loadSkills('${v}')" class="px-2.5 py-1 rounded-md text-[11px] font-semibold border ${scope === v ? 'bg-[#e11d48] text-white border-[#e11d48]' : 'bg-white text-[#0f172a] border-[#e2e8f0]'}">${label}</button>`;
        panel.innerHTML = `
          <div class="flex flex-wrap items-center justify-between gap-2 mb-4">
            <div class="text-xs text-[#64748b]">${data.vacancies_analyzed} vacantes analizadas</div>
            <div class="flex gap-1.5">${btn('relevant', 'Afines')}${btn('applied', 'Postuladas')}${btn('all', 'Todas')}</div>
          </div>
          <div class="grid md:grid-cols-2 gap-4 mb-5">
            <div class="p-4 rounded-xl bg-white border border-[#e11d48]/30"><div class="font-bold text-xs text-[#e11d48] mb-2"><i class="fa-solid fa-bullseye mr-1"></i>Lo que más te falta</div><ol class="list-decimal pl-5 space-y-1 text-xs text-[#0f172a]">${gaps || '<li>Nada relevante</li>'}</ol></div>
            <div class="p-4 rounded-xl bg-white border border-[#e8a765]/40"><div class="font-bold text-xs text-[#b7791f] mb-2"><i class="fa-solid fa-arrow-trend-up mr-1"></i>Tienes, pero en nivel básico</div><ol class="list-decimal pl-5 space-y-1 text-xs text-[#0f172a]">${reinforce || '<li>Nada</li>'}</ol></div>
          </div>
          <div class="p-4 rounded-xl bg-white border border-[#e2e8f0]">${data.skills.map(row).join('')}</div>`;
      } catch (e) {
        panel.innerHTML = '<div class="text-xs text-[#e5484d]">No se pudo cargar el análisis.</div>';
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    let qaEntriesCache = [];

    async function openQAModal() {
      document.getElementById('qaModal').classList.remove('hidden');
      if (qaEntriesCache.length === 0) {
        try {
          const res = await fetch('/api/qa/entries');
          qaEntriesCache = await res.json();
        } catch (e) {
          console.error(e);
        }
      }
      renderQACards(qaEntriesCache);
    }

    function closeQAModal() {
      document.getElementById('qaModal').classList.add('hidden');
    }

    function renderQACards(entries, highlightId = null) {
      const container = document.getElementById('qaCardsContainer');
      if (!entries || entries.length === 0) {
        container.innerHTML = '<p class="text-xs text-[#64748b] text-center py-4">No se encontraron respuestas para esa consulta.</p>';
        return;
      }
      container.innerHTML = entries.map(e => {
        const isHighlight = highlightId && e.id === highlightId;
        const borderCls = isHighlight ? 'border-[#e11d48] bg-[#ffe4e6]/30 shadow-[0_0_15px_-3px_rgba(225,29,72,0.3)]' : 'border-[#e2e8f0] bg-[#f1f5f9]';
        return `
          <div class="p-4 rounded-xl border ${borderCls} transition-all">
            <div class="flex items-center justify-between gap-2 mb-2">
              <span class="text-xs font-bold text-[#0f172a] flex items-center gap-2">
                ${isHighlight ? '<i class="fa-solid fa-star text-[#be123c] text-[10px]"></i>' : ''}
                ${escapeHtml(e.category)}
              </span>
              <div class="flex items-center gap-2">
                <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-[#f8fafc] border border-[#e2e8f0] text-[#64748b]">
                  ${e.length} / 500 chars
                </span>
                <button onclick="copyQAText(this, '${e.id}')" class="px-2.5 py-1 rounded-lg bg-[#e2e8f0] hover:bg-[#e11d48] text-white text-[11px] font-bold flex items-center gap-1.5 transition active:scale-95">
                  <i class="fa-regular fa-copy text-[10px]"></i>
                  <span>Copiar</span>
                </button>
              </div>
            </div>
            <p class="text-xs text-[#64748b] leading-relaxed whitespace-pre-line">${escapeHtml(e.answer)}</p>
          </div>
        `;
      }).join('');
    }

    async function handleQASearch(query) {
      const q = query.trim();
      if (!q) {
        renderQACards(qaEntriesCache);
        return;
      }
      try {
        const res = await fetch('/api/qa/match', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: q })
        });
        const match = await res.json();
        if (match.matched) {
          const sorted = [...qaEntriesCache].sort((a, b) => (a.id === match.id ? -1 : 1));
          renderQACards(sorted, match.id);
        } else {
          renderQACards(qaEntriesCache);
        }
      } catch (err) {
        console.error(err);
      }
    }

    function copyQAText(btn, id) {
      const entry = (qaEntriesCache || []).find(e => e.id === id);
      const textToCopy = entry ? entry.answer : '';
      if (!textToCopy) return;

      navigator.clipboard.writeText(textToCopy).then(() => {
        const originalHtml = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-check text-[#38bdf8] text-[10px]"></i><span class="text-[#38bdf8]">¡Copiado!</span>';
        btn.classList.add('bg-[#0ea5e9]/20', 'border-[#0ea5e9]/40');
        showToast('Respuesta copiada al portapapeles', 'success');
        setTimeout(() => {
          btn.innerHTML = originalHtml;
          btn.classList.remove('bg-[#0ea5e9]/20', 'border-[#0ea5e9]/40');
        }, 1800);
      });
    }

    loadOffers();
    fetch('/api/sync/status').then(r => r.json()).then(st => {
      if (st && st.is_running) startSyncPolling();
    }).catch(() => {});
    // Arrancar el alert polling y pedir permiso de notificaciones
    requestNotificationPermission();
    startAlertPolling();
    refreshStatusBanner();
    setInterval(refreshStatusBanner, 30000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    # Arranque sin .bat:  python server.py
    # (sin --reload: así uvicorn usa el bucle correcto en Windows). Abre el dashboard solo.
    import os
    import threading
    import webbrowser
    import uvicorn

    host = os.environ.get("RADAR_HOST", "127.0.0.1")
    port = int(os.environ.get("RADAR_PORT", "8000"))
    if os.environ.get("RADAR_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host=host, port=port)
