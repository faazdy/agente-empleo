import re
from typing import Dict, List, Optional

QA_ENTRIES: List[Dict] = [
    {
        "id": "contacto",
        "category": "Contacto y Datos Personales",
        "patterns": [
            r"contacto", r"tel[eé]fono", r"whatsapp", r"celular", r"n[uú]mero", r"correo", r"email"
        ],
        "answer": (
            "Número de contacto y WhatsApp: +57 300 305 9463\n"
            "Correo profesional: miguxld.corp@gmail.com\n"
            "Ubicación: Bogotá, D.C.\n"
            "Portafolio: https://miguxld.site\n"
            "Disponibilidad inmediata para contacto telefónico o entrevistas virtuales."
        ),
    },
    {
        "id": "salario",
        "category": "Aspiración Salarial",
        "patterns": [
            r"aspiraci[oó]n", r"salarial", r"salario", r"pretensi[oó]n", r"cu[aá]nto aspiras", r"remuneraci[oó]n"
        ],
        "answer": (
            "A convenir. Mi interés principal es sumarme a un proyecto donde pueda aportar valor analítico y continuar "
            "desarrollándome profesionalmente. Mi expectativa es flexible y abierta a evaluación según la escala salarial "
            "de la institución, las responsabilidades del cargo y el paquete de beneficios."
        ),
    },
    {
        "id": "formacion",
        "category": "Formación Académica",
        "patterns": [
            r"formaci[oó]n", r"acad[eé]mica", r"estudios", r"carrera", r"t[ií]tulo", r"universidad", r"grado", r"semestre"
        ],
        "answer": (
            "Estudiante de décimo semestre (culminando materias) de Ingeniería de Sistemas en la Corporación "
            "Universitaria Minuto de Dios (UNIMINUTO), Bogotá. Cuento con formación técnica enfocada en gestión y "
            "modelado de bases de datos relacionales, analítica con Python/SQL, arquitectura de software y metodologías ágiles."
        ),
    },
    {
        "id": "experiencia_general",
        "category": "Experiencia en el Cargo / Tiempo",
        "patterns": [
            r"tiempo de experiencia", r"a[ñn]os de experiencia", r"experiencia tiene en el cargo", r"experiencia laboral", r"cu[aá]nto tiempo"
        ],
        "answer": (
            "Cuento con más de 1 año de experiencia práctica en desarrollo y analítica de datos, incluyendo 6 meses en "
            "la unidad de innovación SymbioTIC. He implementado pipelines de ingeniería de datos procesando millones de "
            "registros (ETL, scraping oficial, modelado en SQL Server y PostgreSQL) y construcción de tableros de indicadores "
            "estratégicos para la toma de decisiones institucionales."
        ),
    },
    {
        "id": "excel",
        "category": "Manejo de Excel",
        "patterns": [
            r"excel", r"hojas? de c[aá]lculo", r"tablas din[aá]micas", r"buscarv", r"vlookup", r"xlookup"
        ],
        "answer": (
            "Nivel intermedio-avanzado. Manejo con soltura fórmulas lógicas y de búsqueda (BUSCARV, XLOOKUP, SI), tablas "
            "dinámicas, segmentación, validación de datos y consolidación de fuentes. Además, integro hojas de cálculo "
            "con scripts de Python para automatizar la limpieza y generación de reportes estructurados sin errores manuales."
        ),
    },
    {
        "id": "python_datos",
        "category": "Python para Análisis / Datos",
        "patterns": [
            r"python.*datos", r"python.*procesamiento", r"pandas", r"polars", r"anal[ií]tica.*python", r"python"
        ],
        "answer": (
            "Sí, amplia experiencia. Python es mi herramienta central de analítica utilizando Pandas, Polars y DuckDB. "
            "He diseñado e implementado pipelines ETL para extraer información desde APIs y bases de datos, depurar "
            "inconsistencias, calcular métricas institucionales y estructurar datos para la toma de decisiones estratégicas."
        ),
    },
    {
        "id": "sql_bases_datos",
        "category": "SQL y Gestión de Bases de Datos",
        "patterns": [
            r"sql", r"bases? de datos", r"postgresql", r"sql server", r"sqlite", r"consultas", r"dimensional"
        ],
        "answer": (
            "Sí, sólida experiencia práctica con SQL Server, PostgreSQL y SQLite. Domino el diseño y optimización de "
            "consultas complejas (JOINs, funciones de agregación, subconsultas, CTEs y filtros avanzados), modelado relacional, "
            "normalización y estructuración de esquemas dimensionales para alimentar reportes e indicadores con total integridad."
        ),
    },
    {
        "id": "power_bi_dashboards",
        "category": "Power BI y Visualización / Dashboards",
        "patterns": [
            r"power bi", r"powerbi", r"dashboard", r"tableros?", r"visualizaci[oó]n", r"dax", r"power query", r"reporting", r"indicadores"
        ],
        "answer": (
            "Sí. Utilizo Power BI para modelado de datos, creación de medidas DAX y tableros interactivos orientados a "
            "seguimiento de KPIs. Asimismo, tengo amplia experiencia construyendo dashboards analíticos dinámicos y "
            "reportes ejecutivos con librerías de visualización como Plotly y Recharts para comités directivos."
        ),
    },
    {
        "id": "etl_calidad_git",
        "category": "ETL/ELT, Calidad de Datos y Git",
        "patterns": [
            r"etl", r"elt", r"calidad de datos", r"conciliaci[oó]n", r"git", r"control de cambios", r"pipelines?"
        ],
        "answer": (
            "Sí, experiencia comprobada en todo el ciclo:\n"
            "- ETL/ELT: Pipelines automatizados con deduplicación y reglas de ingesta programadas.\n"
            "- Calidad y conciliación: Auditoría de consistencia de fuentes heterogéneas y validación estricta de esquemas.\n"
            "- Visualización: Dashboards interactivos y reportes ejecutivos en Power BI y entornos web.\n"
            "- Git: Control de versiones con conventional commits y documentación estructurada."
        ),
    },
    {
        "id": "experiencia_analisis",
        "category": "Experiencia en Análisis de Información",
        "patterns": [
            r"an[aá]lisis de informaci[oó]n", r"analista de datos", r"an[aá]lisis de datos"
        ],
        "answer": (
            "Cuento con experiencia en el diseño y ejecución de flujos de análisis de información de punta a punta. He "
            "trabajado en la extracción, depuración y estructuración de datos de fuentes heterogéneas (APIs, bases de datos "
            "y datos abiertos) con Python (Pandas, Polars y SQL). Transformo datos crudos en métricas de negocio mediante "
            "tableros interactivos y modelos de scoring garantizando consistencia y trazabilidad."
        ),
    },
    {
        "id": "sector_educativo",
        "category": "Experiencia en Sector Educativo",
        "patterns": [
            r"educativo", r"educaci[oó]n", r"universidad", r"men", r"acad[eé]mic"
        ],
        "answer": (
            "Sí, experiencia directa en educación superior (SymbioTIC / UNIMINUTO). He trabajado en el cálculo de indicadores "
            "académicos y de gestión (competitividad de programas, comportamiento de matrícula, análisis de oferta/demanda y "
            "reportes oficiales al MEN), administrando bases de datos en SQL Server y creando tableros ejecutivos para "
            "toma de decisiones institucionales basadas en evidencia."
        ),
    }
]


