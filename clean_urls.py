import sqlite3

conn = sqlite3.connect("data/jobs.db")
rows = conn.execute("SELECT id, url FROM job_offers").fetchall()
cleaned = 0
for offer_id, url in rows:
    if "#" in url:
        clean_url = url.split("#")[0]
        conn.execute("UPDATE job_offers SET url = ? WHERE id = ?", (clean_url, offer_id))
        cleaned += 1

conn.commit()
print(f"Limpieza completada: {cleaned} URLs actualizadas sin fragmentos #lc.")
