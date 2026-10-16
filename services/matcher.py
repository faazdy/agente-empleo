import re
from typing import Dict, List, Tuple
from core.models import JobOffer
from services.filters import (
    BODY_NEGATIVE_PATTERNS, HARD_TECH_SKILLS, MAX_YEARS_EXPERIENCE, PREFERRED_ROLES,
    SEMI_SENIOR_PATTERN, TITLE_NEGATIVE_PATTERNS, is_senior_title, required_years,
)

# Perfil técnico — Miguel David Vásquez Leyton
# Regla de diseño: PRECISIÓN sobre COBERTURA.
# Es mejor perder una vacante borderline que mostrar basura.
CANDIDATE_PROFILE = {
    "name": "Miguel David Vásquez Leyton",
    "title": "Ingeniero de Sistemas · Desarrollador Full Stack & Datos",

    "primary_skills": [
        "python", "fastapi", "flask", "django", "react", "next.js", "typescript",
        "javascript", "node.js", "nodejs", "sql", "postgresql", "postgres", "sqlite",
        "tailwind", "docker", "git", "rest api", "api rest", "html", "css",
        "power bi", "powerbi", "excel avanzado", "etl", "dashboards", "indicadores", "kpis",
    ],

    "secondary_skills": [
        "pandas", "polars", "supabase", "prisma", "graphql", "websockets",
        "azure", "aws", "linux", "ai", "llm", "scraping", "beautifulsoup", "playwright",
        "inteligencia artificial", "automatización", "tableau", "c#", "dax", "power query",
        "modelado de datos", "análisis de datos", "analisis de datos",
        "business intelligence", "bi",
    ],

    # Listas compartidas con los extractores (services/filters.py): una sola fuente de verdad.
    "preferred_roles": PREFERRED_ROLES,

    "hard_tech_skills": HARD_TECH_SKILLS,
    "title_negative_patterns": TITLE_NEGATIVE_PATTERNS,
    "body_negative_patterns": BODY_NEGATIVE_PATTERNS,
    # Perfil Jr: 6 meses de experiencia real. Más de esto → descarte.
    "max_years_experience": MAX_YEARS_EXPERIENCE,
}


