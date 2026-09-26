import sqlite3

conn = sqlite3.connect("data/jobs.db")
cur = conn.cursor()

# Guardamos el recuento antes de limpiar
before_count = cur.execute("SELECT COUNT(*) FROM job_offers").fetchone()[0]

# Eliminamos duplicados manteniendo el primer registro único por (portal, empresa, título)
query = """
DELETE FROM job_offers
WHERE rowid NOT IN (
    SELECT MIN(rowid)
    FROM job_offers
    GROUP BY LOWER(portal), LOWER(TRIM(company)), LOWER(TRIM(title))
)
"""
cur.execute(query)
deleted = cur.rowcount
conn.commit()

after_count = cur.execute("SELECT COUNT(*) FROM job_offers").fetchone()[0]
matched_count = cur.execute("SELECT COUNT(*) FROM job_offers WHERE status = 'matched'").fetchone()[0]

print(f"Antes: {before_count} ofertas.")
print(f"Eliminadas: {deleted} duplicadas.")
print(f"Total únicas restantes: {after_count}.")
print(f"Total disponibles para ti (matched): {matched_count}.")
