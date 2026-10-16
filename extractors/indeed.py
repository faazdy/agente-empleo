"""
Extractor de Indeed Colombia (co.indeed.com) a través del navegador REAL (Brave vía CDP).

Por qué no HTTPX: Indeed está detrás de Cloudflare y bloquea clientes HTTP sin huella
TLS/JS de navegador. Usando Brave con la sesión real las peticiones son indistinguibles
de una persona navegando.

La app abre sola Microsoft Edge (o Brave) con un perfil dedicado, fuera de pantalla,
y lo cierra al terminar (core/browser.py). No hace falta abrir nada a mano. Verificado el 2026-09-25: Indeed carga sin bloqueo en un navegador real y
los resultados están en window.mosaic (15 por página).

Reglas anti-baneo:
- Una sola pestaña, peticiones secuenciales, pausas de 6 a 10 s entre páginas.
- Se pide el detalle SOLO de vacantes prometedoras (prefiltro de título) y con tope por sesión.
- Captcha / "Just a moment" / "Security check" / HTTP 403-429 → enfriamiento global del portal.
"""
import asyncio
import random
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from core.guard import VerificationRequired, WafBlocked, guard
from core.isolated import run_isolated
from core.models import JobOffer
from extractors.base import BaseExtractor
from services.filters import is_fresh, is_promising_title

BASE_URL = "https://co.indeed.com"

# Pantallas de verificación de Cloudflare: un navegador real normalmente las pasa solo en unos segundos.
CHALLENGE_TITLES = (
    "un momento", "just a moment", "verificando", "verifying", "checking your browser",
    "security check", "verifica que eres humano", "verify you are human",
    "additional verification required", "attention required", "captcha",
)
# Bloqueo real: no se resuelve esperando.
HARD_BLOCK_TITLES = ("access denied", "acceso denegado", "403 forbidden", "error 1020", "you have been blocked")

# Pantallas de login: si Indeed redirige a login, pedir a la persona que inicie sesión
LOGIN_TITLES = ("iniciar sesión", "iniciar sesion", "cuentas indeed", "sign in", "log in")

AUTO_WAIT_ROUNDS = 12          # x 2.5 s: espera a que la verificación se resuelva sola
HUMAN_WAIT_ROUNDS = 60         # x 3 s: tiempo para que la persona la resuelva en la ventana


def _is_challenge(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in CHALLENGE_TITLES)


def _is_login(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in LOGIN_TITLES)


def _is_hard_block(status: int, title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in HARD_BLOCK_TITLES) or (status in (403, 429) and not _is_challenge(t) and not _is_login(t))


# Lee las tarjetas de resultados. Primero intenta el JSON embebido (mosaic), luego el DOM.
_RESULTS_JS = r"""
() => {
  const out = [];
  try {
    const m = window.mosaic && window.mosaic.providerData && window.mosaic.providerData["mosaic-provider-jobcards"];
    const res = m && m.metaData && m.metaData.mosaicProviderJobCardsModel && m.metaData.mosaicProviderJobCardsModel.results;
    if (res && res.length) {
      for (const r of res) {
        out.push({
          jk: r.jobkey, title: r.displayTitle || r.title, company: r.company,
          location: r.formattedLocation, remote: !!r.remoteLocation,
          salary: (r.salarySnippet && r.salarySnippet.text) || (r.extractedSalary ? JSON.stringify(r.extractedSalary) : ""),
          snippet: (r.snippet || "").replace(/<[^>]+>/g, " "),
          relative: r.formattedRelativeTime || "", pubDate: r.pubDate || null,
        });
      }
      return out;
    }
  } catch (e) {}
  document.querySelectorAll("a[data-jk]").forEach(a => {
    const card = a.closest("td.resultContent, div.job_seen_beacon, li") || a.parentElement;
    const q = s => { const el = card && card.querySelector(s); return el ? el.innerText.trim() : ""; };
    out.push({
      jk: a.getAttribute("data-jk"), title: a.innerText.trim(),
      company: q('[data-testid="company-name"]'), location: q('[data-testid="text-location"]'),
      remote: false, salary: q('[data-testid="attribute_snippet_testid"]'),
      snippet: q('[data-testid="jobsnippet_footer"], .job-snippet'),
      relative: q('[data-testid="myJobsStateDate"], span.date'), pubDate: null,
    });
  });
  return out;
}
"""


