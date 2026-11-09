"""A fictional flower shop for the demo."""
import sqlite3

PRODUCTS = (
    ("peony", "Peony bouquet", 4500, 8),
    ("ranunculus", "Ranunculus bouquet", 3800, 3),
    ("tulip", "Tulip bouquet", 2900, 20),
    ("orchid", "Orchid pot", 6200, 0),
)
SUBSTITUTES = (("peony", "ranunculus"), ("peony", "tulip"), ("orchid", "peony"))


def seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as c:
        c.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", PRODUCTS)
        c.executemany("INSERT OR IGNORE INTO substitutes VALUES (?,?)", SUBSTITUTES)
