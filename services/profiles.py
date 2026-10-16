"""
Perfiles de búsqueda.

Un perfil reúne TODO lo que el usuario puede ajustar a mano desde la app:
qué buscar (consultas, ciudad, remoto, portales, días hacia atrás) y cómo filtrar
(cargos afines, palabras que descartan, tecnologías, años de experiencia, puntaje mínimo).

Se guardan en data/profiles.json. El perfil "Miguel (por defecto)" reproduce exactamente
las reglas de siempre (services/filters.py + services/matcher.py); los demás perfiles
pueden sumarse a esas reglas o reemplazarlas por completo (use_builtin_rules = False).
"""
import json
import re
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional

from services import filters

PROFILES_PATH = Path("data/profiles.json")
DEFAULT_ID = "default"

DEFAULT_PROFILE: Dict = {
    "id": DEFAULT_ID,
    "name": "Miguel (por defecto)",
    # --- Qué buscar ---
    "use_default_queries": True,     # True = usa las listas de consultas del radar (server.py)
    "queries": [],                   # consultas propias (si use_default_queries es False)
    "city": "Bogotá",
    "location_mode": "ambas",        # ambas | ciudad | remoto
    "portals": {"computrabajo": True, "elempleo": True, "indeed": True},
    "max_pages": 2,
    "max_age_days": filters.MAX_AGE_DAYS,
    # --- Cómo filtrar ---
    "use_builtin_rules": True,       # True = reglas del radar + lo que agregues abajo
    "roles": [],                     # palabras en el CARGO que lo hacen afín
    "exclude": [],                   # palabras en el CARGO que lo descartan
    "skills": [],                    # tecnologías principales (+12 c/u)
    "skills_plus": [],               # tecnologías secundarias (+5 c/u)
    "max_years_experience": filters.MAX_YEARS_EXPERIENCE,
    "exclude_senior": True,
    "min_score": 20,
}

_LIST_FIELDS = ("queries", "roles", "exclude", "skills", "skills_plus")
_ACCENTS = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uúü]", "n": "[nñ]"}


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def keyword_regex(word: str) -> str:
    """'desarrollador' -> patrón que acepta tildes y plurales/femenino ('desarrolladora', 'desarrolladores')."""
    base = _strip_accents(word.strip().lower())
    out = []
    for ch in base:
        if ch in _ACCENTS:
            out.append(_ACCENTS[ch])
        elif ch == " ":
            out.append(r"\s+")
        else:
            out.append(re.escape(ch))
    return r"\b" + "".join(out) + r"(?:es|s|a|as)?\b"


def _clean_list(value) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    seen, out = set(), []
    for item in value or []:
        item = str(item).strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _as_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def normalize(data: Dict) -> Dict:
    """Valida y completa un perfil que llega del formulario."""
    p = deepcopy(DEFAULT_PROFILE)
    p.update({k: v for k, v in (data or {}).items() if k in DEFAULT_PROFILE})
    p["id"] = str((data or {}).get("id") or "").strip() or uuid.uuid4().hex[:8]
    p["name"] = str(p.get("name") or "").strip()[:60] or "Perfil sin nombre"
    for f in _LIST_FIELDS:
        p[f] = _clean_list(p.get(f))
    p["city"] = str(p.get("city") or "Bogotá").strip()[:60]
    if p["location_mode"] not in ("ambas", "ciudad", "remoto"):
        p["location_mode"] = "ambas"
    portals = p.get("portals") or {}
    p["portals"] = {k: bool(portals.get(k, False)) for k in ("computrabajo", "elempleo", "indeed")}
    p["use_default_queries"] = bool(p.get("use_default_queries"))
    p["use_builtin_rules"] = bool(p.get("use_builtin_rules"))
    p["exclude_senior"] = bool(p.get("exclude_senior"))
    p["max_pages"] = _as_int(p.get("max_pages"), 2, 1, 6)
    p["max_age_days"] = _as_int(p.get("max_age_days"), filters.MAX_AGE_DAYS, 1, 30)
    p["max_years_experience"] = _as_int(p.get("max_years_experience"), filters.MAX_YEARS_EXPERIENCE, 0, 15)
    p["min_score"] = _as_int(p.get("min_score"), 20, 0, 100)
    if not p["use_default_queries"] and not p["queries"]:
        p["use_default_queries"] = True  # sin consultas propias se usan las del radar
    return p


# ---------------------------------------------------------------------------
# Almacenamiento
# ---------------------------------------------------------------------------

def _read() -> Dict:
    try:
        data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        profiles = [normalize(p) for p in data.get("profiles", [])]
    except Exception:
        data, profiles = {}, []
    if not any(p["id"] == DEFAULT_ID for p in profiles):
        profiles.insert(0, deepcopy(DEFAULT_PROFILE))
    active = data.get("active") if any(p["id"] == data.get("active") for p in profiles) else DEFAULT_ID
    return {"active": active, "profiles": profiles}


def _write(data: Dict) -> None:
    PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_profiles() -> Dict:
    return _read()


def get_profile(profile_id: str) -> Optional[Dict]:
    return next((p for p in _read()["profiles"] if p["id"] == profile_id), None)


def get_active() -> Dict:
    data = _read()
    return next(p for p in data["profiles"] if p["id"] == data["active"])


def save_profile(profile: Dict) -> Dict:
    p = normalize(profile)
    data = _read()
    data["profiles"] = [x for x in data["profiles"] if x["id"] != p["id"]] + [p]
    data["profiles"].sort(key=lambda x: (x["id"] != DEFAULT_ID, x["name"].lower()))
    _write(data)
    if p["id"] == data["active"]:
        apply_rules(p)
    return p