def _normalize_posted(pub_ms: Optional[int], relative: str) -> str:
    """
    Convierte la antigüedad al formato que ya entiende JobDatabase
    ('Hace N horas' / 'Ayer' / 'Hace N días').

    Prioridad al texto relativo de Indeed ('Recién publicado', 'hace 2 días'): es lo que ve la persona
    y refleja la ÚLTIMA actualización del aviso. `pubDate` es la fecha de publicación ORIGINAL
    (verificado en vivo el 2026-09-25: un aviso con pubDate del 12 de septiembre mostraba
    'hace 4 días'), y además viene truncada a la medianoche. Solo se usa si no hay texto relativo.
    """
    r = (relative or "").lower().strip()
    if r:
        if any(k in r for k in ("recién", "recien", "justo ahora", "hoy", "just posted", "today")):
            return "Hace 1 horas"
        m = re.search(r"(\d+)\s*(?:\+\s*)?(?:hora|minuto)", r)
        if m:
            return "Hace 1 horas"
        m = re.search(r"(\d+)\s*(?:\+\s*)?d[ií]a", r) or re.search(r"(\d+)\s*day", r)
        if m:
            n = int(m.group(1))
            return "Ayer" if n == 1 else f"Hace {n} días"
        if "un día" in r or "un dia" in r or "1 día" in r:
            return "Ayer"
        if "30" in r or "mes" in r:
            return "Más de 30 días"
    if pub_ms:
        age_h = (datetime.now(timezone.utc).timestamp() * 1000 - float(pub_ms)) / 3_600_000
        if age_h < 1:
            return f"Hace {max(1, int(age_h * 60))} minutos"
        if age_h < 24:
            return f"Hace {int(age_h)} horas"
        if age_h < 48:
            return "Ayer"
        return f"Hace {int(age_h // 24)} días"
    return relative or ""


def _modality(item: Dict) -> str:
    loc = (item.get("location") or "").lower()
    if "híbrido" in loc or "hibrido" in loc:
        return "Híbrido"
    if item.get("remote") or "remoto" in loc or "desde casa" in loc or "remote" in loc:
        return "Remoto"
    return "Presencial"


# Indeed cambió el marcado: #jobDescriptionText ya no existe (verificado en vivo el 2026-09-25;
# hoy es div.react-native-html-content.simple-job-description-html). Se prueban varios selectores.
DESCRIPTION_SELECTORS = (
    "#jobDescriptionText",
    "div.simple-job-description-html",
    "div.react-native-html-content",
    "[data-testid='jobsearch-JobComponent-description']",
)


