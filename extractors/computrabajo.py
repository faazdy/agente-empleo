import asyncio
import random
import re
import urllib.parse
from typing import List, Optional, Set, Tuple
import httpx
from bs4 import BeautifulSoup
from core.models import JobOffer
from extractors.base import BaseExtractor
from core.guard import WafBlocked, guard
from services.filters import is_fresh, is_promising_title

# Perfiles de navegador COHERENTES: User-Agent y Client Hints corresponden al mismo navegador.
# Se elige uno por sesión de escaneo (no por petición): cambiar de huella en cada
# petición de una misma IP es una señal clara de bot para el WAF.
BROWSER_PROFILES = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
        "Sec-Ch-Ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Microsoft Edge";v="140"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
    {
        # Firefox no envía Client Hints (Sec-Ch-Ua): se omiten a propósito.
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0",
    },
]


class ComputrabajoExtractor(BaseExtractor):
    portal_name = "Computrabajo"
    base_url = "https://co.computrabajo.com"

    def __init__(self):
        self.new_session()

    def new_session(self) -> None:
        """Elige una huella de navegador para toda la sesión de escaneo."""
        self._profile = random.choice(BROWSER_PROFILES)

    def _get_headers(self) -> dict:
        headers = {
            "Accept-Language": "es-CO,es-419;q=0.9,es;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://co.computrabajo.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(self._profile)
        return headers

    def _is_promising_job(self, title: str) -> bool:
        # Mismas reglas que el matcher (services/filters.py)
        return is_promising_title(title)

    async def _fetch_detail(self, client: httpx.AsyncClient, job_url: str) -> Tuple[str, List[str], bool]:
        """
        Descarga la descripción completa y requisitos respetando el rate-limiting.
        Retorna (descripcion, requisitos, waf_blocked).
        """
        delay = random.uniform(3.0, 5.0)
        await asyncio.sleep(delay)
        try:
            res = await client.get(job_url, headers=self._get_headers())
            if res.status_code in (403, 429):
                print(f"[ComputrabajoExtractor] WAF rate-limit detectado ({res.status_code}). Deteniendo para proteger IP.")
                guard.trip(self.portal_name, f"HTTP {res.status_code} en detalle")
                return "", [], True
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                body = soup.find("p", class_="mbB")
                desc = body.get_text("\n", strip=True) if body else ""

                reqs = []
                req_ul = soup.find("ul", class_="disc")
                if req_ul:
                    reqs = [li.get_text(strip=True) for li in req_ul.find_all("li")]

                return desc, reqs, False
        except Exception as err:
            print(f"[ComputrabajoExtractor] Error obteniendo detalle: {err}")

        return "", [], False

    async def search_jobs(
        self, 
        query: str, 
        location: str = "bogota-dc", 
        max_pages: int = 5,
        existing_ids: Optional[Set[str]] = None,
        pubdate: Optional[int] = None
    ) -> List[JobOffer]:
        """
        Extrae ofertas de Computrabajo con rate-limiting humano, prefiltrado
        de roles no afines y extracción de la descripción y requerimientos reales.
        Por defecto pubdate=None (todas las ofertas vigentes) para no perder ninguna vacante activa.
        """
        guard.check(self.portal_name)  # respeta el enfriamiento persistente
        offers: List[JobOffer] = []
        known_ids = existing_ids or set()
        encoded_query = urllib.parse.quote_plus(query)

        async with httpx.AsyncClient(headers=self._get_headers(), follow_redirects=True, timeout=20.0) as client:
            for page in range(1, max_pages + 1):
                delay = random.uniform(3.5, 5.5)
                await asyncio.sleep(delay)

                # by=publicationtime: orden por fecha (por defecto Computrabajo ordena por
                # relevancia y las vacantes nuevas podían quedar en páginas que no se leían).
                url = f"{self.base_url}/empleos-en-{location}?q={encoded_query}&by=publicationtime"
                if pubdate:
                    url += f"&pubdate={pubdate}"
                if page > 1:
                    url += f"&p={page}"
                try:
                    response = await client.get(url, headers=self._get_headers())
                    if response.status_code in (403, 429):
                        guard.trip(self.portal_name, f"HTTP {response.status_code} en búsqueda")
                        raise WafBlocked(self.portal_name, f"HTTP {response.status_code}", offers)
                    if response.status_code != 200:
                        break

                    soup = BeautifulSoup(response.text, "html.parser")
                    articles = soup.find_all("article", class_="box_offer")
                    if not articles:
                        break
                    has_next = bool(soup.find(attrs={"title": "Siguiente"}) or
                                    soup.find("span", attrs={"data-path": re.compile(r"[?&]p=%d\b" % (page + 1))}) or
                                    soup.find("a", href=re.compile(r"[?&]p=%d\b" % (page + 1))))

                    for art in articles:
                        offer_id = art.get("data-id")
                        if offer_id and (offer_id in known_ids or f"computrabajo-{offer_id}" in known_ids):
                            continue

                        title_tag = art.find("a", class_="js-o-link")
                        if not title_tag:
                            continue

                        title = title_tag.get_text(strip=True)

                        # Fecha ANTES de pedir el detalle: no gastar peticiones en vacantes viejas
                        date_p = art.find("p", class_="fc_aux")
                        posted_date = date_p.get_text(strip=True) if date_p else None
                        if not is_fresh(posted_date):
                            continue

                        # Filtro previo: Si el título no es tecnológico o es basura, no gastar requests de detalle
                        if not self._is_promising_job(title):
                            continue

                        rel_link = title_tag.get("href", "")
                        clean_rel = rel_link.split("#")[0]
                        job_url = f"{self.base_url}{clean_rel}" if clean_rel.startswith("/") else clean_rel
                        if job_url in known_ids:
                            continue

                        company_tag = art.find("a", attrs={"offer-grid-article-company-url": True}) or art.find("a", class_="t_ellipsis")
                        company = company_tag.get_text(strip=True) if company_tag else "Confidencial / No especificada"

                        sig = f"{title.strip().lower()}::{company.strip().lower()}"
                        if sig in known_ids:
                            continue

                        # Ubicación
                        loc_p = art.find_all("p", class_="fs16")
                        loc_text = location
                        if len(loc_p) > 1:
                            loc_text = loc_p[1].get_text(strip=True)

                        # Modalidad detectada directamente de los badges del portal
                        modality = "Presencial"
                        if location.lower() in ("remoto", "teletrabajo") or "teletrabajo" in loc_text.lower():
                            modality = "Remoto"
                        for span in art.find_all("span"):
                            stext = span.get_text(strip=True).lower()
                            if "remoto" in stext and "presencial" in stext:
                                modality = "Híbrido"
                                break
                            elif "remoto" in stext or "teletrabajo" in stext or "home office" in stext:
                                modality = "Remoto"
                                break
                            elif "híbrido" in stext or "hibrido" in stext or "alternancia" in stext:
                                modality = "Híbrido"
                                break
                            elif "presencial" in stext:
                                modality = "Presencial"
                                break

                        # Salario
                        salary = "A convenir"
                        salary_icon = art.find("span", class_="i_salary")
                        if salary_icon and salary_icon.parent:
                            salary = salary_icon.parent.get_text(strip=True)

                        # Extracción segura del detalle real
                        description, requirements, waf_blocked = await self._fetch_detail(client, job_url)
                        if waf_blocked:
                            # Corte GLOBAL: la excepción detiene todo el escaneo, no solo esta consulta.
                            raise WafBlocked(self.portal_name, "HTTP 403/429 en detalle", offers)

                        if not description:
                            description = f"Vacante de {title} en {company}."

                        offer = JobOffer(
                            portal=self.portal_name,
                            external_id=offer_id,
                            title=title,
                            company=company,
                            location=loc_text,
                            modality=modality,
                            salary=salary,
                            description=description,
                            requirements=requirements,
                            url=job_url,
                            posted_date=posted_date,
                        )

                        if offer.id in known_ids:
                            continue

                        offers.append(offer)
                        known_ids.add(offer.id)
                        if offer_id:
                            known_ids.add(offer_id)

                    # No se corta por fecha: Computrabajo intercala avisos destacados fuera de orden.
                    # pubdate ya limita la ventana en el servidor; se sigue mientras haya página siguiente.
                    if not has_next or len(articles) < 20:
                        break
                except WafBlocked:
                    raise
                except Exception as err:
                    print(f"[ComputrabajoExtractor] Error en página {page}: {err}")
                    break

        return offers