class MatcherEngine:
    """
    Motor de evaluación calibrado para Miguel Vásquez (Ingeniero de Sistemas / Full Stack & Datos).

    Capas de decisión:
    1. Descarte estricto en título (patrones negativos).
    2. Descarte estricto en cuerpo (contratos aprendizaje, telemarketing).
    3. Verificación de afinidad de rol en el título (preferred_roles → +35 base).
    4. Fallback: si el título no es tech pero la descripción tiene stack duro (hard_tech_skills → +15 base).
    5. Si ni 3 ni 4 → descartada.
    6. Puntuación por stack primario (+12c/u, tope 45), secundario (+5c/u, tope 15).
    7. Bonus Jr/Mid (+15).
    8. Nivel: títulos Senior/Líder/Arquitecto/Jefe se DESCARTAN; Semi Senior -10.
    9. Experiencia exigida (descripción + requisitos): > max_years_experience → descarte;
       exactamente el máximo → -10.
    10. Threshold matched: ≥ 20.
    """

    def __init__(self, profile: Dict = CANDIDATE_PROFILE):
        self.profile = profile

    def evaluate(self, offer: JobOffer) -> Tuple[float, str, str]:
        reqs = " ".join(offer.requirements or [])
        text_corpus = f"{offer.title} {offer.description} {reqs} {offer.salary or ''} {offer.location or ''}".lower()
        title_lower = offer.title.lower()

        # 1. Descarte estricto en título
        for pat in self.profile.get("title_negative_patterns", []):
            if re.search(pat, title_lower):
                clean = pat.replace(r"\b", "").replace("[aá]", "a").replace("[eé]", "e") \
                           .replace("[ií]", "i").replace("[oó]", "o")
                return 0.0, f"Descartada: Filtro excluyente en título ('{clean}')", "discarded"

        # 1b. Nivel: cargos senior / de liderazgo no corresponden a un primer empleo
        if self.profile.get("exclude_senior", True) and is_senior_title(title_lower):
            return 0.0, "Descartada: Nivel Senior/Líder en el título (perfil Jr)", "discarded"

        # 1c. Años de experiencia exigidos
        years = required_years(f"{offer.description} {reqs}")
        max_years = self.profile.get("max_years_experience", MAX_YEARS_EXPERIENCE)
        if years is not None and years > max_years:
            return 0.0, f"Descartada: Pide {years} años de experiencia (perfil Jr, máx. {max_years})", "discarded"

        # 2. Descarte estricto en cuerpo
        for pat in self.profile.get("body_negative_patterns", []):
            if re.search(pat, text_corpus):
                clean = pat.replace(r"\b", "")
                return 0.0, f"Descartada: Filtro excluyente en cuerpo ('{clean}')", "discarded"

        score = 0.0
        reasons: List[str] = []

        # 3. Afinidad de rol en el título
        has_role = any(re.search(pat, title_lower) for pat in self.profile.get("preferred_roles", []))

        # 4. Hard tech skills en la descripción (fallback)
        hard_hits = [
            skill for skill in self.profile.get("hard_tech_skills", [])
            if re.search(r"\b" + re.escape(skill) + r"\b", text_corpus)
        ]

        # 5. Si no hay nada → descartar (salvo perfiles sin cargos ni tecnologías definidos)
        no_criteria = not self.profile.get("preferred_roles") and not self.profile.get("hard_tech_skills")
        if not has_role and not hard_hits and not no_criteria:
            return 0.0, "Descartada: Sin afinidad de cargo ni stack técnico", "discarded"

        if no_criteria:
            score += 35.0
            reasons.append("Perfil sin filtro de cargo")
        elif has_role:
            score += 35.0
            reasons.append("Rol Tech afín")
        else:
            score += 15.0
            reasons.append("Cargo alternativo con stack afín")

        # 6. Stack primario (hasta 45 puntos)
        primary_hits = [
            skill for skill in self.profile["primary_skills"]
            if re.search(r"\b" + re.escape(skill) + r"\b", text_corpus)
        ]
        if primary_hits:
            score += min(45.0, len(primary_hits) * 12.0)
            reasons.append(f"Stack: {', '.join(primary_hits[:3])}")

        # 7. Skills secundarias / IA / Datos (hasta 15 puntos)
        sec_hits = [
            skill for skill in self.profile["secondary_skills"]
            if re.search(r"\b" + re.escape(skill) + r"\b", text_corpus)
        ]
        if sec_hits:
            score += min(15.0, len(sec_hits) * 5.0)
            reasons.append(f"Plus: {', '.join(sec_hits[:2])}")

        # 8. Bonus Junior / Mid (+15)
        if any(
            re.search(r"\b" + re.escape(j) + r"\b", title_lower) or
            re.search(r"\b" + re.escape(j) + r"\b", text_corpus)
            for j in ["junior", "jr", "entry", "sin experiencia", "mid"]
        ):
            score += 15.0
            reasons.append("Nivel Jr/Mid")

        # 9. Penalizaciones de nivel (no descartan)
        if SEMI_SENIOR_PATTERN.search(title_lower):
            score = max(15.0, score - 10.0)
            reasons.append("Semi Senior (-10)")
        if years is not None and years >= 2:
            score = max(15.0, score - 10.0)
            reasons.append(f"Pide {years} años (-10)")
        elif years is not None:
            reasons.append(f"Pide {years} año(s)")

        score = min(100.0, round(score, 1))
        min_score = float(self.profile.get("min_score", 20.0))
        if score < min_score:
            reasons.append(f"Bajo el mínimo del perfil ({min_score:g})")
        status = "matched" if score >= min_score else "discarded"
        return score, " | ".join(reasons), status
