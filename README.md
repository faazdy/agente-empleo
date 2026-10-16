# Radar de Empleo — Agente de búsqueda de vacantes (MVL-PRY-0022)

Busca vacantes en **Computrabajo**, **elempleo.com** e **Indeed Colombia**, las filtra según el perfil
(primer cargo como desarrollador, Jr, Bogotá o remoto) y las ordena en un dashboard.
La postulación la hace la persona desde el dashboard: el sistema **no** postula solo.

## Estructura

| Ruta | Qué hace |
|---|---|
| `server.py` | API FastAPI (`http://localhost:8000`), escaneos y perfiles |
| `static/index.html`, `static/styles.css`, `static/app.js` | Dashboard: HTML + Bootstrap 5.3 + Bootstrap Icons y JavaScript sin frameworks (no se compila nada) |
| `services/profiles.py` | Perfiles de búsqueda en `data/profiles.json`: consultas, ciudad, portales y filtros manuales |
| `core/models.py` | Modelo `JobOffer` (hash por portal + empresa + cargo) |
| `core/database.py` | SQLite `data/jobs.db`, triple candado anti-duplicados, ranking 60% afinidad + 40% frescura |
| `core/guard.py` | Guardián anti-baneo: candado único, corte global, enfriamiento en `data/cooldown.json`, horario activo |
| `core/browser.py` | Navegador gestionado (Edge/Brave por CDP, puerto 9223): lo abre, conecta y cierra |
| `services/skills_gap.py` | Análisis "qué me falta aprender" |
| `services/filters.py` | Reglas únicas de filtrado (títulos, nivel, años de experiencia) |
| `services/matcher.py` | Puntaje de afinidad con el perfil |
| `services/qa_bank.py` | Banco de respuestas para preguntas de filtro |
| `extractors/computrabajo.py` | Extractor HTTPX con huella de navegador coherente por sesión |
| `extractors/elempleo.py` | Extractor de elempleo.com: API JSON del portal (token + cookies), sin navegador ni peticiones de detalle |
| `extractors/indeed.py` | Extractor de Indeed usando el navegador real |
| `tests/test_app.py` | Pruebas (`pytest`) |

## Uso

1. `python server.py` (desde la carpeta del proyecto, con el entorno virtual activado). Abre el dashboard
   solo en `http://localhost:8000`. No uses `--reload`: sin él uvicorn usa el bucle de eventos correcto en Windows
   (aun así, la parte del navegador corre aislada en su propio bucle y funciona en ambos casos).
   Variables opcionales: `RADAR_PORT`, `RADAR_HOST`, `RADAR_NO_BROWSER=1`.
2. **Escanear** recorre Computrabajo, luego elempleo y luego Indeed. En el menú del botón hay una barra para elegir
   **hasta cuántos días hacia atrás buscar** (1 a 30, solo para ese escaneo: útil al empezar o tras varios días sin
   escanear), una **pasada corta** (solo la primera página de cada consulta, sin Indeed) y la opción de escanear un
   portal suelto. Cada portal recibe su propio filtro de fecha: Computrabajo acepta 1, 3, 7, 15 o 30 días; Indeed
   corta en 14; elempleo filtra por su cuenta con el número exacto. Para Indeed la app abre sola Microsoft Edge
   (o Brave si no hay Edge) con un perfil dedicado en `%LOCALAPPDATA%\RadarEmpleo\navegador-perfil`,
   fuera de la pantalla, y lo cierra al terminar. No toca tu Edge personal.
3. **Navegador del radar** (pestaña Herramientas) abre ese mismo navegador visible, para iniciar sesión
   en Computrabajo/Indeed o resolver una verificación. Las sesiones quedan guardadas en el perfil.
4. **Sincronización diaria automática**: una sincronización completa al día desde las 07:30 (hora Bogotá),
   o apenas se encienda el servidor si ese día no se ha hecho (`GET /api/daily/status`).
5. **Postuladas** muestra el seguimiento: etapa de cada postulación (postulado, en proceso, entrevista,
   prueba técnica, oferta, rechazado, cerrada), notas, tasa de respuesta y las que llevan más de 21 días sin respuesta.
6. **Qué aprender** cuenta las tecnologías que piden las vacantes afines y las compara con el stack del
   Perfil Maestro (`services/skills_gap.py`).

