"""
Navegador gestionado por la app (sin .bat).

La app abre por su cuenta Microsoft Edge (o Brave si no hay Edge) con:
- un perfil DEDICADO en %LOCALAPPDATA%\RadarEmpleo\navegador-perfil (no toca tu Edge personal; desde Chromium 136
  el puerto de depuración no funciona con el perfil principal),
- el puerto de depuración 9223 (distinto al 9222 habitual, para no chocar con nada),
- la ventana fuera de la pantalla, para que no estorbe mientras escanea.

Si la app abrió el navegador, también lo cierra al terminar. Si ya estaba abierto
(por ejemplo, porque lo abriste en modo visible para iniciar sesión), lo deja abierto.
"""
import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import httpx

CDP_PORT = int(os.environ.get("RADAR_CDP_PORT", "9223"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
# El perfil va en %LOCALAPPDATA% y NO dentro del proyecto: el proyecto está en OneDrive y
# sincronizar un perfil de navegador (cientos de MB, archivos bloqueados) da problemas.
_DEFAULT_PROFILE = Path(os.environ.get("LOCALAPPDATA", "data")) / "RadarEmpleo" / "navegador-perfil"
PROFILE_DIR = Path(os.environ.get("RADAR_BROWSER_PROFILE", str(_DEFAULT_PROFILE))).resolve()

BROWSER_CANDIDATES = [
    ("Edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("Edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("Edge", os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe")),
    ("Brave", r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("Brave", os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")),
]

# Fuera de pantalla (no minimizado: Chromium frena las pestañas minimizadas y eso se nota).
OFFSCREEN_ARGS = ["--window-position=-32000,-32000", "--window-size=1366,900"]
COMMON_ARGS = [
    "--no-first-run", "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
]


def find_browser() -> Optional[tuple]:
    """(nombre, ruta) del primer navegador disponible, o None."""
    custom = os.environ.get("RADAR_BROWSER_PATH")
    if custom and Path(custom).exists():
        return ("Personalizado", custom)
    for name, path in BROWSER_CANDIDATES:
        if path and Path(path).exists():
            return (name, path)
    return None


async def cdp_alive(port: int = CDP_PORT) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get(f"http://127.0.0.1:{port}/json/version")
            return r.status_code == 200
    except Exception:
        return False


class ManagedBrowser:
    """Arranca y apaga el navegador del radar."""

    def __init__(self, port: int = CDP_PORT, profile_dir: Path = PROFILE_DIR):
        self.port = port
        self.profile_dir = profile_dir
        self.launched_by_app = False
        self.visible = False
        self.browser_name: Optional[str] = None

    async def ensure(self, visible: bool = False, start_url: str = "about:blank", timeout: float = 25.0) -> bool:
        """Deja el navegador listo. Retorna True si lo tuvo que abrir."""
        if await cdp_alive(self.port):
            return False
        found = find_browser()
        if not found:
            raise ConnectionError(
                "No encontré Microsoft Edge ni Brave instalados. Instala Edge o define RADAR_BROWSER_PATH."
            )
        self.browser_name, exe = found
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [exe, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile_dir}", *COMMON_ARGS]
        if not visible:
            args += OFFSCREEN_ARGS
        args.append(start_url)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(0.5)
            waited += 0.5
            if await cdp_alive(self.port):
                self.launched_by_app = True
                self.visible = visible
                return True
        raise ConnectionError(f"{self.browser_name} no respondió en el puerto {self.port} tras {timeout:.0f} s.")

    async def shutdown(self, browser) -> None:
        """Cierra el navegador SOLO si lo abrió la app y está en modo oculto."""
        if not (self.launched_by_app and not self.visible) or browser is None:
            return
        try:
            session = await browser.new_browser_cdp_session()
            await session.send("Browser.close")
        except Exception:
            pass
        self.launched_by_app = False


managed_browser = ManagedBrowser()


class BrowserController:
    """Conexión Playwright (CDP) al navegador del radar. Lo abre si hace falta."""

    def __init__(self, cdp_url: str = CDP_URL, manager: ManagedBrowser = managed_browser,
                 auto_launch: bool = True, visible: bool = False):
        self.cdp_url = cdp_url
        self.manager = manager
        self.auto_launch = auto_launch
        self.visible = visible
        self._playwright = None
        self._browser = None
        self._context = None

    async def connect(self):
        if self.auto_launch:
            await self.manager.ensure(visible=self.visible)
        from playwright.async_api import async_playwright  # import perezoso
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else await self._browser.new_context()
            return self._context
        except Exception as e:
            await self.close()
            raise ConnectionError(f"No se pudo conectar al navegador del radar en {self.cdp_url}: {e}") from e

    async def open_job_page(self, url: str):
        if not self._context:
            await self.connect()
        page = await self._context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        return page

    async def sync_computrabajo_applications(self, db) -> dict:
        """
        Lee "Mis postulaciones" de Computrabajo y marca como 'applied' en la base.
        Requiere haber iniciado sesión en Computrabajo UNA vez en el navegador del radar
        (botón "Navegador del radar" del dashboard).
        """
        context = await self.connect()
        page = await context.new_page()
        try:
            url = "https://candidato.co.computrabajo.com/candidato/postulaciones"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.5)
            if "login" in page.url.lower() or "acceso" in page.url.lower():
                return {"success": False, "needs_login": True,
                        "message": "Inicia sesión en Computrabajo en el navegador del radar y vuelve a intentar."}

            apps = await page.evaluate("""
                () => {
                    const results = [];
                    const cards = document.querySelectorAll('article, .box_offer, .b_offer, tr, div[data-id]');
                    cards.forEach(card => {
                        const titleEl = card.querySelector('h2, h3, a.js-o-link, .title_offer, a');
                        const companyEl = card.querySelector('.t_ellipsis, p.fc_base, span');
                        if (titleEl) {
                            const title = titleEl.innerText.trim();
                            const company = companyEl ? companyEl.innerText.trim() : '';
                            const link = titleEl.getAttribute('href') || '';
                            if (title && title.length > 3) {
                                results.push({ title, company, url: link });
                            }
                        }
                    });
                    return results;
                }
            """)

            updated_count = 0
            for app_item in apps:
                if db.mark_as_applied(title=app_item.get("title", ""), company=app_item.get("company", "")):
                    updated_count += 1
            return {"success": True, "found_applications": len(apps), "synced_to_db": updated_count}
        finally:
            await page.close()

    async def close(self) -> None:
        """Cierra el navegador solo si la app lo abrió en modo oculto; siempre corta Playwright."""
        try:
            await self.manager.shutdown(self._browser)
        finally:
            self._browser = None
            self._context = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None


# Compatibilidad con el nombre anterior
BraveController = BrowserController