def match_question(question: str) -> Dict:
    """
    Busca la respuesta más adecuada para una pregunta de postulación.
    Retorna un diccionario con la respuesta y metadata.
    """
    q_clean = question.lower().strip()

    best_match = None
    max_score = 0

    for entry in QA_ENTRIES:
        score = 0
        for pat in entry["patterns"]:
            if re.search(pat, q_clean):
                score += 1

        if score > max_score:
            max_score = score
            best_match = entry

    if best_match and max_score > 0:
        return {
            "matched": True,
            "id": best_match["id"],
            "category": best_match["category"],
            "answer": best_match["answer"],
            "length": len(best_match["answer"]),
        }

    # Fallback genérico profesional alineado a Miguel
    fallback_text = (
        "Cuento con formación en Ingeniería de Sistemas y experiencia en desarrollo y analítica de datos utilizando "
        "Python, SQL, Power BI y arquitecturas modernas. He diseñado e implementado soluciones en producción orientadas "
        "a la automatización, modelado y visualización de información para la toma de decisiones estratégicas. "
        "Mayor detalle y proyectos en: https://miguxld.site"
    )
    return {
        "matched": False,
        "id": "general_fallback",
        "category": "Perfil General",
        "answer": fallback_text,
        "length": len(fallback_text),
    }


def get_all_entries() -> List[Dict]:
    """Retorna todas las entradas del diccionario con conteo de caracteres."""
    return [
        {
            "id": e["id"],
            "category": e["category"],
            "answer": e["answer"],
            "length": len(e["answer"]),
        }
        for e in QA_ENTRIES
    ]
