"""Perfiles de búsqueda: filtros manuales y consultas propias."""
import pytest

from core.models import JobOffer
from services import filters, profiles
from services.matcher import MatcherEngine


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_PATH", tmp_path / "profiles.json")
    yield
    filters.set_active_rules(None)


def _offer(title, desc="", **kw):
    return JobOffer(portal="Computrabajo", title=title, company="ACME", description=desc, url="https://x/" + title.replace(" ", "-"), **kw)


def test_default_profile_matches_builtin_rules():
    rules = profiles.build_rules(profiles.get_active())
    o = _offer("Desarrollador Python Junior", "Python, SQL y React. 1 año de experiencia")
    assert MatcherEngine(rules).evaluate(o) == MatcherEngine().evaluate(o)


def test_keyword_regex_accents_and_plural():
    import re
    pat = profiles.keyword_regex("contador")
    assert re.search(pat, "contadora publica") and re.search(pat, "contadores")
    assert re.search(profiles.keyword_regex("analista de datos"), "analista  de datos jr")
    assert re.search(profiles.keyword_regex("diseno"), "diseño gráfico")


def test_custom_profile_without_builtin_rules():
    p = profiles.save_profile({"name": "Contable", "use_builtin_rules": False,
                               "roles": "contador, auxiliar contable", "exclude": "ventas",
                               "skills": "siigo, excel", "queries": "contador\nauxiliar contable"})
    rules = profiles.apply_rules(p)
    m = MatcherEngine(rules)
    # 'contable' está en las exclusiones del radar, pero este perfil no las usa
    assert m.evaluate(_offer("Auxiliar Contable", "Manejo de Siigo y Excel"))[2] == "matched"
    assert m.evaluate(_offer("Asesor de ventas"))[2] == "discarded"
    assert m.evaluate(_offer("Desarrollador Python"))[2] == "discarded"
    assert filters.is_promising_title("Contadora junior")
    assert not filters.is_promising_title("Desarrollador backend")


def test_builtin_plus_extra_exclusion_and_min_score():
    p = profiles.save_profile({"name": "Sin Java", "use_builtin_rules": True, "exclude": "java",
                               "min_score": 60, "use_default_queries": True})
    m = MatcherEngine(profiles.build_rules(p))
    assert m.evaluate(_offer("Desarrollador Java"))[2] == "discarded"
    assert m.evaluate(_offer("Desarrollador Python", "python"))[2] == "discarded"  # 47 < 60
    assert m.evaluate(_offer("Desarrollador Python Junior", "python sql react fastapi"))[2] == "matched"


def test_senior_allowed_when_disabled():
    p = profiles.normalize({"name": "Senior ok", "exclude_senior": False, "use_builtin_rules": True})
    assert MatcherEngine(profiles.build_rules(p)).evaluate(_offer("Desarrollador Senior Python", "python"))[2] == "matched"


def test_freshness_window_follows_profile():
    profiles.apply_rules(profiles.normalize({"max_age_days": 10, "use_builtin_rules": True}))
    assert filters.is_fresh("Hace 8 días")
    filters.set_active_rules(None)
    assert not filters.is_fresh("Hace 8 días")


def test_search_targets():
    assert profiles.search_targets(profiles.DEFAULT_PROFILE) is None
    t = profiles.search_targets(profiles.normalize({"use_default_queries": False, "queries": ["contador"],
                                                    "city": "Medellín", "location_mode": "ambas", "max_pages": 3}))
    assert {"query": "contador", "location": "medellin", "max_pages": 3} in t["computrabajo"]
    assert any(x["location"] == "remoto" for x in t["computrabajo"])
    assert t["indeed"][0]["location"] == "Medellín"
    assert t["elempleo"] == [{"query": "contador", "max_pages": 1, "remote": True}]  # elempleo: solo Bogotá o remoto
    t = profiles.search_targets(profiles.normalize({"use_default_queries": False, "queries": ["qa"], "location_mode": "ciudad"}))
    assert t["computrabajo"] == [{"query": "qa", "location": "bogota-dc", "max_pages": 2}]
    assert t["elempleo"] == [{"query": "qa", "max_pages": 2}]


