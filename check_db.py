from backend.services.database import database

c = database.connect()

tables = c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()

print([row[0] for row in tables])

c.close()
