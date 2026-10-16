"""
Reglas de filtrado ÚNICAS, compartidas por los extractores (prefiltro de título,
antes de gastar peticiones de detalle) y por el matcher (evaluación final).
Antes había dos listas de exclusión distintas que se desalinearon.
"""
import re
from typing import Optional

# Máximo de años de experiencia que el perfil puede cubrir (hasta 3 años tolerado con penalización).
MAX_YEARS_EXPERIENCE = 3

# Ventana de frescura: hasta 4 días para cubrir cómodamente fines de semana (viernes a lunes).
# Coincide con pubdate=7 de Computrabajo y fromage=7 de Indeed.
MAX_AGE_DAYS = 4

# Ventana propia de elempleo.com: 4 días hacia atrás (primer barrido del portal, aún sin
# postulaciones allí). Se puede subir o bajar sin afectar a Computrabajo ni a Indeed.
ELEMPLEO_MAX_AGE_DAYS = 4


def posted_age_days(posted_str) -> Optional[float]:
    """Antigüedad en días de textos tipo 'Hace 3 horas', 'Ayer', 'Hace 2 días'. None si no se reconoce."""
    if not posted_str:
        return None
    s = str(posted_str).lower().strip()
    if "minuto" in s or "hora" in s or "hoy" in s or "recién" in s or "recien" in s or "justo ahora" in s:
        return 0.0
    if "ayer" in s:
        return 1.0
    m = re.search(r"(\d+)\s*d[ií]as?", s)
    if m:
        return float(m.group(1))
    # elempleo: "Hace 1 semana", "Hace 2 semanas"
    m = re.search(r"(\d+)\s*semanas?", s)
    if m:
        return float(m.group(1)) * 7
    if "semana" in s:
        return 7.0
    if "más de 30" in s or "mas de 30" in s or "mes" in s:
        return 31.0
    return None


def is_fresh(posted_str, max_days: int = MAX_AGE_DAYS) -> bool:
    """True si la vacante está dentro de la ventana. Si la fecha no se reconoce, se admite (no perder)."""
    age = posted_age_days(posted_str)
    return age is None or age <= max_days

_NUM_WORDS = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
}
_NUM = r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")"

_EXPERIENCE_PATTERNS = [
    # "3 años de experiencia", "3+ años de experiencia", "3 a 5 años de experiencia", "tres (3) años"
    re.compile(_NUM + r"\s*(?:\(\s*\d{1,2}\s*\))?\s*\+?\s*(?:(?:a|-|–|o)\s*\d{1,2}\s*)?(?:o\s+m[aá]s\s+)?a[ñn]os?\s+(?:o\s+m[aá]s\s+)?(?:de\s+|en\s+)?(?:experiencia|exp\b|trayectoria)"),
    # "experiencia mínima de 3 años", "experiencia de 2 años", "experiencia: 4 años"
    re.compile(r"experiencia\s*(?:laboral\s*|profesional\s*|certificada\s*|comprobada\s*|relacionada\s*)?(?:m[ií]nima\s*)?(?:de\s*|:\s*|superior\s+a\s*|mayor\s+a\s*)?(?:al\s+menos\s*|m[ií]nimo\s*)?" + _NUM + r"\s*(?:\(\s*\d{1,2}\s*\)\s*)?\+?\s*a[ñn]os?"),
    # "mínimo 3 años", "al menos 3 años"
    re.compile(r"(?:m[ií]nimo|al\s+menos|m[aá]s\s+de)\s+(?:de\s+)?" + _NUM + r"\s*(?:\(\s*\d{1,2}\s*\)\s*)?\+?\s*a[ñn]os?"),
    # inglés: "3+ years of experience"
    re.compile(_NUM + r"\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?\s+(?:of\s+)?(?:\w+\s+)?experience"),
]


def _to_int(tok: str) -> Optional[int]:
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


def required_years(text: str) -> Optional[int]:
    """
    Años mínimos de experiencia que exige el texto. Si hay varias menciones
    toma la mayor (p. ej. '1 año en SQL y 3 años de experiencia en desarrollo' → 3).
    Ignora valores > 15 (suelen ser fechas o cifras de la empresa).
    """
    if not text:
        return None
    t = text.lower()
    found = []
    for pat in _EXPERIENCE_PATTERNS:
        for m in pat.finditer(t):
            n = _to_int(m.group(1))
            if n is not None and 0 < n <= 15:
                found.append(n)
    return max(found) if found else None


# Títulos de nivel que no corresponden a un primer cargo (descarte).
SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|lead|l[ií]der|arquitect[oa]|director[a]?|gerente|jefe|head|principal|staff|expert[oa]|especialista\s+senior)\b"
)
# Semi senior: se admite con penalización (suele pedir 2-3 años).
SEMI_SENIOR_PATTERN = re.compile(r"\b(?:semi[\s-]?senior|ssr|semisenior)\b")