def delete_profile(profile_id: str) -> bool:
    if profile_id == DEFAULT_ID:
        raise ValueError("El perfil por defecto no se puede borrar (sí se puede editar).")
    data = _read()
    before = len(data["profiles"])
    data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
    if len(data["profiles"]) == before:
        return False
    if data["active"] == profile_id:
        data["active"] = DEFAULT_ID
        apply_rules(get_active_from(data))
    _write(data)
    return True


def get_active_from(data: Dict) -> Dict:
    return next(p for p in data["profiles"] if p["id"] == data["active"])


def set_active(profile_id: str) -> Dict:
    data = _read()
    if not any(p["id"] == profile_id for p in data["profiles"]):
        raise KeyError(profile_id)
    data["active"] = profile_id
    _write(data)
    p = get_active_from(data)
    apply_rules(p)
    return p


def reset_default() -> Dict:
    p = deepcopy(DEFAULT_PROFILE)
    return save_profile(p)


# ---------------------------------------------------------------------------
# Reglas de filtrado y consultas a partir del perfil
# ---------------------------------------------------------------------------

def build_rules(p: Dict) -> Dict:
    """Diccionario que entiende MatcherEngine (y el prefiltro de títulos de los extractores)."""
    from services.matcher import CANDIDATE_PROFILE
    roles = [keyword_regex(k) for k in p.get("roles", [])]
    exclude = [keyword_regex(k) for k in p.get("exclude", [])]
    skills = [s.lower() for s in p.get("skills", [])]
    plus = [s.lower() for s in p.get("skills_plus", [])]
    if p.get("use_builtin_rules", True):
        rules = deepcopy(CANDIDATE_PROFILE)
        rules["preferred_roles"] = list(CANDIDATE_PROFILE["preferred_roles"]) + roles
        rules["title_negative_patterns"] = list(CANDIDATE_PROFILE["title_negative_patterns"]) + exclude
        rules["primary_skills"] = list(CANDIDATE_PROFILE["primary_skills"]) + [s for s in skills if s not in CANDIDATE_PROFILE["primary_skills"]]
        rules["secondary_skills"] = list(CANDIDATE_PROFILE["secondary_skills"]) + [s for s in plus if s not in CANDIDATE_PROFILE["secondary_skills"]]
        rules["hard_tech_skills"] = list(CANDIDATE_PROFILE["hard_tech_skills"]) + [s for s in skills if s not in CANDIDATE_PROFILE["hard_tech_skills"]]
    else:
        rules = {
            "name": p.get("name"),
            "preferred_roles": roles,
            "title_negative_patterns": exclude,
            "body_negative_patterns": [],
            "primary_skills": skills,
            "secondary_skills": plus,
            "hard_tech_skills": skills,
        }
    rules["max_years_experience"] = p.get("max_years_experience", filters.MAX_YEARS_EXPERIENCE)
    rules["exclude_senior"] = p.get("exclude_senior", True)
    rules["min_score"] = p.get("min_score", 20)
    rules["max_age_days"] = p.get("max_age_days", filters.MAX_AGE_DAYS)
    rules["profile_id"] = p.get("id")
    return rules


def apply_rules(p: Dict) -> Dict:
    """Activa las reglas del perfil en el prefiltro de los extractores y en la ventana de frescura."""
    rules = build_rules(p)
    filters.set_active_rules(rules)
    return rules


def _city_slug(city: str) -> str:
    s = _strip_accents(city.lower()).strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return "bogota-dc" if s in ("bogota", "bogota-d-c", "bogota-dc") else (s or "bogota-dc")


def _indeed_city(city: str) -> str:
    return "Bogotá, D.C." if _city_slug(city) == "bogota-dc" else city


def search_targets(p: Dict) -> Optional[Dict[str, List[Dict]]]:
    """
    Consultas por portal para el escaneo completo. None = usar las listas del radar.
    elempleo solo filtra por Bogotá; con otra ciudad allí se busca únicamente remoto.
    """
    if p.get("use_default_queries", True) or not p.get("queries"):
        return None
    mode = p.get("location_mode", "ambas")
    pages = p.get("max_pages", 2)
    slug = _city_slug(p.get("city", "Bogotá"))
    ct, ee, ind = [], [], []
    for q in p["queries"]:
        if mode in ("ambas", "ciudad"):
            ct.append({"query": q, "location": slug, "max_pages": pages})
            ind.append({"query": q, "location": _indeed_city(p.get("city", "Bogotá")), "max_pages": pages})
            if slug == "bogota-dc":
                ee.append({"query": q, "max_pages": pages})
        if mode in ("ambas", "remoto"):
            ct.append({"query": q, "location": "remoto", "max_pages": max(1, pages - 1)})
            ind.append({"query": q, "location": "Colombia", "max_pages": 1, "remote": True})
            ee.append({"query": q, "max_pages": 1, "remote": True})
    return {"computrabajo": ct, "elempleo": ee, "indeed": ind}


def alert_targets(p: Dict) -> Optional[Dict[str, List[Dict]]]:
    """Consultas del modo alerta (página 1). None = listas del radar."""
    t = search_targets(p)
    if t is None:
        return None
    return {
        "computrabajo": [{**x, "max_pages": 1} for x in t["computrabajo"]],
        "elempleo": [{**x, "max_pages": 1} for x in t["elempleo"]],
    }
