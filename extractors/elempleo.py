"""
Extractor de elempleo.com (Colombia).

Cómo funciona (verificado en vivo el 2026-09-29):
- La página de resultados trae un token (`.js-search-api-token input[name=__RequestVerificationToken]`)
  y deja cookies de sesión. Con ese token + cookies, el propio portal consulta
  POST /co/api/joboffers/findbyfilter, que devuelve JSON con TODO lo necesario:
  título, empresa, ciudad, salario, modalidad, descripción completa y la antigüedad
  ("Hoy", "Ayer", "Hace 4 días", "Hace 1 semana").
- Por eso NO se pide la página de detalle de cada vacante: una sola petición trae hasta
  50 vacantes con descripción. Mucho más liviano (y menos riesgo de bloqueo) que Computrabajo.
- Se ordena por fecha (`PublishDate_Desc`, sortBy vacío) y se pagina hasta pasar la
  ventana de frescura (ELEMPLEO_MAX_AGE_DAYS, 4 días por defecto).
- Si la API deja de responder (cambio del portal), se usa como respaldo el HTML de la
  página de resultados (20 vacantes, sin descripción completa).

Reglas anti-baneo (mismas que el resto del radar):
- Una sola huella de navegador por sesión, cliente HTTP único con cookies.
- Pausas de 3-5 s entre páginas y 6-12 s entre consultas (las pone el servidor).
- 403/429 → corte global del portal y enfriamiento persistente (core/guard.py).
- 401 → el token venció: se renueva una vez; si sigue fallando se pasa al respaldo HTML.
"""
import asyncio
import html as _html
import json
import random
import re
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

from core.guard import WafBlocked, guard
from core.models import JobOffer
from extractors.base import BaseExtractor
from extractors.computrabajo import BROWSER_PROFILES
from services.filters import ELEMPLEO_MAX_AGE_DAYS, is_promising_title, posted_age_days

BASE_URL = "https://www.elempleo.com"
API_URL = f"{BASE_URL}/co/api/joboffers/findbyfilter"
BOGOTA_CITY_ID = 34  # id de Bogotá en elempleo (data-cities-val del portal)
PAGE_SIZE = 50       # el portal admite 20, 50 o 100; 50 es un buen equilibrio


