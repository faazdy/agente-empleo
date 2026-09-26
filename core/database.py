import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from core.models import JobOffer


STAGES = {
    "postulado": "Postulado (sin respuesta)",
    "en_proceso": "Te contactaron / En proceso",
    "entrevista": "Entrevista",
    "prueba_tecnica": "Prueba técnica",
    "oferta": "Oferta",
    "rechazado": "Rechazado",
    "cerrada": "Cerrada / Sin respuesta",
}
STALE_DAYS = 21  # postulaciones sin movimiento por más de 3 semanas


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobDatabase:
    """Gestor de persistencia SQLite para ofertas de empleo."""

    def __init__(self, db_path: str = "data/jobs.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_offers (
                    id TEXT PRIMARY KEY,
                    portal TEXT NOT NULL,
                    external_id TEXT,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    modality TEXT,
                    salary TEXT,
                    description TEXT NOT NULL,
                    requirements TEXT,
                    url TEXT NOT NULL,
                    posted_date TEXT,
                    scraped_at TEXT NOT NULL,
                    match_score REAL,
                    match_reason TEXT,
                    status TEXT NOT NULL DEFAULT 'new'
                )
                """
            )
            # Migración no destructiva: columnas de seguimiento
            cols = {r[1] for r in conn.execute("PRAGMA table_info(job_offers)").fetchall()}
            for col in ("applied_at", "stage", "stage_updated_at", "notes"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE job_offers ADD COLUMN {col} TEXT")
            # Postulaciones antiguas: fecha aproximada = fecha de extracción
            conn.execute(
                "UPDATE job_offers SET applied_at = scraped_at WHERE status = 'applied' AND applied_at IS NULL"
            )
            conn.execute(
                "UPDATE job_offers SET stage = 'postulado' WHERE status = 'applied' AND stage IS NULL"
            )
            conn.commit()

    def save_offer(self, offer: JobOffer) -> bool:
        """
        Guarda una oferta si no existe. 
        Blindaje triple anti-duplicados:
        1. Por ID o external_id
        2. Por URL exacta
        3. Por mismo título y misma empresa (normalizados)
        Retorna True si fue insertada, False si era duplicada.
        """
        with self._get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM job_offers 
                WHERE id = ? 
                   OR (external_id IS NOT NULL AND external_id = ?)
                   OR url = ?
                   OR (LOWER(TRIM(title)) = ? AND LOWER(TRIM(company)) = ?)
                LIMIT 1
                """,
                (
                    offer.id,
                    offer.external_id,
                    offer.url,
                    offer.title.strip().lower(),
                    offer.company.strip().lower(),
                ),
            ).fetchone()

            if existing:
                return False

            query = """
                INSERT INTO job_offers (
                    id, portal, external_id, title, company, location, modality,
                    salary, description, requirements, url, posted_date,
                    scraped_at, match_score, match_reason, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cursor = conn.execute(
                query,
                (
                    offer.id,
                    offer.portal,
                    offer.external_id,
                    offer.title,
                    offer.company,
                    offer.location,
                    offer.modality,
                    offer.salary,
                    offer.description,
                    json.dumps(offer.requirements, ensure_ascii=False),
                    offer.url,
                    offer.posted_date,
                    offer.scraped_at.isoformat(),
                    offer.match_score,
                    offer.match_reason,
                    offer.status,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def is_today_or_yesterday(posted_str: Optional[str]) -> bool:
        """
        Blindaje de fecha: solo admite vacantes publicadas HOY o AYER.
        Descarta cualquier cosa de 'Hace 2 días', 'Hace 5 días', 'Más de 30 días', etc.
        """
        if not posted_str:
            return True
        s = str(posted_str).lower().strip()
        # Hoy: "hace 10 minutos", "hace 4 horas"
        if "minuto" in s or "hora" in s:
            return True
        # Ayer: "ayer"
        if "ayer" in s:
            return True
        # Cualquier otra antigüedad queda descartada
        return False

    @staticmethod
    def is_fresh(posted_str: Optional[str]) -> bool:
        """Ventana vigente del radar (hoy, ayer y hace 2 días). Ver services/filters.MAX_AGE_DAYS."""
        from services.filters import is_fresh
        return is_fresh(posted_str)

    @staticmethod
    def _parse_posted_age_hours(posted_str: Optional[str]) -> float:
        if not posted_str:
            return 48.0
        import re
        s = str(posted_str).lower().strip()
        m = re.search(r"hace\s+(\d+)\s+minuto", s)
        if m:
            return float(m.group(1)) / 60.0
        m = re.search(r"hace\s+(\d+)\s+hora", s)
        if m:
            return float(m.group(1))
        if "ayer" in s:
            return 24.0
        m = re.search(r"hace\s+(\d+)\s+d[ií]a", s)
        if m:
            return float(m.group(1)) * 24.0
        m = re.search(r"(\d+)\s+de\s+septiembre", s)
        if m:
            diff = max(1, 23 - int(m.group(1)))
            return float(diff * 24.0)
        if "agosto" in s or "m[aá]s de 30" in s or "mas de 30" in s:
            return 720.0
        return 72.0

    @classmethod
    def compute_opportunity_score(cls, match_score: Optional[float], posted_date: Optional[str]) -> float:
        """Calcula score combinado de 0 a 100: 60% Afinidad Técnica + 40% Frescura de Publicación."""
        match = float(match_score or 0)
        hours = cls._parse_posted_age_hours(posted_date)
        if hours <= 2:
            recency = 100.0
        elif hours <= 6:
            recency = 90.0
        elif hours <= 12:
            recency = 80.0
        elif hours <= 24:
            recency = 70.0
        elif hours <= 48:
            recency = 55.0
        elif hours <= 72:
            recency = 40.0
        elif hours <= 168:
            recency = 25.0
        elif hours <= 360:
            recency = 10.0
        else:
            recency = 0.0
        return round((match * 0.6) + (recency * 0.4), 1)

    def get_offers(
        self, 
        status: Optional[str] = None, 
        min_match: Optional[float] = None,
        sort_by: str = "smart"
    ) -> List[JobOffer]:
        """
        Obtiene ofertas filtradas y ordenadas.
        sort_by:
          - 'smart': Combina 60% Afinidad Técnica + 40% Frescura de Publicación (Recomendado).
          - 'recent': Orden estricto por momento de extracción (scraped_at DESC).
          - 'score': Orden estricto por porcentaje de afinidad técnica (match_score DESC).
        """
        query = "SELECT * FROM job_offers WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if min_match is not None:
            query += " AND match_score >= ?"
            params.append(min_match)
            
        if sort_by == "score":
            query += " ORDER BY match_score DESC, scraped_at DESC"
        elif sort_by == "recent":
            query += " ORDER BY scraped_at DESC, match_score DESC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            offers = []
            for r in rows:
                data = dict(r)
                if data["requirements"]:
                    data["requirements"] = json.loads(data["requirements"])
                else:
                    data["requirements"] = []
                offers.append(JobOffer(**data))

            if sort_by == "smart":
                offers.sort(
                    key=lambda o: (
                        self.compute_opportunity_score(o.match_score, o.posted_date),
                        o.match_score or 0,
                    ),
                    reverse=True
                )

            return offers

    def update_status(self, offer_id: str, status: str) -> None:
        with self._get_connection() as conn:
            if status == "applied":
                conn.execute(
                    """UPDATE job_offers SET status = 'applied',
                           applied_at = COALESCE(applied_at, ?),
                           stage = COALESCE(stage, 'postulado'),
                           stage_updated_at = COALESCE(stage_updated_at, ?)
                       WHERE id = ?""",
                    (_now_iso(), _now_iso(), offer_id),
                )
            else:
                # Si se desmarca la postulación, se limpia el seguimiento
                conn.execute(
                    """UPDATE job_offers SET status = ?, applied_at = NULL, stage = NULL,
                           stage_updated_at = NULL WHERE id = ?""",
                    (status, offer_id),
                )
            conn.commit()

    def update_tracking(self, offer_id: str, stage: Optional[str] = None, notes: Optional[str] = None) -> bool:
        if stage is not None and stage not in STAGES:
            raise ValueError(f"Etapa inválida: {stage}")
        with self._get_connection() as conn:
            row = conn.execute("SELECT status FROM job_offers WHERE id = ?", (offer_id,)).fetchone()
            if not row or row["status"] != "applied":
                return False
            if stage is not None:
                conn.execute("UPDATE job_offers SET stage = ?, stage_updated_at = ? WHERE id = ?",
                             (stage, _now_iso(), offer_id))
            if notes is not None:
                conn.execute("UPDATE job_offers SET notes = ? WHERE id = ?", (notes, offer_id))
            conn.commit()
            return True

    def get_tracking_summary(self, now: Optional[datetime] = None) -> dict:
        """Embudo de postulaciones, tasa de respuesta y postulaciones estancadas."""
        now = now or datetime.now(timezone.utc)
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, title, company, portal, url, applied_at, stage, stage_updated_at "
                "FROM job_offers WHERE status = 'applied'"
            ).fetchall()
        by_stage = {k: 0 for k in STAGES}
        by_portal: dict = {}
        stale = []
        for r in rows:
            stage = r["stage"] or "postulado"
            by_stage[stage] = by_stage.get(stage, 0) + 1
            by_portal[r["portal"]] = by_portal.get(r["portal"], 0) + 1
            ref = r["stage_updated_at"] or r["applied_at"]
            try:
                ref_dt = datetime.fromisoformat(ref)
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=timezone.utc)
                days = (now - ref_dt).days
            except Exception:
                days = None
            if stage == "postulado" and days is not None and days >= STALE_DAYS:
                stale.append({"id": r["id"], "title": r["title"], "company": r["company"],
                              "url": r["url"], "days": days})
        total = len(rows)
        responded = total - by_stage.get("postulado", 0) - by_stage.get("cerrada", 0)
        stale.sort(key=lambda x: -x["days"])
        return {
            "total": total,
            "by_stage": by_stage,
            "stage_labels": STAGES,
            "by_portal": by_portal,
            "response_rate": round(100.0 * responded / total, 1) if total else 0.0,
            "stale_days": STALE_DAYS,
            "stale_count": len(stale),
            "stale": stale[:50],
        }

    def mark_as_applied(self, external_id: Optional[str] = None, title: Optional[str] = None, company: Optional[str] = None) -> bool:
        """Marca una oferta como postulado si coincide por external_id o por título y empresa."""
        with self._get_connection() as conn:
            if external_id:
                res = conn.execute(
                    "UPDATE job_offers SET status = 'applied', applied_at = COALESCE(applied_at, ?), "
                    "stage = COALESCE(stage, 'postulado') WHERE external_id = ?",
                    (_now_iso(), external_id))
                if res.rowcount > 0:
                    conn.commit()
                    return True
            if title and company:
                res = conn.execute(
                    "UPDATE job_offers SET status = 'applied', applied_at = COALESCE(applied_at, ?), "
                    "stage = COALESCE(stage, 'postulado') WHERE LOWER(title) LIKE ? AND LOWER(company) LIKE ?",
                    (_now_iso(), f"%{title.lower().strip()}%", f"%{company.lower().strip()}%")
                )
                if res.rowcount > 0:
                    conn.commit()
                    return True
        return False

    def update_match(self, offer_id: str, score: float, reason: str, status: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE job_offers SET match_score = ?, match_reason = ?, status = ? WHERE id = ?",
                (score, reason, status, offer_id),
            )
            conn.commit()

    def get_stats(self) -> dict:
        """Retorna conteo consolidado de ofertas por estado."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) as cnt FROM job_offers GROUP BY status").fetchall()
            stats = {"total": 0, "matched": 0, "applied": 0, "favorite": 0, "discarded": 0, "new": 0}
            for r in rows:
                st = r["status"]
                cnt = r["cnt"]
                stats[st] = cnt
                stats["total"] += cnt
            return stats

    def get_existing_ids(self) -> set:
        """Retorna conjunto de todos los identificadores conocidos (ID, external_id, URL, título+empresa) para evitar duplicados."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT id, external_id, url, title, company FROM job_offers").fetchall()
            ids = set()
            for r in rows:
                if r["id"]:
                    ids.add(r["id"])
                if r["external_id"]:
                    ids.add(r["external_id"])
                if r["url"]:
                    ids.add(r["url"])
                if r["title"] and r["company"]:
                    sig = f"{r['title'].strip().lower()}::{r['company'].strip().lower()}"
                    ids.add(sig)
            return ids

