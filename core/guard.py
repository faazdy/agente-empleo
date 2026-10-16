"""
Guardián anti-baneo compartido por todos los extractores.

- Candado único: nunca corren dos escaneos a la vez (manual y automático).
- Corte global: un 403/429 en cualquier consulta detiene TODO el escaneo de ese portal.
- Enfriamiento persistente: el bloqueo se guarda en data/cooldown.json y sobrevive
  a reinicios del servidor, para no volver a golpear el portal apenas se levanta.
- Horario activo para los escaneos automáticos (hora de Bogotá, UTC-5 fijo:
  Colombia no tiene horario de verano).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

BOGOTA_TZ = timezone(timedelta(hours=-5))
COOLDOWN_HOURS = 2.0
ACTIVE_WINDOW = (7, 21)  # los escaneos automáticos solo corren de 07:00 a 21:00 hora de Bogotá


class WafBlocked(Exception):
    """El portal respondió con bloqueo (403/429/captcha). Lleva las ofertas ya extraídas."""

    def __init__(self, portal: str, detail: str, partial: Optional[List] = None):
        super().__init__(f"{portal}: bloqueo detectado ({detail}). Escaneo detenido para proteger la IP.")
        self.portal = portal
        self.detail = detail
        self.partial = partial or []


class VerificationRequired(WafBlocked):
    """El portal muestra una verificación (p. ej. Cloudflare 'Un momento…') que necesita a una persona.
    NO es un bloqueo de IP: no activa enfriamiento; se resuelve en la ventana del navegador del radar."""

    def __init__(self, portal: str, detail: str = "", partial: Optional[List] = None):
        Exception.__init__(self, f"{portal} pide una verificación (Cloudflare). Abre el navegador del radar, "
                                 f"resuélvela y vuelve a escanear.")
        self.portal = portal
        self.detail = detail
        self.partial = partial or []


class ScrapeGuard:
    def __init__(self, path: str = "data/cooldown.json"):
        self.path = Path(path)
        self.lock = asyncio.Lock()

    # --- persistencia -------------------------------------------------------
    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- enfriamiento -------------------------------------------------------
    def blocked_until(self, portal: str) -> Optional[datetime]:
        entry = self._read().get(portal.lower())
        if not entry or entry.get("kind") == "verification":
            return None
        try:
            until = datetime.fromisoformat(entry["until"])
        except Exception:
            return None
        return until if until > datetime.now(timezone.utc) else None

    def check(self, portal: str) -> None:
        """Lanza WafBlocked si el portal sigue en enfriamiento."""
        until = self.blocked_until(portal)
        if until:
            local = until.astimezone(BOGOTA_TZ).strftime("%H:%M")
            raise WafBlocked(portal, f"en enfriamiento hasta las {local} (hora Bogotá)")

    def trip(self, portal: str, reason: str, hours: float = COOLDOWN_HOURS) -> datetime:
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        data = self._read()
        data[portal.lower()] = {
            "until": until.isoformat(),
            "reason": reason,
            "tripped_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)
        print(f"[ScrapeGuard] {portal} bloqueado ({reason}). Enfriamiento hasta {until.isoformat()}")
        return until

    def mark_verification(self, portal: str, reason: str) -> None:
        """Marca que el portal necesita verificación humana (no bloquea consultas)."""
        data = self._read()
        data[portal.lower()] = {"kind": "verification", "until": datetime.now(timezone.utc).isoformat(),
                                "reason": reason, "tripped_at": datetime.now(timezone.utc).isoformat()}
        self._write(data)

    def resolve_verification(self, portal: str) -> None:
        """Quita la marca de verificación (no toca los bloqueos reales)."""
        data = self._read()
        if data.get(portal.lower(), {}).get("kind") == "verification":
            data.pop(portal.lower(), None)
            self._write(data)

    def clear(self, portal: str) -> None:
        data = self._read()
        data.pop(portal.lower(), None)
        self._write(data)

    def status(self) -> dict:
        now = datetime.now(timezone.utc)
        out = {}
        for portal, entry in self._read().items():
            try:
                until = datetime.fromisoformat(entry["until"])
            except Exception:
                continue
            verification = entry.get("kind") == "verification"
            out[portal] = {
                "blocked": (until > now) and not verification,
                "needs_verification": verification,
                "until": entry["until"],
                "reason": entry.get("reason"),
            }
        return {"scan_running": self.lock.locked(), "portals": out,
                "active_window_open": in_active_window()}


def in_active_window(now: Optional[datetime] = None) -> bool:
    """True si estamos en el horario en que corren los escaneos automáticos."""
    now = now or datetime.now(timezone.utc)
    hour = now.astimezone(BOGOTA_TZ).hour
    return ACTIVE_WINDOW[0] <= hour < ACTIVE_WINDOW[1]


guard = ScrapeGuard()