## Perfiles y filtros manuales

- Pestaña **Perfiles**: cada perfil define qué buscar (consultas propias o las del radar, ciudad, ciudad/remoto,
  portales, páginas, días hacia atrás) y cómo filtrar (cargos afines, palabras que descartan, tecnologías,
  máximo de años de experiencia, puntaje mínimo, descartar Senior).
- "Partir de las reglas del radar" suma tus palabras a las reglas de `services/filters.py`; desmarcado, solo cuentan las tuyas
  (sirve para buscar otro tipo de cargo). El perfil **Miguel (por defecto)** se puede editar pero no borrar.
- **Crear perfil en blanco** abre un perfil vacío: no hereda las palabras clave ni los filtros del original, así que
  sirve para otra persona o para otro oficio. **Duplicar** parte del que estés viendo.
- Al **usar** un perfil (botón del formulario o selector de arriba) se vuelven a puntuar las vacantes de la base, sin tocar
  postuladas ni guardadas; el siguiente escaneo y la sincronización diaria usan sus consultas.
- En **Vacantes** los botones de estado (compatibles, guardadas, postuladas, descartadas, todas) llevan el conteo al lado.
  El buscador y los filtros (portal, modalidad, puntaje mínimo, esconder palabra, orden) solo afectan la vista y se recuerdan
  en el navegador.
- elempleo solo filtra por Bogotá: con otra ciudad, allí se busca únicamente remoto.

## Reglas anti-baneo

- Nunca dos escaneos a la vez: el manual y el diario comparten candado.
- Un 403/429 o captcha detiene **todo** el escaneo del portal y activa 2 h de enfriamiento persistente
  (`GET /api/guard/status` para verlo; borra la entrada de `data/cooldown.json` para levantarlo a mano).
- Computrabajo: una sola huella de navegador por sesión, pausas de 3.5–5.5 s por página, 3–5 s por detalle
  y 6–12 s extra entre consultas. Indeed: pausas de 6–10 s y máximo 25 detalles por sesión.
- elempleo: una sola sesión (cookies + token) por escaneo, 50 vacantes con descripción por petición,
  pausas de 3-5 s entre páginas y 6-12 s entre consultas; 401 renueva el token una vez; 403/429 = enfriamiento.

## Cobertura (verificada en vivo el 2026-09-25)

- Computrabajo se consulta **ordenado por fecha** (`by=publicationtime`) y se pagina mientras haya "Siguiente".
  Antes: orden por relevancia y tope de 2 páginas → en "desarrollador" (48 vacantes, 3 páginas) quedaban fuera
  vacantes de "Hace 4 horas" que aparecían en la página 3.
- Ventana de frescura: hasta 4 días (`MAX_AGE_DAYS`; Computrabajo con `pubdate=7` recortado por el filtro). La fecha se revisa antes de pedir el detalle.
- elempleo tiene su propia ventana, `ELEMPLEO_MAX_AGE_DAYS = 4` (en `services/filters.py`). Se consulta ordenado por fecha
  (`PublishDate_Desc`) y se deja de paginar cuando la mitad de la página ya está fuera de la ventana. Entiende "Hoy", "Ayer",
  "Hace N días" y "Hace N semanas". Si la API del portal cambia, usa como respaldo el HTML de resultados (página 1).
- Remoto: el slug correcto es `remoto` (`teletrabajo` devolvía 1 vacante contra 4 reales).
- Consultas amplias (`desarrollador`, `programador`, `ingeniero de sistemas`, `analista de datos`) más específicas
  para títulos que no dicen "desarrollador" (`software`, `developer`, `data analyst`, `power bi`, `sql`, `qa`…).

## Filtros de perfil

- Título: exclusiones (ventas, call center, PLC/CNC, salud, operativos, prácticas…) y roles afines.
- Nivel: títulos Senior / Líder / Arquitecto / Jefe se descartan; Semi Senior resta 10 puntos.
- Experiencia: si la descripción o los requisitos piden más años de los que acepta el perfil, se descarta;
  si piden 2 o más, resta 10 puntos. El tope se cambia desde la pestaña Perfiles (o en `services/filters.py`
  para el valor de partida).

## Pruebas

```
venv\Scripts\python -m pytest -q
```