def _slug(text: str) -> str:
    """'Analista de Datos' -> 'analista-de-datos' (formato de URL de elempleo)."""
    t = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _clean_description(raw: Optional[str]) -> str:
    """La API trae la descripción con <br/> y \\r; se deja en texto plano legible."""
    if not raw:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t).replace("\r", "")
    # Emojis que el portal ya entrega rotos como '?'
    t = re.sub(r"(?m)^\s*\?+\s*", "", t)
    t = re.sub(r"\?{2,}", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _nice_title(title: str) -> str:
    """elempleo entrega los títulos en minúscula ('Desarrollador ia'); solo se capitaliza la primera letra."""
    title = (title or "").strip()
    return title[:1].upper() + title[1:] if title else title


def _clean_url(url: str) -> str:
    """Quita '?trabajo=<consulta>' para que la misma vacante tenga la misma URL en todas las búsquedas."""
    return (url or "").split("?")[0].split("#")[0]


def _modality(item: Dict) -> str:
    txt = (item.get("idworkmodalitytxt") or "").strip()
    if txt:
        low = txt.lower()
        if "remot" in low or "teletrabajo" in low:
            return "Remoto"
        if "brido" in low:
            return "Híbrido"
        return "Presencial"
    if item.get("isRemote"):
        return "Remoto"
    if item.get("isHybrid"):
        return "Híbrido"
    return "Presencial"


class ElempleoExtractor(BaseExtractor):
    portal_name = "Elempleo"
    base_url = BASE_URL

    def __init__(self, max_age_days: int = ELEMPLEO_MAX_AGE_DAYS):
        self.max_age_days = max_age_days
        self.new_session()

    def new_session(self) -> None:
        self._profile = random.choice(BROWSER_PROFILES)
        self._token: Optional[str] = None
        self.used_fallback = False
        self.stats = {"paginas": 0, "vistas": 0, "ya_conocidas": 0, "viejas": 0, "titulo_no_afin": 0,
                      "no_remotas": 0, "nuevas": 0}

    # ------------------------------------------------------------------ HTTP
    def _headers(self, api: bool = False) -> dict:
        h = {
            "Accept-Language": "es-CO,es-419;q=0.9,es;q=0.8,en;q=0.7",
            "Referer": f"{BASE_URL}/co/ofertas-empleo/",
        }
        if api:
            h.update({
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
            })
            if self._token:
                h["__RequestVerificationToken"] = self._token
        else:
            h.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": "1",
            })
        h.update(self._profile)
        return h

    @staticmethod
    def search_page_url(query: str, bogota: bool = True) -> str:
        path = f"/co/ofertas-empleo/bogota/trabajo-{_slug(query)}" if bogota else f"/co/ofertas-empleo/trabajo-{_slug(query)}"
        return BASE_URL + path

    async def _get_page(self, client: httpx.AsyncClient, url: str) -> str:
        res = await client.get(url, headers=self._headers())
        if res.status_code in (403, 429):
            guard.trip(self.portal_name, f"HTTP {res.status_code} en búsqueda")
            raise WafBlocked(self.portal_name, f"HTTP {res.status_code}")
        if res.status_code != 200:
            raise RuntimeError(f"elempleo respondió HTTP {res.status_code}")
        return res.text

    @staticmethod
    def extract_token(page_html: str) -> Optional[str]:
        soup = BeautifulSoup(page_html, "html.parser")
        inp = soup.select_one('.js-search-api-token input[name="__RequestVerificationToken"]')
        if inp and inp.get("value"):
            return inp["value"].strip()
        m = re.search(r'js-search-api-token[^>]*>\s*<input[^>]*value="([^"]+)"', page_html)
        return m.group(1) if m else None

    async def _refresh_token(self, client: httpx.AsyncClient, query: str = "desarrollador") -> str:
        page = await self._get_page(client, self.search_page_url(query))
        self._token = self.extract_token(page)
        return page

    @staticmethod
    def build_body(query: str, page: int, bogota: bool) -> dict:
        return {
            "jobOfferId": 0, "query": query, "queryOptional": None, "salaryInfo": [],
            "contractType": 0, "workModality": 0,
            "cities": [BOGOTA_CITY_ID] if bogota else [],
            "sector": 0, "industry": 0,
            "publishDate": 0,  # sin filtro: se ordena por fecha y se corta por antigüedad aquí
            "positionLevel": 0, "area": 0, "countryId": 0, "departmentId": 0, "companyId": 0,
            "jobOfferCode": None, "pageIndex": page, "pageSize": PAGE_SIZE,
            "sortExpression": "PublishDate_Desc", "sortBy": "",
            "skills": None, "requiredexperience": 0,
        }

    async def _post_search(self, client: httpx.AsyncClient, query: str, page: int, bogota: bool) -> Optional[dict]:
        """Devuelve el JSON de la API o None si el token no sirve (tras un reintento)."""
        body = json.dumps(self.build_body(query, page, bogota))
        for attempt in range(2):
            if not self._token:
                await self._refresh_token(client, query)
                if not self._token:
                    return None
            res = await client.post(API_URL, content=body, headers=self._headers(api=True))
            if res.status_code in (403, 429):
                guard.trip(self.portal_name, f"HTTP {res.status_code} en API")
                raise WafBlocked(self.portal_name, f"HTTP {res.status_code}")
            if res.status_code == 401:
                self._token = None  # venció: se renueva y se intenta una vez más
                await asyncio.sleep(random.uniform(2.0, 3.5))
                continue
            if res.status_code != 200:
                return None
            try:
                return res.json()
            except ValueError:
                return None
        return None

    # --------------------------------------------------------------- parsing
    def offer_from_api(self, item: Dict, query: str) -> JobOffer:
        oid = str(item.get("id") or "")
        company = (item.get("companyName") or "").strip() or "Confidencial / No especificada"
        keywords = [k.get("keyWord") for k in (item.get("keyWords") or []) if k.get("keyWord")]
        structures = item.get("positionStructures")
        reqs = keywords + ([f"Cargo equivalente: {structures}"] if structures else [])
        extra = []
        if item.get("contracttypetxt"):
            extra.append(f"Contrato: {item['contracttypetxt']}")
        desc = _clean_description(item.get("description"))
        if extra:
            desc = (desc + "\n\n" + " · ".join(extra)).strip()
        city = (item.get("city") or "").strip()
        modality = _modality(item)
        return JobOffer(
            portal=self.portal_name,
            external_id=f"elempleo-{oid}" if oid else None,
            title=_nice_title(item.get("title") or ""),
            company=company,
            location=f"{city} (remoto)" if modality == "Remoto" and city else (city or "Colombia"),
            modality=modality,
            salary=(item.get("salaryInfo") or "").strip() or "A convenir",
            description=desc or f"Vacante de {item.get('title')} en {company}.",
            requirements=reqs,
            url=_clean_url(item.get("jobOfferUrl") or f"{BASE_URL}/co/ofertas-trabajo/{_slug(item.get('title') or '')}-{oid}"),
            posted_date=(item.get("publishDateInfo") or "").strip() or None,
        )

    def offers_from_html(self, page_html: str) -> List[Tuple[JobOffer, Optional[float]]]:
        """Respaldo: tarjetas del HTML de resultados (sin descripción completa)."""
        soup = BeautifulSoup(page_html, "html.parser")
        out = []
        for card in soup.select(".result-item"):
            area = card.select_one("[data-ga4-offerdata]")
            if not area:
                continue
            try:
                data = json.loads(area["data-ga4-offerdata"])
            except (ValueError, KeyError):
                continue
            date_tag = card.select_one(".js-offer-date")
            posted = date_tag.get_text(" ", strip=True) if date_tag else None
            mod_tag = card.select_one(".js-work-modality")
            mod_txt = (mod_tag.get_text(strip=True) if mod_tag else "").lstrip("- ").strip()
            item = {
                "id": data.get("id"), "title": data.get("title"), "companyName": data.get("company"),
                "city": data.get("location"), "salaryInfo": data.get("salary"),
                "jobOfferUrl": BASE_URL + (area.get("data-url") or ""),
                "publishDateInfo": posted, "idworkmodalitytxt": mod_txt or None,
                "isRemote": (data.get("location") or "").lower() == "remoto",
                "positionStructures": data.get("equivalentPositions"),
                "keyWords": [{"keyWord": t.strip()} for t in (data.get("tags") or "").split(",") if t.strip()],
            }
            out.append((self.offer_from_api(item, ""), posted_age_days(posted)))
        return out

    # ------------------------------------------------------------ filtering
    def _accept(self, offer: JobOffer, age: Optional[float], known: Set[str], remote_only: bool) -> bool:
        self.stats["vistas"] += 1
        if age is not None and age > self.max_age_days:
            self.stats["viejas"] += 1
            return False
        sig = f"{offer.title.strip().lower()}::{offer.company.strip().lower()}"
        if offer.external_id in known or offer.url in known or offer.id in known or sig in known:
            self.stats["ya_conocidas"] += 1
            return False
        if remote_only and offer.modality != "Remoto":
            self.stats["no_remotas"] += 1
            return False
        if not is_promising_title(offer.title):
            self.stats["titulo_no_afin"] += 1
            return False
        known.update({offer.id, offer.url, sig})
        if offer.external_id:
            known.add(offer.external_id)
        self.stats["nuevas"] += 1
        return True

    # --------------------------------------------------------------- search
    async def _search_one(self, client: httpx.AsyncClient, target: Dict, known: Set[str]) -> List[JobOffer]:
        query = target["query"]
        remote_only = bool(target.get("remote"))
        bogota = not remote_only
        offers: List[JobOffer] = []
        for page in range(1, target.get("max_pages", 3) + 1):
            if page > 1:
                await asyncio.sleep(random.uniform(3.0, 5.0))
            data = await self._post_search(client, query, page, bogota)
            if data is None:
                # Respaldo HTML: solo primera página (orden del portal, 20 vacantes)
                self.used_fallback = True
                if page == 1:
                    page_html = await self._get_page(client, self.search_page_url(query, bogota))
                    self.stats["paginas"] += 1
                    for offer, age in self.offers_from_html(page_html):
                        if self._accept(offer, age, known, remote_only):
                            offers.append(offer)
                break
            self.stats["paginas"] += 1
            results = data.get("results") or []
            if not results:
                break
            too_old = 0
            for item in results:
                offer = self.offer_from_api(item, query)
                age = posted_age_days(offer.posted_date)
                if age is not None and age > self.max_age_days:
                    too_old += 1
                if self._accept(offer, age, known, remote_only):
                    offers.append(offer)
            total_pages = (data.get("paginationInfo") or {}).get("totalPages") or 1
            # Ordenado por fecha: si la mayoría de la página ya es vieja, las siguientes también.
            if page >= total_pages or too_old >= max(1, len(results) // 2):
                break
        return offers

    async def search_many(self, targets: List[Dict], existing_ids: Optional[Set[str]] = None,
                          on_progress=None, pause=(6.0, 12.0)) -> List[JobOffer]:
        """Recorre varias consultas con UNA sola sesión (cookies + token)."""
        guard.check(self.portal_name)
        known = existing_ids if existing_ids is not None else set()
        offers: List[JobOffer] = []
        async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
            try:
                await self._refresh_token(client, targets[0]["query"] if targets else "desarrollador")
                for idx, t in enumerate(targets, 1):
                    if on_progress:
                        on_progress(idx, t)
                    await asyncio.sleep(random.uniform(*pause) if idx > 1 else random.uniform(2.0, 4.0))
                    offers.extend(await self._search_one(client, t, known))
            except WafBlocked as wb:
                wb.partial = offers
                raise
        return offers

    async def search_jobs(self, query: str, location: str = "bogota", max_pages: int = 3,
                          existing_ids: Optional[Set[str]] = None) -> List[JobOffer]:
        target = {"query": query, "max_pages": max_pages, "remote": location.lower() in ("remoto", "remote")}
        return await self.search_many([target], existing_ids)
