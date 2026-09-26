import asyncio
from core.database import JobDatabase
from extractors.computrabajo import ComputrabajoExtractor
from services.matcher import MatcherEngine
from core.guard import WafBlocked


async def run_pipeline():
    db = JobDatabase("data/jobs.db")
    extractor = ComputrabajoExtractor()
    matcher = MatcherEngine()

    keywords = [
        "desarrollador junior",
        "programador junior",
        "ingeniero de sistemas junior",
        "analista de datos junior",
    ]

    print("=== 1. EXTRACCIÓN Y DEDUPLICACIÓN ===")
    total_new = 0
    for kw in keywords:
        try:
            offers = await extractor.search_jobs(query=kw, location="bogota-dc", max_pages=1)
            blocked = None
        except WafBlocked as wb:
            offers, blocked = wb.partial, wb
        for offer in offers:
            if db.save_offer(offer):
                total_new += 1
        if blocked:
            print(f"[!] {blocked}")
            break

    print(f"Nuevas vacantes insertadas: {total_new}")

    print("\n=== 2. EVALUACIÓN Y SCORING CON PERFIL ===")
    all_offers = db.get_offers()
    matched_count = 0
    review_count = 0
    discarded_count = 0

    for offer in all_offers:
        score, reason, status = matcher.evaluate(offer)
        db.update_match(offer.id, score, reason, status)
        if status == "matched":
            matched_count += 1
        elif status == "review":
            review_count += 1
        else:
            discarded_count += 1

    print(f"Total evaluadas: {len(all_offers)}")
    print(f"  [+] Matches prioritarios (>= 65%): {matched_count}")
    print(f"  [?] Por revisar (40-64%): {review_count}")
    print(f"  [-] Descartadas (< 40%): {discarded_count}")

    print("\n=== TOP VACANTES COMPATIBLES (MATCH ALTO) ===")
    top_matches = db.get_offers(status="matched")
    for i, offer in enumerate(top_matches[:10], start=1):
        print(f"\n{i}. [{offer.match_score}%] {offer.title}")
        print(f"   Empresa: {offer.company} | Salario: {offer.salary} | Ubicación: {offer.location}")
        print(f"   Motivo: {offer.match_reason}")
        print(f"   URL: {offer.url}")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
