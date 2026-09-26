"""
"Qué me falta aprender": cuenta qué tecnologías piden las vacantes vistas por el radar
y las separa entre las que ya tengo respaldadas y las que me faltan.

MY_SKILLS sale del Perfil Maestro del vault (00-personal/perfil/PERFIL-MAESTRO.md, sección 5:
solo tecnologías con proyecto real detrás). Si el perfil cambia, se actualiza aquí.
"""
import json
import re
from typing import Dict, Iterable, List, Optional

# Nombre canónico -> patrones (regex sobre texto en minúsculas)
TECH_CATALOG: Dict[str, List[str]] = {
    # Lenguajes
    "Python": [r"\bpython\b"], "JavaScript": [r"\bjavascript\b", r"\bjs\b"],
    "TypeScript": [r"\btypescript\b"], "Java": [r"\bjava\b(?!\s*script)"],
    "C#": [r"\bc#", r"\bc\s*sharp\b"], ".NET": [r"\.net\b", r"\bdotnet\b", r"\basp\.?net\b"],
    "PHP": [r"\bphp\b"], "Go": [r"\bgolang\b"], "Kotlin": [r"\bkotlin\b"], "Swift": [r"\bswift\b"],
    "R": [r"\blenguaje r\b", r"\br studio\b", r"\brstudio\b"], "Scala": [r"\bscala\b"],
    "SQL": [r"\bsql\b"], "Bash / Shell": [r"\bbash\b", r"\bshell\b", r"\bpowershell\b"],
    # Frontend
    "React": [r"\breact(?:\.?js)?\b(?!\s*native)"], "Angular": [r"\bangular\b"], "Vue": [r"\bvue(?:\.?js)?\b"],
    "Next.js": [r"\bnext\.?js\b"], "HTML/CSS": [r"\bhtml5?\b", r"\bcss3?\b"],
    "Tailwind": [r"\btailwind\b"], "Bootstrap": [r"\bbootstrap\b"],
    # Móvil
    "React Native": [r"\breact\s*native\b"], "Flutter": [r"\bflutter\b"], "Android": [r"\bandroid\b"],
    # Backend / frameworks
    "Node.js": [r"\bnode(?:\.?js)?\b"], "Express": [r"\bexpress(?:\.?js)?\b"], "FastAPI": [r"\bfastapi\b"],
    "Django": [r"\bdjango\b"], "Flask": [r"\bflask\b"], "Spring Boot": [r"\bspring(?:\s*boot)?\b"],
    "Laravel": [r"\blaravel\b"], "NestJS": [r"\bnest\.?js\b"],
    "APIs REST": [r"\bapi\s*rest", r"\brest(?:ful)?\s*api", r"\bapis?\s+rest"], "GraphQL": [r"\bgraphql\b"],
    "Microservicios": [r"\bmicroservicios?\b", r"\bmicroservices?\b"],
    # Datos
    "PostgreSQL": [r"\bpostgres(?:ql)?\b"], "MySQL": [r"\bmysql\b"], "SQL Server": [r"\bsql\s*server\b"],
    "Oracle": [r"\boracle\b", r"\bpl/?sql\b"], "MongoDB": [r"\bmongo(?:db)?\b"], "Redis": [r"\bredis\b"],
    "Power BI": [r"\bpower\s*bi\b", r"\bpowerbi\b"], "DAX": [r"\bdax\b"], "Tableau": [r"\btableau\b"],
    "Excel avanzado": [r"\bexcel\b"], "ETL": [r"\betl\b", r"\belt\b"], "pandas": [r"\bpandas\b"],
    "Spark": [r"\bspark\b", r"\bpyspark\b"], "Databricks": [r"\bdatabricks\b"], "Snowflake": [r"\bsnowflake\b"],
    "Airflow": [r"\bairflow\b"], "Machine Learning": [r"\bmachine\s*learning\b", r"\baprendizaje autom"],
    "IA generativa / LLM": [r"\bllms?\b", r"\bia generativa\b", r"\bgenerative ai\b", r"\bopenai\b", r"\bgpt\b", r"\brag\b"],
    # Cloud / DevOps
    "Azure": [r"\bazure\b"], "AWS": [r"\baws\b", r"\bamazon web services\b"], "GCP": [r"\bgcp\b", r"\bgoogle cloud\b"],
    "Docker": [r"\bdocker\b"], "Kubernetes": [r"\bkubernetes\b", r"\bk8s\b"], "Git": [r"\bgit\b", r"\bgithub\b", r"\bgitlab\b"],
    "CI/CD": [r"\bci\s*/\s*cd\b", r"\bjenkins\b", r"\bgithub actions\b", r"\bazure devops\b"],
    "Linux": [r"\blinux\b"], "Terraform": [r"\bterraform\b"],
    # Calidad
    "Pruebas automatizadas": [r"\bpytest\b", r"\bjest\b", r"\bselenium\b", r"\bcypress\b", r"\bpruebas unitarias\b", r"\bunit test"],
    # Plataformas
    "SAP": [r"\bsap\b"], "Salesforce": [r"\bsalesforce\b"], "Odoo": [r"\bodoo\b"], "WordPress": [r"\bwordpress\b"],
    "Power Platform": [r"\bpower\s*apps\b", r"\bpower\s*automate\b"],
    # Metodología
    "Scrum / Ágil": [r"\bscrum\b", r"\bágil\b", r"\bagile\b", r"\bkanban\b"], "Jira": [r"\bjira\b"],
    # Idioma
    "Inglés B2+": [r"\bingl[eé]s\s*(?:b2|c1|avanzado|intermedio[\s-]*alto)\b", r"\b(?:b2|c1)\s*(?:de\s*)?ingl[eé]s\b",
                   r"\benglish\s*(?:b2|c1|advanced|fluent)\b", r"\bbiling[uü]e\b"],
}

