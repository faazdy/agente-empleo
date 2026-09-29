"""
tests/test_app.py
Suite de pruebas para el Radar de Empleo — Miguel Vásquez
Cubre: DB ordering, matcher logic, API endpoints, alert scanner state
"""

import sys
import os
import pytest
from datetime import datetime, timezone, timedelta

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import JobDatabase
from core.models import JobOffer
from services.matcher import MatcherEngine, CANDIDATE_PROFILE
from httpx import AsyncClient, ASGITransport
from server import app

# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def in_memory_db():
    """DB SQLite en memoria para tests — no toca el archivo real."""
    import sqlite3 as _sql3
    from unittest.mock import patch

    # Usamos un archivo temporal en lugar de :memory: porque
    # SQLite :memory: crea una conexión por-hilo y _init_db no ve la tabla.
    import tempfile, pathlib
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = JobDatabase(tmp.name)
    yield db
    pathlib.Path(tmp.name).unlink(missing_ok=True)


def make_offer(title, description="Python SQL React", score=None, hours_ago=1, status="new") -> JobOffer:
    scraped_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return JobOffer(
        id=f"test-{title[:20].replace(' ', '-').lower()}",
        portal="computrabajo",
        external_id=None,
        title=title,
        company="Test Corp",
        location="Bogotá D.C.",
        modality="Presencial",
        salary="A convenir",
        description=description,
        requirements=["Python", "SQL"],
        url=f"https://computrabajo.com/test/{title[:25].replace(' ', '-').lower()}",
        posted_date="2026-09-23",
        scraped_at=scraped_at,
        match_score=score,
        match_reason=None,
        status=status,
    )


# ─────────────────────────────────────────────
# 1. DATABASE — Ordenamiento y deduplicación
# ─────────────────────────────────────────────

class TestDatabaseOrdering:
    def test_offers_sorted_by_scraped_at_desc(self, in_memory_db):
        """Las vacantes más recientes deben aparecer primero."""
        offers = [
            make_offer("Vacante A Python", hours_ago=5),
            make_offer("Vacante B React",  hours_ago=1),
            make_offer("Vacante C SQL",    hours_ago=3),
        ]
        for o in offers:
            in_memory_db.save_offer(o)
            in_memory_db.update_match(o.id, 50.0, "Rol Tech", "matched")

        result = in_memory_db.get_offers(status="matched", sort_by="recent")
        titles = [r.title for r in result]
        assert titles[0] == "Vacante B React", "La más reciente debe ser la primera"
        assert titles[-1] == "Vacante A Python", "La más antigua debe ser la última"

    def test_smart_opportunity_score_ranking(self, in_memory_db):
        """El ranking smart debe priorizar vacantes de alta afinidad y recientes sobre vacantes viejas."""
        # Oferta A: 95% match pero de hace 30 días
        o_a = make_offer("Senior Dev Pasado", score=95.0, status="matched")
        o_a.posted_date = "Más de 30 días"
        # Oferta B: 90% match publicada hoy (hace 1 hora)
        o_b = make_offer("Fullstack Hoy", score=90.0, status="matched")
        o_b.posted_date = "Hace 1 hora"
        # Oferta C: 35% match publicada hace 10 minutos (soporte bajo)
        o_c = make_offer("Soporte L1", score=35.0, status="matched")
        o_c.posted_date = "Hace 10 minutos"

        in_memory_db.save_offer(o_a)
        in_memory_db.save_offer(o_b)
        in_memory_db.save_offer(o_c)
        in_memory_db.update_match(o_a.id, 95.0, "Tech", "matched")
        in_memory_db.update_match(o_b.id, 90.0, "Tech", "matched")
        in_memory_db.update_match(o_c.id, 35.0, "Soporte", "matched")

        smart_offers = in_memory_db.get_offers(status="matched", sort_by="smart")
        # Fullstack Hoy (90% + Hace 1h = 94.0 pts) debe ganarle a Senior Dev Pasado (95% + >30d = 57.0 pts)
        # y a Soporte L1 (35% + 10min = 61.0 pts)
        assert smart_offers[0].title == "Fullstack Hoy", "Fullstack fresco con 90% match debe ser Top #1"

    def test_no_duplicate_offers(self, in_memory_db):
        """Insertar la misma oferta dos veces NO debe duplicarla."""
        offer = make_offer("Desarrollador Python Junior")
        saved1 = in_memory_db.save_offer(offer)
        saved2 = in_memory_db.save_offer(offer)
        assert saved1 is True
        assert saved2 is False, "Segunda inserción debe retornar False (ya existe)"
        all_offers = in_memory_db.get_offers()
        assert len(all_offers) == 1

    def test_scraped_at_integrity(self, in_memory_db):
        """Ninguna oferta debe tener scraped_at nulo o vacío."""
        for i in range(3):
            in_memory_db.save_offer(make_offer(f"Oferta {i}"))
        result = in_memory_db.get_offers()
        for o in result:
            assert o.scraped_at is not None
            assert str(o.scraped_at) != ""


# ─────────────────────────────────────────────
# 2. MATCHER — Lógica de evaluación
# ─────────────────────────────────────────────

