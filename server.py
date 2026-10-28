import asyncio
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from services import profiles as profile_store

from core.database import JobDatabase
from core.models import JobOffer
from extractors.computrabajo import ComputrabajoExtractor
from extractors.indeed import IndeedExtractor
from extractors.elempleo import ElempleoExtractor
from services.matcher import MatcherEngine
from services import filters as filters_mod
from core.guard import WafBlocked, VerificationRequired, guard, in_active_window, BOGOTA_TZ
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
elempleo_extractor = ElempleoExtractor()
# El matcher y el prefiltro de los extractores usan las reglas del perfil activo (data/profiles.json).
matcher = MatcherEngine(profile_store.apply_rules(profile_store.get_active()))


def _refresh_matcher(profile: dict) -> None:
    global matcher
    matcher = MatcherEngine(profile_store.apply_rules(profile))


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


# ---------------------------------------------------------------------------
# Perfiles de búsqueda (filtros manuales)
# ---------------------------------------------------------------------------

def reevaluate_offers() -> dict:
    """Vuelve a puntuar con el perfil activo las vacantes que no tocaste a mano
    (no cambia postuladas ni guardadas)."""
    counts = {"evaluadas": 0, "compatibles": 0, "descartadas": 0}
    for offer in db.get_offers():
        if offer.status not in ("new", "matched", "discarded"):
            continue
        score, reason, status = matcher.evaluate(offer)
        db.update_match(offer.id, score, reason, status)
        counts["evaluadas"] += 1
        counts["compatibles" if status == "matched" else "descartadas"] += 1
    return counts


@app.get("/api/profiles")
def list_profiles():
    return profile_store.list_profiles()


@app.get("/api/profiles/defaults")
def profile_defaults():
    """
    Lo que trae el radar de fábrica: valores por defecto del formulario, las consultas que usa
    cuando el perfil no tiene propias, y sus filtros en palabras (para poder verlos en la app).
    """
    return {
        "profile": profile_store.DEFAULT_PROFILE,
        "default_queries": sorted({t["query"] for t in (*SEARCH_TARGETS, *ELEMPLEO_TARGETS, *INDEED_TARGETS)}),
        "builtin_rules": profile_store.describe_builtin_rules(),
    }


