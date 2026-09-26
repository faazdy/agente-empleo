# Radar de Empleo — Agente de búsqueda de vacantes (MVL-PRY-0022)

Busca vacantes en **Computrabajo** e **Indeed Colombia**, las filtra según el perfil
(primer cargo como desarrollador, Jr, Bogotá o remoto) y las ordena en un dashboard.
La postulación la hace la persona desde el dashboard: el sistema **no** postula solo.

## Estructura

| Ruta | Qué hace |
|---|---|
| `server.py` | API + dashboard FastAPI (`http://localhost:8000`), sincronización y modo alerta |
| `core/models.py` | Modelo `JobOffer` (hash por portal + empresa + cargo) |
| `core/database.py` | SQLite `data/jobs.db`, triple candado anti-duplicados, ranking 60% afinidad + 40% frescura |
| `core/guard.py` | Guardián anti-baneo: candado único, corte global, enfriamiento en `data/cooldown.json`, horario de alerta |
| `core/browser.py` | Navegador gestionado (Edge/Brave por CDP, puerto 9223): lo abre, conecta y cierra |
| `services/skills_gap.py` | Análisis "qué me falta aprender" |
| `services/filters.py` | Reglas únicas de filtrado (títulos, nivel, años de experiencia) |
| `services/matcher.py` | Puntaje de afinidad con el perfil |
| `services/qa_bank.py` | Banco de respuestas para preguntas de filtro |
| `extractors/computrabajo.py` | Extractor HTTPX con huella de navegador coherente por sesión |
| `extractors/indeed.py` | Extractor de Indeed usando el navegador real |
| `tests/test_app.py` | Pruebas (`pytest`) |

## Uso

1. `python server.py` (desde la carpeta del proyecto, con el entorno virtual activado). Abre el dashboard
   solo en `http://localhost:8000`. No uses `--reload`: sin él uvicorn usa el bucle de eventos correcto en Windows
   (aun así, la parte del navegador corre aislada en su propio bucle y funciona en ambos casos).
   Variables opcionales: `RADAR_PORT`, `RADAR_HOST`, `RADAR_NO_BROWSER=1`.
2. **Escanear ahora** recorre Computrabajo y luego Indeed. Para Indeed la app abre sola Microsoft Edge
   (o Brave si no hay Edge) con un perfil dedicado en `%LOCALAPPDATA%\RadarEmpleo\navegador-perfil`,
   fuera de la pantalla, y lo cierra al terminar. No toca tu Edge personal.
3. **Navegador del radar** (botón del encabezado) abre ese mismo navegador visible, para iniciar sesión
   en Computrabajo/Indeed o resolver una verificación. Las sesiones quedan guardadas en el perfil.
4. **Sincronización diaria automática**: una sincronización completa al día desde las 07:30 (hora Bogotá),
   o apenas se encienda el servidor si ese día no se ha hecho (`GET /api/daily/status`).
5. **Postuladas** muestra el seguimiento: etapa de cada postulación (postulado, en proceso, entrevista,
   prueba técnica, oferta, rechazado, cerrada), notas, tasa de respuesta y las que llevan más de 21 días sin respuesta.
6. **Qué aprender** cuenta las tecnologías que piden las vacantes afines y las compara con el stack del
   Perfil Maestro (`services/skills_gap.py`).

## Reglas anti-baneo

- Nunca dos escaneos a la vez (sync manual y modo alerta comparten candado).
- Un 403/429 o captcha detiene **todo** el escaneo del portal y activa 2 h de enfriamiento persistente
  (`GET /api/guard/status` para verlo; borra la entrada de `data/cooldown.json` para levantarlo a mano).
- Computrabajo: una sola huella de navegador por sesión, pausas de 3.5–5.5 s por página, 3–5 s por detalle
  y 6–12 s extra entre consultas. Indeed: pausas de 6–10 s y máximo 25 detalles por sesión.
- Modo alerta: solo Computrabajo, página 1, cada 45 min y únicamente de 07:00 a 21:00 (hora Bogotá).

## Cobertura (verificada en vivo el 2026-09-25)

- Computrabajo se consulta **ordenado por fecha** (`by=publicationtime`) y se pagina mientras haya "Siguiente".
  Antes: orden por relevancia y tope de 2 páginas → en "desarrollador" (48 vacantes, 3 páginas) quedaban fuera
  vacantes de "Hace 4 horas" que aparecían en la página 3.
- Ventana de frescura: hoy, ayer y hace 2 días (`pubdate=2`, `MAX_AGE_DAYS`). La fecha se revisa antes de pedir el detalle.
- Remoto: el slug correcto es `remoto` (`teletrabajo` devolvía 1 vacante contra 4 reales).
- Consultas amplias (`desarrollador`, `programador`, `ingeniero de sistemas`, `analista de datos`) más específicas
  para títulos que no dicen "desarrollador" (`software`, `developer`, `data analyst`, `power bi`, `sql`, `qa`…).

## Filtros de perfil

- Título: exclusiones (ventas, call center, PLC/CNC, salud, operativos, prácticas…) y roles afines.
- Nivel: títulos Senior / Líder / Arquitecto / Jefe se descartan; Semi Senior resta 10 puntos.
- Experiencia: si la descripción o los requisitos piden más de `MAX_YEARS_EXPERIENCE` (2) años, se descarta;
  si piden exactamente 2, resta 10 puntos. Se ajusta en `services/filters.py`.

## Pruebas

```
venv\Scripts\python -m pytest -q
```