class IndeedExtractor(BaseExtractor):
    portal_name = "Indeed"

    def __init__(self, controller_factory=None, max_details_per_session: int = 40):
        # controller_factory permite inyectar un controlador falso en pruebas
        self._controller_factory = controller_factory
        self.max_details_per_session = max_details_per_session
        self.stats = {}
        self.detail_attempts = 0
        self.detail_failures = 0   # descripciones que no se pudieron leer (¿cambió el marcado?)
        self.interactive = True    # si hay verificación, mostrar la ventana para que la persona la resuelva
        self.on_status = None      # callback(str) para mostrar mensajes en el dashboard
        self.fromage = 7           # días que se le piden al portal (1, 3, 7 o 14: es su tope)

    def _new_controller(self):
        if self._controller_factory:
            return self._controller_factory()
        from core.browser import BrowserController
        return BrowserController()

    @staticmethod
    def build_url(query: str, location: str, remote: bool = False, start: int = 0, fromage: int = 7) -> str:
        """
        URL de búsqueda. `remote=True` agrega la palabra "remoto" a la consulta.
        Verificado en vivo (2026-09-25): el filtro sc=0kf:attr(DSQF7) y l=remoto NO filtran en
        co.indeed.com (devuelven vacantes presenciales de Bogotá); en cambio "<consulta> remoto"
        con l=Colombia sí trae vacantes reales con ubicación "Desde casa".
        """
        q = f"{query} remoto" if remote and "remot" not in query.lower() else query
        # fromage: ventana que filtra el propio portal (su tope es 14 días); el radar la recorta después.
        params = {"q": q, "l": location, "fromage": str(fromage), "sort": "date"}
        if start:
            params["start"] = str(start)
        return f"{BASE_URL}/jobs?{urllib.parse.urlencode(params)}"

    def _say(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    async def _window(self, page, visible: bool) -> None:
        """Trae la ventana del navegador a la pantalla (o la vuelve a esconder) por CDP."""
        try:
            client = await page.context.new_cdp_session(page)
            win = await client.send("Browser.getWindowForTarget")
            bounds = ({"left": 80, "top": 60, "width": 1200, "height": 850, "windowState": "normal"} if visible
                      else {"left": -32000, "top": -32000, "width": 1366, "height": 900, "windowState": "normal"})
            await client.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": bounds})
            if visible:
                await page.bring_to_front()
        except Exception:
            pass  # sin ventana visible el flujo sigue funcionando; solo se pierde la ayuda visual

    async def _resolve_challenge(self, page) -> None:
        """Espera a que la verificación se resuelva sola; si no, pide ayuda a la persona."""
        for _ in range(AUTO_WAIT_ROUNDS):
            await asyncio.sleep(2.5)
            if not _is_challenge(await page.title()):
                return
        if not self.interactive:
            guard.mark_verification(self.portal_name, "verificación pendiente (escaneo sin supervisión)")
            raise VerificationRequired(self.portal_name, "sin supervisión")
        self._say("Indeed pide una verificación: resuélvela en la ventana que se abrió (tienes 3 minutos).")
        await self._window(page, True)
        for _ in range(HUMAN_WAIT_ROUNDS):
            await asyncio.sleep(3.0)
            if not _is_challenge(await page.title()):
                await self._window(page, False)
                self._say("")
                return
        await self._window(page, False)
        guard.mark_verification(self.portal_name, "verificación sin resolver")
        raise VerificationRequired(self.portal_name, "sin resolver")

    async def _resolve_login(self, page) -> None:
        """Si Indeed redirige a login, muestra la ventana para que la persona inicie sesión."""
        if not self.interactive:
            return
        self._say("Indeed pide iniciar sesión: inicia sesión en la ventana del radar (esperando hasta 2 min)...")
        await self._window(page, True)
        for _ in range(40):  # hasta 2 minutos
            await asyncio.sleep(3.0)
            if not _is_login(await page.title()):
                await self._window(page, False)
                self._say("")
                return
        await self._window(page, False)

    async def _goto(self, page, url: str) -> None:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(random.uniform(2.0, 3.5))  # deja cargar el JS como una persona
        status = resp.status if resp else 200
        title = (await page.title()) or ""
        if _is_challenge(title):
            await self._resolve_challenge(page)   # espera / pide ayuda; lanza VerificationRequired si no se resuelve
            await asyncio.sleep(random.uniform(1.5, 2.5))
            title = (await page.title()) or ""
            status = 200
        if _is_login(title):
            await self._resolve_login(page)
            await asyncio.sleep(random.uniform(1.5, 2.5))
            title = (await page.title()) or ""
            status = 200
        if _is_hard_block(status, title):
            guard.trip(self.portal_name, f"bloqueo real (HTTP {status}, título '{title[:40]}')")
            raise WafBlocked(self.portal_name, f"HTTP {status} / bloqueo")
        guard.resolve_verification(self.portal_name)

    async def _detail(self, page, jk: str) -> str:
        await asyncio.sleep(random.uniform(6.0, 10.0))
        await self._goto(page, f"{BASE_URL}/viewjob?jk={jk}")
        for sel in DESCRIPTION_SELECTORS:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        return ""

    async def search_jobs(self, query: str, location: str = "Bogotá, D.C.", max_pages: int = 1,
                          existing_ids: Optional[Set[str]] = None, remote: bool = False) -> List[JobOffer]:
        return await self.search_many([{"query": query, "location": location,
                                        "max_pages": max_pages, "remote": remote}], existing_ids)

    async def search_many(self, targets: List[Dict], existing_ids: Optional[Set[str]] = None,
                          on_progress=None, interactive: bool = True, on_status=None) -> List[JobOffer]:
        """
        Recorre varias consultas en UNA sola pestaña/sesión del navegador.
        Se ejecuta en un bucle de eventos propio: Playwright no arranca en Windows si el servidor
        usa SelectorEventLoop (uvicorn --reload).
        """
        guard.check(self.portal_name)
        self.interactive = interactive
        self.on_status = on_status
        return await run_isolated(lambda: self._search_many(targets, existing_ids, on_progress))

    async def _search_many(self, targets: List[Dict], existing_ids: Optional[Set[str]] = None,
                           on_progress=None) -> List[JobOffer]:
        guard.check(self.portal_name)
        self.detail_attempts = self.detail_failures = 0
        self.stats = {"paginas": 0, "vistas": 0, "ya_conocidas": 0, "viejas": 0, "titulo_no_afin": 0, "nuevas": 0}
        known = existing_ids if existing_ids is not None else set()
        offers: List[JobOffer] = []
        details_left = self.max_details_per_session

        controller = self._new_controller()
        await controller.connect()
        page = await controller._context.new_page()
        try:
            for idx, t in enumerate(targets, 1):
                if on_progress:
                    on_progress(idx, t)
                url = self.build_url(t["query"], t["location"], t.get("remote", False), fromage=self.fromage)
                for _ in range(t.get("max_pages", 3)):
                    await asyncio.sleep(random.uniform(6.0, 10.0))
                    try:
                        await self._goto(page, url)
                        items = []
                        for _ in range(6):
                            items = await page.evaluate(_RESULTS_JS)
                            if items:
                                break
                            await asyncio.sleep(1.0)
                        self.stats["paginas"] += 1
                        self.stats["vistas"] += len(items or [])
                        if not items:
                            print(f"[IndeedExtractor] '{t['query']}': página sin resultados (título '{(await page.title())[:60]}')")
                        next_href = await page.evaluate(
                            "() => { const a = document.querySelector('[data-testid=\"pagination-page-next\"]');"
                            " return a ? a.getAttribute('href') : null; }"
                        )
                    except WafBlocked as wb:
                        wb.partial = offers
                        raise
                    if not items:
                        break
                    for it in items:
                        jk = it.get("jk")
                        title = (it.get("title") or "").strip()
                        company = (it.get("company") or "").strip() or "Confidencial / No especificada"
                        job_url = f"{BASE_URL}/viewjob?jk={jk}"
                        sig = f"{title.lower()}::{company.lower()}"
                        if not jk or not title or jk in known or job_url in known or sig in known:
                            self.stats["ya_conocidas"] += 1
                            continue
                        posted = _normalize_posted(it.get("pubDate"), it.get("relative", ""))
                        if not is_fresh(posted):
                            self.stats["viejas"] += 1
                            continue
                        if not is_promising_title(title):
                            self.stats["titulo_no_afin"] += 1
                            continue
                        description = it.get("snippet") or ""
                        if details_left > 0:
                            try:
                                full = await self._detail(page, jk)
                            except WafBlocked as wb:
                                wb.partial = offers
                                raise
                            self.detail_attempts += 1
                            if full:
                                description = full
                            else:
                                self.detail_failures += 1
                            details_left -= 1
                        offer = JobOffer(
                            portal=self.portal_name, external_id=jk, title=title, company=company,
                            location=it.get("location") or t["location"], modality=_modality(it),
                            salary=it.get("salary") or "A convenir",
                            description=description or f"Vacante de {title} en {company}.",
                            requirements=[], url=job_url, posted_date=posted,
                        )
                        offers.append(offer)
                        self.stats["nuevas"] += 1
                        known.update({offer.id, jk, job_url, sig})
                    if not next_href:
                        break
                    url = next_href if next_href.startswith("http") else BASE_URL + next_href
        finally:
            try:
                await page.close()
            finally:
                await controller.close()
        return offers