# Descarte estricto por TÍTULO.
TITLE_NEGATIVE_PATTERNS = [
    # Prácticas / SENA / Aprendizaje
    r"\bpr[aá]cticas?\b", r"\bpracticantes?\b", r"\baprendi[zc]es?\b", r"\baprendiz\b", r"\bsena\b",
    r"\betapa productiva\b", r"\bpasant[ií]as?\b", r"\bpasantes?\b", r"\bconvenio sena\b",
    r"\bcontrato de aprendizaje\b", r"\bjudicatura\b", r"\bestudiantes?\b",
    # Ventas, Comercial, Call Center, Cobranzas
    r"\bcomercial\b", r"\bventas\b", r"\bvendedor(?:es|as?)?\b", r"\bcall center\b",
    r"\bcobranzas?\b", r"\btelemercadeo\b", r"\bdesarrollador[a]? de negocios\b",
    r"\bbusiness development\b", r"\basesor(?:es|as?)?\b", r"\bback\s*office\b",
    # Programadores de logística / manufactura / PLC (NO son dev de software)
    r"\bprogramador(?:a)? de rutas\b", r"\bprogramador(?:a)? de producci[oó]n\b",
    r"\bprogramador(?:a)? de turnos\b", r"\bprogramador(?:a)? de mantenimiento\b",
    r"\bprogramador(?:a)? de corte\b", r"\bprogramador cnc\b",
    r"\bprogramador(?:a)? de despacho\b", r"\bprogramador(?:a)? de transporte\b",
    r"\bplc\b", r"\bhmi\b", r"\bscada\b",
    r"\bprogramador(?:a)? de (?:cirug[ií]as?|agendas?|citas|malla|horarios?|pedidos|compras|medios|pauta)\b",
    r"\bpromotor(?:es|as?)?\b", r"\bejecutiv[oa]s?\s+(?:comercial|de\s+ventas|de\s+cuenta)\b",
    # Roles clínicos / salud
    r"\bm[eé]dicos?\b", r"\benfermer[oa]s?\b", r"\bodont[oó]logos?\b",
    r"\bfisicoqu[ií]micos?\b", r"\bfarmac[eé]uticos?\b",
    # Roles físicos / operativos / obra civil / manufactura
    r"\bcocineros?\b", r"\bvigilantes?\b", r"\bvigilancia\b", r"\bseguridad\s+privada\b",
    r"\bcajas?\b", r"\bcajer[oa]s?\b",
    r"\bdomiciliarios?\b", r"\bcapacitador(?:es|as?)?\b", r"\boperador(?:es|as?)?\b",
    r"\boperari[oa]s?\b", r"\bconductor(?:es|as?)?\b", r"\bmotorizad[oa]s?\b",
    r"\bm[aá]quinas?\s+cnc\b", r"\bcnc\b", r"\boperaciones\s+a[eé]reas?\b",
    r"\binstalador(?:es|as?)?\b", r"\bmantenimiento locativo\b", r"\bcontraincendios?\b",
    r"\btaller\b", r"\bautomotriz\b", r"\brefrigeraci[oó]n\b", r"\bhvac\b",
    r"\bh[uú]medas?\b", r"\bobra\b", r"\bplomer[ií]a\b", r"\btuber[ií]a\b", r"\boficial\b",
    # Administrativos / contables / gestión industrial / ISO
    r"\bcontable\b", r"\bcontabilidad\b",
    r"\bsistemas?\s+de\s+gesti[oó]n\b",
    r"\bgesti[oó]n\s+integrada\b", r"\bsistema\s+integrado\s+de\s+gesti[oó]n\b",
    r"\bsig\b", r"\bhseq\b", r"\bsst\b",
    r"\bevaluaciones?\s+econ[oó]micas?\b",
    # Call-center o digitación disfrazados de tecnología
    r"\bdigitador(?:es|as?)?\b",
    r"\batenci[oó]n al cliente\b",
    r"\bservicio al cliente\b",
    r"\brecepcionista\b",
    r"\bauxiliar administrativo\b",
    r"\bagente\s+de\s+soporte\b",
    r"\bcoordinador\s+(?:de\s+)?(?:ventas|comercial|log[ií]stica|distribuci[oó]n)\b",
    # Roles legales / jurídicos
    r"\babogad[oa]s?\b", r"\bjur[ií]dic[oa]s?\b", r"\blegal\b",
    # Marketing digital / redes / contenido
    r"\bredes\s+sociales\b", r"\bcommunity\s+manager\b", r"\bcreaci[oó]n\s+de\s+contenido\b",
]