class TestMatcherEngine:
    def setup_method(self):
        self.matcher = MatcherEngine()

    def test_tech_role_matches(self):
        """Vacantes con título tech conocido deben superar el umbral (≥20)."""
        tech_titles = [
            ("Desarrollador Python Junior", "Trabajarás con Python, FastAPI, Docker y SQL"),
            ("Analista de Datos", "Power BI, SQL, ETL, Pandas, Dashboard"),
            ("Ingeniero de Sistemas", "Administración de servidores, soporte técnico, redes"),
            ("Full Stack Developer", "React, Node.js, TypeScript, PostgreSQL"),
        ]
        for title, desc in tech_titles:
            offer = make_offer(title, desc)
            score, reason, status = self.matcher.evaluate(offer)
            assert status == "matched", f"'{title}' debería ser matched, fue '{status}' (score={score:.1f})"
            assert score >= 20.0, f"'{title}' score={score:.1f} menor al umbral 20"

    def test_negative_patterns_discard(self):
        """Vacantes con palabras clave negativas en el título deben ser descartadas."""
        negative_titles = [
            ("Asesor de Ventas", "Ventas telefónicas, call center, CRM"),
            ("Médico General", "Atención clínica, salud, medicina general"),
            ("Técnico Automotriz", "Mecánica automotriz, taller, diagnóstico"),
        ]
        for title, desc in negative_titles:
            offer = make_offer(title, desc)
            _, reason, status = self.matcher.evaluate(offer)
            assert status == "discarded", f"'{title}' debería ser discarded, fue '{status}'"

    def test_senior_title_is_discarded(self):
        """Perfil Jr: títulos Senior/Líder/Arquitecto se descartan aunque el stack sea bueno."""
        for title in ["Senior Python Developer", "Líder Técnico FullStack", "Arquitecto de Software IA"]:
            offer = make_offer(title, "Python, FastAPI, PostgreSQL, Docker, AWS")
            _, reason, status = self.matcher.evaluate(offer)
            assert status == "discarded", f"'{title}' debería descartarse (fue {status})"

    def test_semi_senior_is_penalized_not_discarded(self):
        offer = make_offer("Desarrollador Semi Senior Python", "Python, FastAPI, SQL, Docker")
        score, reason, status = self.matcher.evaluate(offer)
        assert status == "matched" and "Semi Senior" in reason

    def test_years_of_experience_discard(self):
        """Si la descripción pide más de 3 años (p. ej. 4 años), se descarta."""
        offer = make_offer("Desarrollador Python", "Requisitos: mínimo cuatro (4) años de experiencia con Python y SQL")
        _, reason, status = self.matcher.evaluate(offer)
        assert status == "discarded" and "4 años" in reason

    def test_years_in_requirements_list_are_read(self):
        offer = make_offer("Desarrollador React", "React, TypeScript")
        offer.requirements = ["4 años de experiencia"]
        _, _, status = self.matcher.evaluate(offer)
        assert status == "discarded"

    def test_two_years_is_penalized_not_discarded(self):
        offer = make_offer("Desarrollador Python", "Python, FastAPI, SQL, Docker. Experiencia de 2 años")
        score, reason, status = self.matcher.evaluate(offer)
        assert status == "matched" and "-10" in reason

    def test_hard_tech_skills_fallback(self):
        """Si el título no es claramente tech, pero la descripción tiene skills fuertes → matched."""
        offer = make_offer(
            "Profesional de Proyectos",
            "Experiencia con Python, SQL, Power BI, análisis de datos, ETL con Pandas"
        )
        score, reason, status = self.matcher.evaluate(offer)
        assert status == "matched", f"Cargo alternativo con stack sólido debe ser matched (score={score:.1f})"

    def test_threshold_at_20(self):
        """Vacante con exactamente score en el límite debe pasar."""
        # Una vacante de soporte técnico básico debería score ~35 con el matcher actual
        offer = make_offer("Técnico de Soporte TI", "Soporte técnico L1, redes, Active Directory, helpdesk")
        score, _, status = self.matcher.evaluate(offer)
        assert score >= 20.0, f"Soporte TI debería superar umbral 20, score={score:.1f}"

    def test_profile_has_minimum_coverage(self):
        """El perfil debe tener cobertura mínima en todas sus dimensiones."""
        assert len(CANDIDATE_PROFILE["preferred_roles"]) >= 20, "Mínimo 20 patrones de rol"
        assert len(CANDIDATE_PROFILE["primary_skills"]) >= 15, "Mínimo 15 skills primarias"
        assert len(CANDIDATE_PROFILE["secondary_skills"]) >= 10, "Mínimo 10 skills secundarias"
        assert len(CANDIDATE_PROFILE["title_negative_patterns"]) >= 30, "Mínimo 30 filtros negativos"


# ─────────────────────────────────────────────
# 3. API ENDPOINTS — Integración
# ─────────────────────────────────────────────

