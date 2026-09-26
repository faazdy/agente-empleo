from core.database import JobDatabase
from services.matcher import MatcherEngine

def re_evaluate_database():
    db = JobDatabase("data/jobs.db")
    matcher = MatcherEngine()
    
    offers = db.get_offers(sort_by="recent")
    print(f"Re-evaluando {len(offers)} vacantes existentes...")

    discarded_count = 0
    matched_count = 0

    for offer in offers:
        score, reason, status = matcher.evaluate(offer)
        # Si ya fue aplicada o favorita por el usuario, preservamos ese estado
        final_status = offer.status if offer.status in ("applied", "favorite") else status
        db.update_match(offer.id, score, reason, final_status)
        
        if final_status == "discarded":
            discarded_count += 1
        elif final_status == "matched":
            matched_count += 1

    print(f"Finalizado: {matched_count} compatibles, {discarded_count} descartadas.")
    stats = db.get_stats()
    print("Estadísticas consolidadas:", stats)

if __name__ == "__main__":
    re_evaluate_database()