BODY_NEGATIVE_PATTERNS = [
    r"\bcontrato de aprendizaje\b", r"\bconvenio sena\b", r"\betapa productiva\b",
    r"\bpasant[ií]a obligatoria\b", r"\bexclusivo para practicantes\b",
    r"\btelemercadeo\b", r"\bcobranza telef[oó]nica\b",
]

# Roles afines en el TÍTULO (dan +35 en el matcher).
PREFERRED_ROLES = [
    r"desarrollad", r"programad", r"software",
    r"full\s*stack", r"fullstack", r"backend", r"back[\s-]end", r"frontend", r"front[\s-]end",
    r"web\s*developer", r"developer",
    # Roles junior / trainee / analista desarrollador
    r"\btrainee\b", r"\bdesarrollador\s+trainee\b", r"\btrainee\s+(?:de\s+)?desarrollo\b",
    r"analista\s+desarrollad",
    # Ingeniería formal (Ing., Ing, Ingeniero/a, de/en sistemas/software/datos)
    r"\b(?:ingenier[oa]|ing)[.,]?\s*(?:.*?\s+)?(?:de\s+|en\s+)?sistemas?",
    r"\b(?:ingenier[oa]|ing)[.,]?\s*(?:.*?\s+)?(?:de\s+|en\s+)?software",
    r"\b(?:ingenier[oa]|ing)[.,]?\s*(?:.*?\s+)?(?:de\s+|en\s+)?datos",
    # Datos / BI / Analytics
    r"analista\s+(?:de\s+)?datos", r"data\s*analyst", r"data\s*engineer", r"data\s*scientist",
    r"cient[ií]fico\s+(?:de\s+)?datos", r"machine\s*learning", r"analista\s+bi\b",
    r"data\s*analytics", r"business\s+intelligence", r"transformaci[oó]n\s+de\s+data",
    r"analista\s+(?:de\s+)?informaci[oó]n",
    # IA
    r"ai\s*engineer", r"ai\s*ops", r"inteligencia\s*artificial",
    # QA / Testing / Calidad de software
    r"\bqa\b", r"tester\b", r"automatizaci[oó]n\s+de\s+pruebas",
    r"analista\s+(?:de\s+)?qa\b", r"ingenier[oa]\s+(?:de\s+)?pruebas\b", r"qa\s*tester\b",
    # Soporte TI y Sistemas (cargos técnicos afines a Ing. de Sistemas)
    r"soporte\s+ti\b",
    r"soporte\s+t[eé]cnico\s+(?:n[12]|l[12]|nivel|junior|senior|especialista|sistemas)?\s*$",
    r"analista\s+(?:de\s+)?sistemas?\b",
    r"t[eé]cnico\s+(?:in\s+house\s+)?ti\b", r"t[eé]cnico\s+(?:de\s+)?sistemas\b",
    r"auxiliar\s+de\s+sistemas\b", r"auxiliar\s+de\s+ti\b",
    # Infraestructura / DevOps / Cloud / Redes TI
    r"infraestructura\b", r"devops\b", r"ciberseguridad\b", r"cloud\b",
    r"redes\s+(?:y\s+seguridad|de\s+datos|cisco|ip|lan|wan|ti|inform[aá]ticas)\b",
    r"administrador\s+de\s+redes\b",
    r"administrador(?:/a|\(a\)|a)?\s+de\s+sistemas\b", r"\bmiddleware\b",
]

# Tecnologías fuertes: si aparecen en el título también justifican pedir el detalle.
HARD_TECH_SKILLS = [
    "python", "sql", "react", "fastapi", "django", "flask", "node", "nodejs",
    "javascript", "typescript", "power bi", "powerbi", "docker", "git", "tableau",
    "aws", "azure", "pandas", "etl", "html", "css", "tailwind", "postgresql", "postgres",
    "inteligencia artificial", "machine learning", "playwright", "scraping",
]


def first_negative(title: str) -> Optional[str]:
    t = title.lower()
    for pat in TITLE_NEGATIVE_PATTERNS:
        if re.search(pat, t):
            return pat
    return None


def has_preferred_role(title: str) -> bool:
    t = title.lower()
    return any(re.search(p, t) for p in PREFERRED_ROLES)


def is_senior_title(title: str) -> bool:
    t = title.lower()
    if SEMI_SENIOR_PATTERN.search(t):
        return False
    return bool(SENIOR_TITLE_PATTERN.search(t))


def is_promising_title(title: str) -> bool:
    """
    Prefiltro barato (sin petición de detalle). Usa exactamente las mismas reglas
    que el matcher para no gastar peticiones en vacantes que luego se descartan.
    """
    t = title.lower()
    if first_negative(t) or is_senior_title(t):
        return False
    if has_preferred_role(t):
        return True
    return any(re.search(r"\b" + re.escape(s) + r"\b", t) for s in HARD_TECH_SKILLS)