def test_store_activate_delete():
    p = profiles.save_profile({"name": "Otro"})
    assert profiles.set_active(p["id"])["name"] == "Otro"
    assert profiles.list_profiles()["active"] == p["id"]
    with pytest.raises(ValueError):
        profiles.delete_profile(profiles.DEFAULT_ID)
    assert profiles.delete_profile(p["id"])
    assert profiles.list_profiles()["active"] == profiles.DEFAULT_ID


@pytest.mark.anyio
async def test_profile_api_and_reevaluate(tmp_path, monkeypatch):
    import httpx
    import server
    from core.database import JobDatabase
    monkeypatch.setattr(server, "db", JobDatabase(str(tmp_path / "jobs.db")))
    server.db.save_offer(_offer("Auxiliar Contable", "siigo"))
    server.db.save_offer(_offer("Desarrollador Python", "python"))
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "bootstrap" in r.text
        p = (await c.post("/api/profiles", json={"name": "Contable", "use_builtin_rules": False,
                                                  "roles": "contador, auxiliar contable"})).json()
        r = (await c.post(f"/api/profiles/{p['id']}/activate")).json()
        assert r["reevaluated"] == {"evaluadas": 2, "compatibles": 1, "descartadas": 1}
        matched = (await c.get("/api/offers?status=matched")).json()
        assert [o["title"] for o in matched] == ["Auxiliar Contable"]
        r = (await c.post("/api/profiles/default/activate")).json()
        matched = (await c.get("/api/offers?status=matched")).json()
        assert [o["title"] for o in matched] == ["Desarrollador Python"]
        assert (await c.delete("/api/profiles/default")).status_code == 400


# ---------------------------------------------------------------------------
# Los filtros del radar deben poder verse desde la app (si no, el perfil por
# defecto parece vacío cuando en realidad trae cientos de reglas).
# ---------------------------------------------------------------------------

def test_humanize_pattern_reads_like_words():
    casos = {
        r"\bvendedor(?:es|as?)?\b": "vendedor",
        r"\bpr[aá]cticas?\b": "practicas",
        r"back[\s-]end": "back-end",
        r"administrador(?:/a|\(a\)|a)?\s+de\s+sistemas\b": "administrador de sistemas",
        r"\b(?:ingenier[oa]|ing)[.,]?\s*(?:.*?\s+)?(?:de\s+|en\s+)?sistemas?": "ingeniero / ing … de / en sistemas",
    }
    for patron, esperado in casos.items():
        assert profiles.humanize_pattern(patron) == esperado, patron


def test_builtin_rules_are_described_without_regex_leftovers():
    r = profiles.describe_builtin_rules()
    for clave in ("roles", "exclude", "skills", "skills_plus", "senior"):
        assert r[clave], f"{clave} no debería venir vacío"
        for palabra in r[clave]:
            assert not set(palabra) & set("[]\\*?^$|()"), f"{clave}: '{palabra}' trae restos de regex"
    assert "desarrollad" in r["roles"] and "ventas" in r["exclude"] and "python" in r["skills"]
    assert r["max_years_experience"] == filters.MAX_YEARS_EXPERIENCE
    assert r["max_age_days"] == filters.MAX_AGE_DAYS


@pytest.mark.anyio
async def test_defaults_endpoint_exposes_queries_and_rules():
    import httpx
    import server
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        d = (await c.get("/api/profiles/defaults")).json()
    assert len(d["default_queries"]) > 15, "deben verse las consultas de los tres portales"
    assert "desarrollador" in d["default_queries"]
    assert d["builtin_rules"]["roles"] and d["builtin_rules"]["exclude"]
