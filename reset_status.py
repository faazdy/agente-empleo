import sqlite3

conn = sqlite3.connect("data/jobs.db")
cur = conn.cursor()
cur.execute("UPDATE job_offers SET status = 'matched' WHERE status = 'applied'")
conn.commit()
print(f"Reseteadas {cur.rowcount} ofertas a estado 'matched'. Ahora Postuladas = 0.")