@pytest.mark.anyio
class TestAPIEndpoints:
    async def test_root_returns_html(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Radar de Empleo" in r.text

    async def test_get_offers_default(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/offers")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    async def test_get_offers_status_filter(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/offers?status=matched")
        assert r.status_code == 200
        data = r.json()
        for item in data:
            assert item["status"] == "matched"

    async def test_get_offers_sorted_recent_first(self):
        """La API debe devolver vacantes ordenadas por scraped_at DESC cuando sort_by=recent."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/offers?status=matched&sort_by=recent")
        assert r.status_code == 200
        data = r.json()
        if len(data) >= 2:
            dates = [item["scraped_at"] for item in data if item.get("scraped_at")]
            for i in range(len(dates) - 1):
                assert dates[i] >= dates[i + 1], (
                    f"Orden roto: {dates[i]} debe ser >= {dates[i+1]}"
                )

    async def test_get_offers_sorted_smart(self):
        """La API debe responder exitosamente con ordenamiento smart (Afinidad + Frescura)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/offers?status=matched&sort_by=smart")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    async def test_get_stats(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/stats")
        assert r.status_code == 200
        stats = r.json()
        assert "total" in stats
        assert "matched" in stats
        assert stats["total"] >= 0

    async def test_sync_status(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/sync/status")
        assert r.status_code == 200
        st = r.json()
        assert "is_running" in st
        assert "total_queries" in st

    async def test_alert_status(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/alert/status")
        assert r.status_code == 200
        st = r.json()
        assert "is_running" in st
        assert "next_scan_at" in st
        assert "last_new_count" in st

    async def test_update_offer_status(self):
        """Cambiar el status de una vacante existente debe funcionar."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Get first offer
            r = await client.get("/api/offers?status=matched")
            offers = r.json()
            if not offers:
                pytest.skip("No hay vacantes matched en la DB para probar")
            offer_id = offers[0]["id"]
            # Mark as applied
            r2 = await client.post(
                f"/api/offers/{offer_id}/status",
                json={"status": "applied"}
            )
        assert r2.status_code == 200
        assert r2.json()["success"] is True
        # Restore to matched
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(f"/api/offers/{offer_id}/status", json={"status": "matched"})

    async def test_qa_entries(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/qa/entries")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0, "Debe haber al menos una respuesta ATS"


# ─────────────────────────────────────────────
# 4. ALERT SCANNER — Lógica de estado
# ─────────────────────────────────────────────

class TestAlertScannerState:
    def test_alert_state_has_required_fields(self):
        from server import alert_state
        assert "is_running" in alert_state
        assert "next_scan_at" in alert_state
        assert "last_new_count" in alert_state
        assert "total_found_today" in alert_state

    def test_next_scan_is_in_future(self):
        from server import alert_state
        if alert_state["next_scan_at"]:
            next_dt = datetime.fromisoformat(alert_state["next_scan_at"].replace("Z", "+00:00"))
            # Allow 1 second of clock skew
            assert next_dt > datetime.now(timezone.utc) - timedelta(seconds=1), (
                "next_scan_at debe estar en el futuro"
            )

    def test_alert_interval_is_reasonable(self):
        from server import ALERT_INTERVAL_SECONDS
        assert 15 * 60 <= ALERT_INTERVAL_SECONDS <= 4 * 60 * 60, (
            "El intervalo de alerta debe estar entre 15 min y 4 horas (anti-ban)"
        )

    def test_alert_targets_are_lightweight(self):
        from server import ALERT_TARGETS
        for t in ALERT_TARGETS:
            assert t["max_pages"] == 1, (
                f"Alert targets deben usar solo página 1 para no saturar. "
                f"Query '{t['query']}' usa {t['max_pages']} páginas"
            )
        assert len(ALERT_TARGETS) <= 15, "Máximo 15 queries en modo alerta (anti-ban)"


# ─────────────────────────────────────────────
# 5. FILTROS COMPARTIDOS Y GUARDIÁN ANTI-BANEO
# ─────────────────────────────────────────────

class TestSharedFilters:
    def test_required_years_parsing(self):
        from services.filters import required_years
        cases = {
            "3 años de experiencia": 3, "experiencia mínima de dos (2) años": 2,
            "5 años o más de experiencia": 5, "3+ years of experience": 3,
            "Empresa con 25 años en el mercado": None, "sin experiencia": None,
        }
        for text, expected in cases.items():
            assert required_years(text) == expected, text

    def test_prefilter_uses_matcher_rules(self):
        from services.filters import is_promising_title
        assert is_promising_title("Ing. de Sistemas")
        assert is_promising_title("Analista Power BI")
        assert not is_promising_title("Programador de Automatización PLC y HMI")
        assert not is_promising_title("Desarrollador Backend Senior Java")
        assert not is_promising_title("Asesor comercial")

    def test_extractor_and_matcher_share_lists(self):
        from services import filters
        assert CANDIDATE_PROFILE["title_negative_patterns"] is filters.TITLE_NEGATIVE_PATTERNS
        assert CANDIDATE_PROFILE["preferred_roles"] is filters.PREFERRED_ROLES


class TestScrapeGuard:
    def _guard(self, tmp_path):
        from core.guard import ScrapeGuard
        return ScrapeGuard(str(tmp_path / "cooldown.json"))

    def test_trip_persists_and_blocks(self, tmp_path):
        from core.guard import ScrapeGuard, WafBlocked
        g = self._guard(tmp_path)
        g.trip("Computrabajo", "HTTP 403")
        # Un guardián nuevo (como tras reiniciar el servidor) sigue viendo el bloqueo
        g2 = ScrapeGuard(str(tmp_path / "cooldown.json"))
        assert g2.blocked_until("Computrabajo") is not None
        with pytest.raises(WafBlocked):
            g2.check("Computrabajo")
        g2.check("Indeed")  # otro portal no se ve afectado

    def test_clear_and_expired(self, tmp_path):
        g = self._guard(tmp_path)
        g.trip("Indeed", "captcha", hours=-1)  # ya expirado
        assert g.blocked_until("Indeed") is None
        g.trip("Indeed", "captcha")
        g.clear("Indeed")
        assert g.blocked_until("Indeed") is None

    def test_alert_window(self):
        from core.guard import in_alert_window
        # 12:00 UTC = 07:00 Bogotá (abre); 02:00 UTC = 21:00 Bogotá (cerrado)
        assert in_alert_window(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        assert not in_alert_window(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        assert not in_alert_window(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))  # 03:00 Bogotá


@pytest.mark.anyio
async def test_computrabajo_403_raises_global_stop(tmp_path, monkeypatch):
    """Un 403 debe lanzar WafBlocked (corte global) y dejar el enfriamiento guardado."""
    import httpx
    import core.guard as guard_mod
    import extractors.computrabajo as ct
    from core.guard import ScrapeGuard, WafBlocked

    g = ScrapeGuard(str(tmp_path / "cooldown.json"))
    monkeypatch.setattr(ct, "guard", g)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(ct.asyncio, "sleep", no_sleep)

    async def fake_get(self, url, **kw):
        return httpx.Response(403, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(WafBlocked):
        await ct.ComputrabajoExtractor().search_jobs("desarrollador python")
    assert g.blocked_until("Computrabajo") is not None


def test_browser_profile_is_coherent_per_session():
    from extractors.computrabajo import ComputrabajoExtractor
    ex = ComputrabajoExtractor()
    for _ in range(5):
        ex.new_session()
        h1, h2 = ex._get_headers(), ex._get_headers()
        assert h1["User-Agent"] == h2["User-Agent"], "La huella no debe cambiar dentro de una sesión"
        if "Firefox" in h1["User-Agent"]:
            assert "Sec-Ch-Ua" not in h1
        else:
            assert "Sec-Ch-Ua" in h1


class TestIndeedHelpers:
    def test_build_url(self):
        from extractors.indeed import IndeedExtractor
        url = IndeedExtractor.build_url("desarrollador python", "Bogotá, D.C.")
        assert url.startswith("https://co.indeed.com/jobs?") and "fromage=7" in url and "sort=date" in url
        # Remoto: el filtro sc=... no funciona en co.indeed.com; se agrega "remoto" a la consulta
        rurl = IndeedExtractor.build_url("desarrollador", "Colombia", remote=True)
        assert "q=desarrollador+remoto" in rurl and "sc=" not in rurl and "l=Colombia" in rurl
        assert "remoto+remoto" not in IndeedExtractor.build_url("desarrollador remoto", "Colombia", remote=True)

    def test_normalize_posted_is_understood_by_db(self):
        import time
        from extractors.indeed import _normalize_posted
        now_ms = time.time() * 1000
        assert JobDatabase.is_today_or_yesterday(_normalize_posted(now_ms - 3 * 3600_000, ""))
        assert JobDatabase.is_today_or_yesterday(_normalize_posted(now_ms - 30 * 3600_000, ""))
        assert not JobDatabase.is_today_or_yesterday(_normalize_posted(now_ms - 80 * 3600_000, ""))
        assert JobDatabase.is_today_or_yesterday(_normalize_posted(None, "Publicado hoy"))


class _FakeEl:
    def __init__(self, text): self.text = text
    async def inner_text(self): return self.text


class _FakePage:
    def __init__(self, items, blocked=False, status=200, titles=None):
        self.items, self.blocked, self.status, self.visited = items, blocked, status, []
        self.titles = list(titles) if titles is not None else None   # secuencia de títulos (verificación que se resuelve)
        class _Ctx:
            async def new_cdp_session(self_inner, page): raise RuntimeError("sin CDP en la prueba")
        self.context = _Ctx()
    async def goto(self, url, **kw):
        self.visited.append(url)
        outer = self
        # Como en la realidad: el 403 solo acompaña a la pantalla de verificación; resuelta, vuelve el 200
        challenged = self.titles is None or any(m in self.titles[0].lower() for m in ("moment", "momento", "denied"))
        class R:
            status = outer.status if challenged else 200
        return R()
    async def title(self):
        if self.titles is not None:
            return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]
        return "Just a moment..." if self.blocked else "Empleos | Indeed"
    async def evaluate(self, js): return None if "pagination" in js else self.items
    async def query_selector(self, sel):
        # Simula el marcado ACTUAL de Indeed: el selector viejo ya no existe
        if sel != "div.simple-job-description-html":
            return None
        return _FakeEl("Buscamos desarrollador. Python, FastAPI, SQL. 1 año de experiencia")
    async def bring_to_front(self): pass
    async def close(self): pass


class _FakeController:
    def __init__(self, page): self.page = page
    async def connect(self):
        page = self.page
        class Ctx:
            async def new_page(self_inner): return page
        self._context = Ctx()
    async def close(self): pass


@pytest.mark.anyio
async def test_indeed_flow_with_fake_browser(tmp_path, monkeypatch):
    import time
    import extractors.indeed as ind
    from core.guard import ScrapeGuard, WafBlocked
    g = ScrapeGuard(str(tmp_path / "cooldown.json"))
    monkeypatch.setattr(ind, "guard", g)
    async def no_sleep(_): return None
    monkeypatch.setattr(ind.asyncio, "sleep", no_sleep)

    now_ms = time.time() * 1000
    items = [
        {"jk": "a1", "title": "Desarrollador Python Junior", "company": "ACME", "location": "Bogotá, D.C.",
         "remote": False, "salary": "", "snippet": "Python", "relative": "", "pubDate": now_ms - 3600_000},
        {"jk": "a2", "title": "Asesor comercial", "company": "X", "location": "Bogotá", "remote": False,
         "salary": "", "snippet": "", "relative": "", "pubDate": now_ms},
        {"jk": "a3", "title": "Analista de datos", "company": "Y", "location": "Remoto", "remote": True,
         "salary": "", "snippet": "SQL", "relative": "", "pubDate": now_ms},
    ]
    page = _FakePage(items)
    ex = ind.IndeedExtractor(controller_factory=lambda: _FakeController(page), max_details_per_session=1)
    offers = await ex.search_many([{"query": "desarrollador", "location": "Bogotá, D.C.", "max_pages": 1}], set())
    assert [o.external_id for o in offers] == ["a1", "a3"]          # el asesor comercial se filtra
    assert offers[0].portal == "Indeed" and "FastAPI" in offers[0].description  # detalle leído
    assert offers[1].modality == "Remoto" and offers[1].description == "SQL"     # tope de detalle = 1
    assert sum("viewjob" in u for u in page.visited) == 1




class TestFreshnessAndCoverage:
    def test_is_fresh_window(self):
        from services.filters import is_fresh
        assert is_fresh("Hace 8 minutos") and is_fresh("Hace 9 horas") and is_fresh("Ayer")
        assert is_fresh("Hace 2 días") and is_fresh("Hace 3 días") and is_fresh("Hace 4 días")
        assert not is_fresh("Hace 5 días") and not is_fresh("Más de 30 días")
        assert is_fresh(None) and is_fresh("fecha rara")  # ante la duda, no perder

    def test_non_software_programmers_filtered(self):
        from services.filters import is_promising_title
        for t in ["Programador de cirugías", "Programador de malla horaria", "Desarrolador/ Promotor"]:
            assert not is_promising_title(t), t
        assert is_promising_title("ingeniero programador")

    def test_remote_targets_use_correct_slug(self):
        from server import SEARCH_TARGETS, ALERT_TARGETS
        locs = {t["location"] for t in SEARCH_TARGETS + ALERT_TARGETS}
        assert "teletrabajo" not in locs and "remoto" in locs


def _ct_article(i, title, date="Hace 3 horas"):
    return (f'<article class="box_offer" data-id="id{i}"><a class="js-o-link" href="/ofertas-de-trabajo/x-{i}">{title}</a>'
            f'<a class="t_ellipsis">Empresa {i}</a><p class="fs16">x</p><p class="fs16">Bogotá</p>'
            f'<p class="fc_aux">{date}</p></article>')


@pytest.mark.anyio
async def test_computrabajo_paginates_sorted_by_date(tmp_path, monkeypatch):
    """Debe ordenar por fecha, seguir mientras haya 'Siguiente' y no pedir detalle de vacantes viejas."""
    import httpx
    import extractors.computrabajo as ct
    from core.guard import ScrapeGuard
    monkeypatch.setattr(ct, "guard", ScrapeGuard(str(tmp_path / "c.json")))
    async def no_sleep(_): return None
    monkeypatch.setattr(ct.asyncio, "sleep", no_sleep)

    page1 = "".join(_ct_article(i, f"Desarrollador Python {i}") for i in range(19)) + \
            _ct_article(99, "Desarrollador viejo", "Hace 5 días") + '<span title="Siguiente" data-path="?q=x&p=2">Siguiente</span>'
    page2 = _ct_article(200, "Desarrollador React fresco", "Hace 4 horas") + '<span class="disabled">Siguiente</span>'
    calls = []
    async def fake_get(self, url, **kw):
        calls.append(url)
        if "/ofertas-de-trabajo/" in url:
            return httpx.Response(200, text='<p class="mbB">Python React</p>', request=httpx.Request("GET", url))
        body = page2 if "&p=2" in url else page1
        return httpx.Response(200, text=f"<html>{body}</html>", request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    offers = await ct.ComputrabajoExtractor().search_jobs("desarrollador", pubdate=2)
    search_calls = [c for c in calls if "/ofertas-de-trabajo/" not in c]
    assert all("by=publicationtime" in c for c in search_calls)
    assert len(search_calls) == 2, "debe leer la página 2"
    titles = {o.title for o in offers}
    assert "Desarrollador React fresco" in titles          # la vacante fresca de la página 2 no se pierde
    assert "Desarrollador viejo" not in titles
    assert not any("x-99" in c for c in calls)            # sin petición de detalle para la vieja



# ─────────────────────────────────────────────
# 6. SEGUIMIENTO, SYNC DIARIO, QUÉ APRENDER, NAVEGADOR
# ─────────────────────────────────────────────

class TestTracking:
    def test_applied_sets_stage_and_date(self, in_memory_db):
        o = make_offer("Desarrollador Python Tracking")
        in_memory_db.save_offer(o)
        in_memory_db.update_status(o.id, "applied")
        got = [x for x in in_memory_db.get_offers(status="applied") if x.id == o.id][0]
        assert got.stage == "postulado" and got.applied_at

    def test_update_tracking_and_summary(self, in_memory_db):
        o = make_offer("Desarrollador React Tracking")
        in_memory_db.save_offer(o)
        assert not in_memory_db.update_tracking(o.id, stage="entrevista")  # aún no postulada
        in_memory_db.update_status(o.id, "applied")
        assert in_memory_db.update_tracking(o.id, stage="entrevista", notes="lunes")
        summ = in_memory_db.get_tracking_summary()
        assert summ["by_stage"]["entrevista"] == 1 and summ["response_rate"] == 100.0
        with pytest.raises(ValueError):
            in_memory_db.update_tracking(o.id, stage="inventada")

    def test_stale_detection(self, in_memory_db):
        o = make_offer("Analista de datos viejo")
        in_memory_db.save_offer(o)
        in_memory_db.update_status(o.id, "applied")
        future = datetime.now(timezone.utc) + timedelta(days=30)
        summ = in_memory_db.get_tracking_summary(now=future)
        assert summ["stale_count"] == 1 and summ["stale"][0]["days"] >= 21

    def test_unapply_clears_tracking(self, in_memory_db):
        o = make_offer("Desarrollador unapply")
        in_memory_db.save_offer(o)
        in_memory_db.update_status(o.id, "applied")
        in_memory_db.update_status(o.id, "matched")
        got = [x for x in in_memory_db.get_offers() if x.id == o.id][0]
        assert got.stage is None and got.applied_at is None

    def test_migration_backfills_old_applied(self, tmp_path):
        import sqlite3
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE job_offers (id TEXT PRIMARY KEY, portal TEXT NOT NULL, external_id TEXT,
            title TEXT NOT NULL, company TEXT NOT NULL, location TEXT, modality TEXT, salary TEXT,
            description TEXT NOT NULL, requirements TEXT, url TEXT NOT NULL, posted_date TEXT,
            scraped_at TEXT NOT NULL, match_score REAL, match_reason TEXT, status TEXT NOT NULL DEFAULT 'new')""")
        conn.execute("INSERT INTO job_offers VALUES ('a','Computrabajo',NULL,'Dev','X',NULL,NULL,NULL,'d','[]','u',NULL,'2026-09-01T10:00:00',50,NULL,'applied')")
        conn.commit(); conn.close()
        db = JobDatabase(str(path))
        o = db.get_offers(status="applied")[0]
        assert o.stage == "postulado" and o.applied_at == "2026-09-01T10:00:00"


class TestDailySync:
    def test_due_logic(self):
        from server import daily_sync_due
        # 13:00 UTC = 08:00 Bogotá
        now = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
        assert daily_sync_due(now, {})                                           # nunca se ha hecho
        assert daily_sync_due(now, {"last_full_sync_at": "2026-09-25T14:00:00+00:00"})  # fue ayer
        assert not daily_sync_due(now, {"last_full_sync_at": "2026-09-26T12:40:00+00:00"})  # ya se hizo hoy
        early = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)                 # 07:00 Bogotá
        assert not daily_sync_due(early, {})                                     # antes de 07:30
        night = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)                  # 22:00 Bogotá
        assert not daily_sync_due(night, {})                                     # fuera de ventana


