import asyncio
from core.database import JobDatabase
from extractors.computrabajo import ComputrabajoExtractor
from services.matcher import MatcherEngine
from core.guard import WafBlocked


async def run_broad_sync():
    db = JobDatabase("data/jobs.db")
    extractor = ComputrabajoExtractor()
    matcher = MatcherEngine()

    # Queries de alta precisión enfocadas en el perfil real de Miguel
    queries = [
        "desarrollador python",
        "desarrollador full stack",
        "desarrollador backend",
        "desarrollador react",
        "desarrollador junior",
        "ingeniero de datos",
        "analista de datos",
        "desarrollador nodejs",
    ]

    print("=== EXTRAYENDO VACANTES AFINES CON DESCRIPCIÓN COMPLETA ===")
    existing_ids = db.get_existing_ids()
    total_added = 0

    for q in queries:
        print(f"\n-> Buscando '{q}' (página 1, con pausas de seguridad)...")
        try:
            offers = await extractor.search_jobs(
                query=q,
                location="bogota-dc",
                max_pages=1,
                existing_ids=existing_ids
            )
            blocked = None
        except WafBlocked as wb:
            offers, blocked = wb.partial, wb
        added_for_q = 0
        for offer in offers:
            if db.save_offer(offer):
                # Evaluación precisa con el cuerpo de la descripción real
                score, reason, status = matcher.evaluate(offer)
                db.update_match(offer.id, score, reason, status)
                existing_ids.add(offer.id)
                added_for_q += 1
                total_added += 1
                print(f"   [{score}% - {status.upper()}] {offer.title} @ {offer.company}")

        print(f"   Total nuevas para '{q}': {added_for_q}")
        if blocked:
            print(f"\n[!] {blocked}")
            break

    # Resumen final
    stats = db.get_stats()
    print("\n=== RESUMEN DE EXTRACCIÓN ===")
    print(f"Total en base de datos: {stats.get('total', 0)}")
    print(f"Vacantes Matched (afines): {stats.get('matched', 0)}")
    print(f"Vacantes Descartadas: {stats.get('discarded', 0)}")


if __name__ == "__main__":
    asyncio.run(run_broad_sync())
