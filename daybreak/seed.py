"""Synthetic demo data: a fictional operator and fictional orders."""
import sqlite3
from datetime import datetime, timedelta, timezone

# version, full refund cutoff hours, partial cutoff hours, partial percent
POLICY = (3, 24, 2, 50)
# order id, capture id, cents, session name
ORDERS = (
    ("order-39", "CAP-39", 3900, "Watercolor Basics"),
    ("order-49", "CAP-49", 4900, "Ink Sketching"),
    ("order-29", "CAP-29", 2900, "Color Mixing"),
)


def seed(db_path: str, now: datetime | None = None) -> None:
    starts = (now or datetime.now(timezone.utc)) + timedelta(days=7)
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT OR IGNORE INTO policies (version, full_refund_cutoff_hours, partial_cutoff_hours, partial_percent)"
            " VALUES (?,?,?,?)",
            POLICY,
        )
        for order_id, capture_id, cents, session in ORDERS:
            c.execute(
                "INSERT OR IGNORE INTO orders VALUES (?,?,?,'USD',?,?)",
                (order_id, capture_id, cents, session, starts.isoformat()),
            )
            for number, days in enumerate((14, 21), start=1):
                c.execute(
                    "INSERT OR IGNORE INTO sessions (id, workshop, starts_at, seats_left) VALUES (?,?,?,?)",
                    (f"{order_id}-s{number}", session, (starts + timedelta(days=days)).isoformat(), 2),
                )