class TestSkillsGap:
    def test_detect_and_analyze(self):
        from services.skills_gap import detect, analyze
        assert set(detect("Buscamos Java, Spring Boot y AWS. Inglés B2")) >= {"Java", "Spring Boot", "AWS", "Inglés B2+"}
        assert "Java" not in detect("JavaScript y TypeScript")
        offers = [make_offer("Dev A", "Java y AWS"), make_offer("Dev B", "Python y AWS"), make_offer("Dev C", "Python")]
        r = analyze(offers)
        top = {x["skill"]: x for x in r["skills"]}
        assert top["AWS"]["count"] == 2 and not top["AWS"]["have"]
        assert top["Python"]["have"]
        assert r["top_gaps"][0]["skill"] == "AWS"


class TestManagedBrowser:
    def test_find_browser_prefers_edge(self, monkeypatch, tmp_path):
        import core.browser as br
        edge = tmp_path / "msedge.exe"; edge.write_text("x")
        brave = tmp_path / "brave.exe"; brave.write_text("x")
        monkeypatch.delenv("RADAR_BROWSER_PATH", raising=False)
        monkeypatch.setattr(br, "BROWSER_CANDIDATES", [("Edge", str(edge)), ("Brave", str(brave))])
        assert br.find_browser()[0] == "Edge"
        edge.unlink()
        assert br.find_browser()[0] == "Brave"

    @pytest.mark.anyio
    async def test_ensure_launches_offscreen_with_dedicated_profile(self, monkeypatch, tmp_path):
        import core.browser as br
        calls = []
        state = {"alive": False}
        async def fake_alive(port=br.CDP_PORT): return state["alive"]
        def fake_popen(args, **kw):
            calls.append(args); state["alive"] = True
        async def no_sleep(_): return None
        monkeypatch.setattr(br, "cdp_alive", fake_alive)
        monkeypatch.setattr(br.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(br.asyncio, "sleep", no_sleep)
        monkeypatch.setattr(br, "find_browser", lambda: ("Edge", "msedge.exe"))
        mb = br.ManagedBrowser(port=9223, profile_dir=tmp_path / "perfil")
        assert await mb.ensure() is True
        args = calls[0]
        assert "--remote-debugging-port=9223" in args
        assert any(a.startswith("--user-data-dir=") and "perfil" in a for a in args)
        assert "--window-position=-32000,-32000" in args
        assert mb.launched_by_app and not mb.visible
        assert await mb.ensure() is False   # ya está vivo: no abre otro

    @pytest.mark.anyio
    async def test_ensure_without_browser_raises(self, monkeypatch):
        import core.browser as br
        async def fake_alive(port=br.CDP_PORT): return False
        monkeypatch.setattr(br, "cdp_alive", fake_alive)
        monkeypatch.setattr(br, "find_browser", lambda: None)
        with pytest.raises(ConnectionError):
            await br.ManagedBrowser().ensure()


@pytest.mark.anyio
async def test_new_endpoints_respond():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        for url in ["/api/tracking", "/api/daily/status", "/api/skills-gap?scope=relevant", "/api/guard/status", "/api/browser/status"]:
            r = await ac.get(url)
            assert r.status_code == 200, url
        r = await ac.post("/api/offers/no-existe/tracking", json={"stage": "entrevista"})
        assert r.status_code == 404



@pytest.mark.anyio
async def test_sync_portal_selection(monkeypatch):
    """Escanear solo Indeed no debe tocar Computrabajo, y viceversa."""
    import server
    calls = {"ct": 0, "indeed": 0}
    async def fake_ct(**kw):
        calls["ct"] += 1
        return []
    async def fake_indeed(targets, existing_ids=None, on_progress=None, **kw):
        calls["indeed"] += 1
        return []
    monkeypatch.setattr(server.extractor, "search_jobs", lambda **kw: fake_ct(**kw))
    monkeypatch.setattr(server.indeed_extractor, "search_many", fake_indeed)
    async def no_sleep(_): return None
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(server, "_save_state", lambda u: None)

    await server._run_sync_worker(("indeed",))
    assert calls == {"ct": 0, "indeed": 1}
    assert server.sync_state["total_queries"] == len(server.INDEED_TARGETS)
    await server._run_sync_worker(("computrabajo",))
    assert calls["ct"] == len(server.SEARCH_TARGETS) and calls["indeed"] == 1


@pytest.mark.anyio
async def test_sync_endpoint_validates_portal():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/api/sync?portal=magneto")
        assert r.status_code == 400


def test_indeed_targets_remote_are_not_using_broken_filter():
    from server import INDEED_TARGETS
    from extractors.indeed import IndeedExtractor
    urls = [IndeedExtractor.build_url(t["query"], t["location"], t.get("remote", False)) for t in INDEED_TARGETS]
    assert not any("sc=" in u or "l=remoto" in u for u in urls)
    assert sum("remoto" in u for u in urls) >= 3


class TestIndeedFreshness:
    def test_relative_text_has_priority_over_original_pubdate(self):
        """pubDate es la fecha ORIGINAL; el texto relativo refleja la última actualización."""
        import time
        from extractors.indeed import _normalize_posted
        old_pub = (time.time() - 13 * 86400) * 1000
        assert JobDatabase.is_fresh(_normalize_posted(old_pub, "hace 1 día"))        # reposteado ayer
        assert JobDatabase.is_fresh(_normalize_posted(old_pub, "Recién publicado"))
        assert JobDatabase.is_fresh(_normalize_posted(old_pub, "hace 2 días"))
        assert JobDatabase.is_fresh(_normalize_posted(old_pub, "hace 4 días"))
        assert not JobDatabase.is_fresh(_normalize_posted(old_pub, "hace 5 días"))
        assert not JobDatabase.is_fresh(_normalize_posted(old_pub, "hace 30+ días"))

    def test_falls_back_to_pubdate_without_relative(self):
        import time
        from extractors.indeed import _normalize_posted
        assert JobDatabase.is_fresh(_normalize_posted((time.time() - 5 * 3600) * 1000, ""))
        assert not JobDatabase.is_fresh(_normalize_posted((time.time() - 6 * 86400) * 1000, ""))



class TestIsolatedLoop:
    @pytest.mark.anyio
    async def test_runs_in_its_own_loop_and_returns_value(self):
        import asyncio
        from core.isolated import run_isolated
        outer = asyncio.get_running_loop()
        async def job():
            await asyncio.sleep(0)
            return asyncio.get_running_loop()
        inner = await run_isolated(job)
        assert inner is not outer

    @pytest.mark.anyio
    async def test_propagates_exceptions(self):
        from core.isolated import run_isolated
        async def boom():
            raise NotImplementedError()
        with pytest.raises(NotImplementedError):
            await run_isolated(boom)

    def test_describe_error_is_never_empty(self):
        from core.isolated import describe_error
        assert describe_error(NotImplementedError()) == "NotImplementedError"
        assert describe_error(ValueError("x")) == "ValueError: x"


@pytest.mark.anyio
async def test_indeed_unexpected_error_shows_type_not_blank(monkeypatch):
    """El aviso 'Error en Indeed:' salía vacío. Ahora siempre incluye el tipo de error."""
    import server
    async def fake_indeed(targets, existing_ids=None, on_progress=None, **kw):
        raise NotImplementedError()
    async def fake_ct(**kw): return []
    monkeypatch.setattr(server.indeed_extractor, "search_many", fake_indeed)
    monkeypatch.setattr(server.extractor, "search_jobs", lambda **kw: fake_ct(**kw))
    async def no_sleep(_): return None
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(server, "_save_state", lambda u: None)
    await server._run_sync_worker(("indeed",))
    assert "NotImplementedError" in server.sync_state["portal_errors"]["Indeed"]



class TestIndeedChallenge:
    """Caso real del 2026-09-25: Indeed mostró 'Un momento…' (Cloudflare) con HTTP 403 y la app
    activó una pausa de 2 h como si fuera un bloqueo de IP."""

    def _setup(self, tmp_path, monkeypatch):
        import extractors.indeed as ind
        from core.guard import ScrapeGuard
        g = ScrapeGuard(str(tmp_path / "cooldown.json"))
        monkeypatch.setattr(ind, "guard", g)
        async def no_sleep(_): return None
        monkeypatch.setattr(ind.asyncio, "sleep", no_sleep)
        return ind, g

    def _items(self):
        import time
        return [{"jk": "z1", "title": "Desarrollador Python Junior", "company": "ACME", "location": "Bogotá",
                 "remote": False, "salary": "", "snippet": "Python", "relative": "Recién publicado",
                 "pubDate": time.time() * 1000}]

    @pytest.mark.anyio
    async def test_challenge_that_resolves_itself_is_not_a_block(self, tmp_path, monkeypatch):
        ind, g = self._setup(tmp_path, monkeypatch)
        # 403 + "Un momento…" durante 3 consultas de título y luego la página normal
        page = _FakePage(self._items(), status=403, titles=["Un momento…", "Un momento…", "Un momento…", "Empleos | Indeed"])
        ex = ind.IndeedExtractor(controller_factory=lambda: _FakeController(page))
        offers = await ex.search_many([{"query": "desarrollador", "location": "Bogotá", "max_pages": 1}], set())
        assert [o.external_id for o in offers] == ["z1"]
        assert g.blocked_until("Indeed") is None and "indeed" not in g.status()["portals"]

    @pytest.mark.anyio
    async def test_unresolved_challenge_asks_for_verification_without_cooldown(self, tmp_path, monkeypatch):
        from core.guard import VerificationRequired, WafBlocked
        ind, g = self._setup(tmp_path, monkeypatch)
        page = _FakePage(self._items(), status=403, titles=["Un momento…"])   # nunca se resuelve
        ex = ind.IndeedExtractor(controller_factory=lambda: _FakeController(page))
        with pytest.raises(VerificationRequired) as ei:
            await ex.search_many([{"query": "x", "location": "Bogotá"}], set(), interactive=True)
        assert isinstance(ei.value, WafBlocked)
        assert g.blocked_until("Indeed") is None                       # sin pausa de 2 h
        assert g.status()["portals"]["indeed"]["needs_verification"] is True
        g.check("Indeed")                                              # y se puede volver a intentar

    @pytest.mark.anyio
    async def test_unattended_scan_does_not_open_window(self, tmp_path, monkeypatch):
        from core.guard import VerificationRequired
        ind, g = self._setup(tmp_path, monkeypatch)
        page = _FakePage(self._items(), status=403, titles=["Just a moment..."])
        shown = []
        page.bring_to_front = lambda: shown.append(1)
        ex = ind.IndeedExtractor(controller_factory=lambda: _FakeController(page))
        with pytest.raises(VerificationRequired):
            await ex.search_many([{"query": "x", "location": "Bogotá"}], set(), interactive=False)
        assert not shown

    @pytest.mark.anyio
    async def test_real_403_still_triggers_cooldown(self, tmp_path, monkeypatch):
        from core.guard import WafBlocked, VerificationRequired
        ind, g = self._setup(tmp_path, monkeypatch)
        page = _FakePage(self._items(), status=403, titles=["Access Denied"])
        ex = ind.IndeedExtractor(controller_factory=lambda: _FakeController(page))
        with pytest.raises(WafBlocked) as ei:
            await ex.search_many([{"query": "x", "location": "Bogotá"}], set())
        assert not isinstance(ei.value, VerificationRequired)
        assert g.blocked_until("Indeed") is not None

    @pytest.mark.anyio
    async def test_successful_page_clears_verification_flag(self, tmp_path, monkeypatch):
        ind, g = self._setup(tmp_path, monkeypatch)
        g.mark_verification("Indeed", "prueba")
        page = _FakePage(self._items())
        ex = ind.IndeedExtractor(controller_factory=lambda: _FakeController(page))
        await ex.search_many([{"query": "x", "location": "Bogotá", "max_pages": 1}], set())
        assert "indeed" not in g.status()["portals"]

    @pytest.mark.anyio
    async def test_clear_endpoint(self, tmp_path, monkeypatch):
        import server
        from core.guard import ScrapeGuard
        g = ScrapeGuard(str(tmp_path / "c.json"))
        monkeypatch.setattr(server, "guard", g)
        g.trip("Indeed", "x")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            assert (await ac.post("/api/guard/clear?portal=indeed")).status_code == 200
            assert (await ac.post("/api/guard/clear?portal=otro")).status_code == 400
        assert g.blocked_until("Indeed") is None