# Del Perfil Maestro (sección 5). Nivel: avanzado / intermedio / básico.
MY_SKILLS: Dict[str, str] = {
    "Python": "avanzado", "TypeScript": "avanzado", "React": "avanzado", "Next.js": "avanzado",
    "FastAPI": "avanzado", "pandas": "avanzado", "Docker": "avanzado", "Git": "avanzado", "Azure": "avanzado",
    "JavaScript": "intermedio", "SQL": "intermedio", "Tailwind": "intermedio", "Node.js": "intermedio",
    "PostgreSQL": "intermedio", "IA generativa / LLM": "intermedio",
    "R": "básico", "Power BI": "básico", "Machine Learning": "básico",
    "HTML/CSS": "intermedio", "ETL": "intermedio", "APIs REST": "intermedio", "CI/CD": "básico",
    "Pruebas automatizadas": "intermedio",
}
# El perfil declara inglés B1: lo que pida B2+ cuenta como brecha.


def detect(text: str) -> List[str]:
    t = (text or "").lower()
    return [name for name, pats in TECH_CATALOG.items() if any(re.search(p, t) for p in pats)]


def analyze(offers: Iterable, my_skills: Optional[Dict[str, str]] = None) -> dict:
    """offers: objetos con title/description/requirements. Retorna ranking de demanda."""
    my = my_skills if my_skills is not None else MY_SKILLS
    counts: Dict[str, int] = {}
    examples: Dict[str, List[str]] = {}
    n = 0
    for o in offers:
        reqs = o.requirements if isinstance(o.requirements, list) else json.loads(o.requirements or "[]")
        found = set(detect(f"{o.title} {o.description} {' '.join(reqs)}"))
        if not found:
            continue
        n += 1
        for name in found:
            counts[name] = counts.get(name, 0) + 1
            if len(examples.setdefault(name, [])) < 3:
                examples[name].append(o.title)
    rows = []
    for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        rows.append({
            "skill": name, "count": c, "pct": round(100.0 * c / n, 1) if n else 0.0,
            "have": name in my, "level": my.get(name), "examples": examples.get(name, []),
        })
    gaps = [r for r in rows if not r["have"]]
    weak = [r for r in rows if r["have"] and r["level"] == "básico"]
    return {"vacancies_analyzed": n, "skills": rows, "top_gaps": gaps[:10], "reinforce": weak[:5]}