@app.post("/api/profiles")
def save_profile(profile: dict):
    saved = profile_store.save_profile(profile)
    if saved["id"] == profile_store.list_profiles()["active"]:
        _refresh_matcher(saved)
    return saved


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    try:
        ok = profile_store.delete_profile(profile_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    if not ok:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    _refresh_matcher(profile_store.get_active())
    return {"success": True}


@app.post("/api/profiles/{profile_id}/activate")
def activate_profile(profile_id: str, reevaluate: bool = Query(True)):
    if sync_state.get("is_running"):
        raise HTTPException(status_code=409, detail="Hay un escaneo en curso; cambia de perfil cuando termine.")
    try:
        p = profile_store.set_active(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    _refresh_matcher(p)
    return {"success": True, "active": p, "reevaluated": reevaluate_offers() if reevaluate else None}


@app.post("/api/profiles/reevaluate")
def reevaluate_endpoint():
    _refresh_matcher(profile_store.get_active())
    return reevaluate_offers()


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
    {"query": "qa", "location": "bogota-dc", "max_pages": 2},
    {"query": "inteligencia artificial", "location": "bogota-dc", "max_pages": 2},
    {"query": "junior", "location": "bogota-dc", "max_pages": 3},
    {"query": "trainee", "location": "bogota-dc", "max_pages": 2},
    {"query": "javascript", "location": "bogota-dc", "max_pages": 2},
    {"query": "node", "location": "bogota-dc", "max_pages": 2},
    # 100% remoto en todo el país (postulación válida viviendo en Bogotá).
    {"query": "desarrollador", "location": "remoto", "max_pages": 2},
    {"query": "programador", "location": "remoto", "max_pages": 2},
    {"query": "analista de datos", "location": "remoto", "max_pages": 2},
    {"query": "python", "location": "remoto", "max_pages": 2},
    {"query": "react", "location": "remoto", "max_pages": 2},
    {"query": "full stack", "location": "remoto", "max_pages": 2},
    {"query": "backend", "location": "remoto", "max_pages": 2},
    {"query": "frontend", "location": "remoto", "max_pages": 2},
    {"query": "software", "location": "remoto", "max_pages": 2},
    {"query": "ingeniero de sistemas", "location": "remoto", "max_pages": 2},
    {"query": "qa", "location": "remoto", "max_pages": 2},
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

# elempleo.com (HTTP + API JSON del propio portal; sin navegador). Cada consulta trae hasta
# 50 vacantes CON descripción por petición, ordenadas por fecha, y se corta al pasar la
# ventana de ELEMPLEO_MAX_AGE_DAYS (4 días). "remote": True busca en todo el país y deja
# solo las marcadas como remotas por el portal.
ELEMPLEO_TARGETS = [
    {"query": "desarrollador",          "max_pages": 3},
    {"query": "programador",            "max_pages": 2},
    {"query": "ingeniero de sistemas",  "max_pages": 2},
    {"query": "software",               "max_pages": 2},
    {"query": "full stack",             "max_pages": 1},
    {"query": "python",                 "max_pages": 1},
    {"query": "react",                  "max_pages": 1},
    {"query": "analista de datos",      "max_pages": 2},
    {"query": "ingeniero de datos",     "max_pages": 1},
    {"query": "power bi",               "max_pages": 1},
    {"query": "junior",                 "max_pages": 2},
    {"query": "desarrollador",          "max_pages": 2, "remote": True},
    {"query": "python",                 "max_pages": 1, "remote": True},
    {"query": "analista de datos",      "max_pages": 1, "remote": True},
]
ELEMPLEO_ENABLED = True

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


PORTALS = ("computrabajo", "elempleo", "indeed")
PORTAL_LABELS = {"computrabajo": "Computrabajo", "elempleo": "elempleo", "indeed": "Indeed"}


def _portal_window(days: int, allowed) -> int:
    """
    Traduce los días que pidió la persona al filtro de fecha del portal: el valor permitido
    más pequeño que los cubra (y si pide más que el tope del portal, su tope).
    """
    return next((v for v in allowed if v >= days), allowed[-1])


# Valores que acepta cada portal en su filtro "publicado hace".
CT_WINDOWS = (1, 3, 7, 15, 30)
INDEED_WINDOWS = (1, 3, 7, 14)


async def _run_sync_worker(portals=PORTALS, interactive: bool = True, quick: bool = False,
                           days: Optional[int] = None):
    """
    Escaneo: Computrabajo, elempleo y/o Indeed (por defecto los tres).
    quick=True recorta cada consulta a la primera página (pasada corta de novedades).
    days sobreescribe, solo para este escaneo, los días hacia atrás del perfil.
    Siempre bajo el candado único.
    """
    profile = profile_store.get_active()
    if days:
        # Copia en memoria: no toca data/profiles.json, vale solo para este escaneo.
        profile["max_age_days"] = max(1, min(30, int(days)))
    _refresh_matcher(profile)  # por si el perfil se editó desde otra pestaña
    custom = profile_store.search_targets(profile)
    ct_targets = custom["computrabajo"] if custom else SEARCH_TARGETS
    ee_targets = custom["elempleo"] if custom else ELEMPLEO_TARGETS
    indeed_targets = custom["indeed"] if custom else INDEED_TARGETS
    if quick:
        ct_targets = [{**t, "max_pages": 1} for t in ct_targets]
        ee_targets = [{**t, "max_pages": 1} for t in ee_targets]
        indeed_targets = [{**t, "max_pages": 1} for t in indeed_targets]
    allowed = profile.get("portals") or {}
    do_ct = "computrabajo" in portals and allowed.get("computrabajo", True) and bool(ct_targets)
    do_ee = ELEMPLEO_ENABLED and "elempleo" in portals and allowed.get("elempleo", True) and bool(ee_targets)
    do_indeed = INDEED_ENABLED and "indeed" in portals and allowed.get("indeed", True) and bool(indeed_targets)
    ventana = profile.get("max_age_days") or filters_mod.MAX_AGE_DAYS
    elempleo_extractor.max_age_days = ventana
    indeed_extractor.fromage = _portal_window(ventana, INDEED_WINDOWS)
    ct_pubdate = _portal_window(ventana, CT_WINDOWS)
    global sync_state
    async with guard.lock:
        sync_state["is_running"] = True
        sync_state["new_offers"] = 0
        sync_state["total_scanned"] = 0
        sync_state["total_queries"] = ((len(ct_targets) if do_ct else 0) + (len(ee_targets) if do_ee else 0)
                                       + (len(indeed_targets) if do_indeed else 0))
        enabled = {"computrabajo": do_ct, "elempleo": do_ee, "indeed": do_indeed}
        sync_state["portals"] = [p for p in PORTALS if enabled[p]]
        sync_state["last_error"] = None
        sync_state["portal_errors"] = {}
        sync_state["note"] = ""

        existing_ids = db.get_existing_ids()
        new_count = 0
        extractor.new_session()  # una sola huella de navegador para todo el escaneo
        report = {"started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "portales": {}}
        sync_state["profile"] = profile.get("name")
        sync_state["max_age_days"] = ventana
        print(f"[SyncWorker] Inicio de escaneo ({profile.get('name')}): {', '.join(sync_state['portals'])}")

        try:
            # --- Fase 1: Computrabajo --------------------------------------
            try:
                for idx, target in enumerate(ct_targets if do_ct else [], 1):
                    sync_state["current_index"] = idx
                    sync_state["current_query"] = f"[Computrabajo] {target['query']} ({target['location']})"
                    if idx > 1:
                        await asyncio.sleep(random.uniform(*INTER_QUERY_PAUSE))
                    offers = await extractor.search_jobs(
                        query=target["query"],
                        location=target["location"],
                        max_pages=target.get("max_pages", 1),
                        existing_ids=existing_ids,
                        pubdate=ct_pubdate,  # ventana que filtra el portal; el radar la recorta después
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

            # --- Fase 2: elempleo (HTTP + API JSON, sin navegador) ----------
            if do_ee:
                ee_base = len(ct_targets) if do_ct else 0
                before_ee = new_count
                elempleo_extractor.new_session()

                def _ee_progress(i, t):
                    sync_state["current_index"] = ee_base + i
                    sync_state["current_query"] = f"[elempleo] {t['query']} ({'remoto' if t.get('remote') else 'Bogotá'})"

                try:
                    offers = await elempleo_extractor.search_many(
                        ee_targets, existing_ids, on_progress=_ee_progress, pause=INTER_QUERY_PAUSE)
                    sync_state["total_scanned"] += len(offers)
                    new_count += _store_offers(offers, existing_ids)
                except WafBlocked as wb:
                    new_count += _store_offers(wb.partial, existing_ids)
                    sync_state["portal_errors"]["elempleo"] = str(wb)
                    print(f"[SyncWorker] {wb}")
                except Exception as e:
                    traceback.print_exc()
                    sync_state["portal_errors"]["elempleo"] = f"Error en elempleo: {describe_error(e)}"
                if elempleo_extractor.used_fallback and "elempleo" not in sync_state["portal_errors"]:
                    sync_state["note"] = ("elempleo: la API del portal no respondió; se leyó solo la primera página "
                                          "de cada búsqueda (sin descripción completa).")
                sync_state["new_offers"] = new_count
                report["portales"]["elempleo"] = {
                    **elempleo_extractor.stats, "compatibles": new_count - before_ee, **_last_store,
                    "error": sync_state["portal_errors"].get("elempleo"),
                }
                print(f"[SyncWorker] elempleo: {report['portales']['elempleo']}")
                _last_store.update(guardadas=0, descartadas_filtro=0)
            ct_matched = new_count

            # --- Fase 3: Indeed (navegador real) ---------------------------
            if do_indeed:
                base = (len(ct_targets) if do_ct else 0) + (len(ee_targets) if do_ee else 0)

                def _progress(i, t):
                    sync_state["current_index"] = base + i
                    sync_state["current_query"] = f"[Indeed] {t['query']} ({t['location']}{', remoto' if t.get('remote') else ''})"

                try:
                    offers = await indeed_extractor.search_many(
                        indeed_targets, existing_ids, on_progress=_progress, interactive=interactive,
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
async def sync_offers(
    portal: str = Query("all", description="all | computrabajo | elempleo | indeed"),
    quick: bool = Query(False, description="Pasada corta: solo la primera página de cada consulta, sin Indeed"),
    days: Optional[int] = Query(None, ge=1, le=30, description="Días hacia atrás solo para este escaneo (1 a 30)"),
):
    portal = (portal or "all").lower()
    if portal not in ("all", *PORTALS):
        raise HTTPException(status_code=400, detail="portal debe ser all, computrabajo, elempleo o indeed")
    selected = PORTALS if portal == "all" else (portal,)
    if quick:
        # Indeed abre el navegador real: queda fuera de la pasada corta.
        selected = tuple(p for p in selected if p != "indeed")
        if not selected:
            raise HTTPException(status_code=400, detail="La pasada corta no incluye Indeed porque abre el navegador.")
    if sync_state.get("is_running") or guard.lock.locked():
        return {"success": True, "message": "Ya hay un escaneo en curso. Se evita correr dos a la vez.", "running": True}

    asyncio.create_task(_run_sync_worker(selected, quick=quick, days=days))
    names = ", ".join(PORTAL_LABELS.get(p, p) for p in selected)
    ventana = days or profile_store.get_active().get("max_age_days") or filters_mod.MAX_AGE_DAYS
    return {"success": True,
            "message": f"{'Pasada corta' if quick else 'Escaneo'} de {names} en marcha, "
                       f"buscando {ventana} día{'s' if ventana != 1 else ''} hacia atrás",
            "running": True}


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
def clear_guard(portal: str = Query(..., description="computrabajo | elempleo | indeed")):
    """Levanta a mano una pausa de seguridad (úsalo solo si estás seguro de que el portal ya funciona en tu navegador)."""
    if portal.lower() not in PORTALS:
        raise HTTPException(status_code=400, detail="portal debe ser computrabajo, elempleo o indeed")
    guard.clear(portal)
    return {"success": True}


@app.get("/api/guard/status")
def get_guard_status():
    """Estado anti-baneo: escaneo en curso, enfriamientos por portal y horario activo."""
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
    if (local.hour, local.minute) < DAILY_SYNC_TIME or not in_active_window(now):
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


@app.on_event("startup")
async def start_background_jobs():
    """Única tarea de fondo: la sincronización diaria completa (07:30 hora Bogotá)."""
    asyncio.create_task(_daily_loop())
    print(f"[DailySync] Sincronización diaria activada ({daily_state['time']} hora Bogotá).")


STATIC_DIR = _Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def get_dashboard_html():
    """Dashboard: HTML + Bootstrap simple en static/ (sin compilar nada)."""
    # no-cache: así el navegador (y Cloudflare) siempre piden la versión nueva de styles/app.
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})



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
