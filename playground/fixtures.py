"""Deterministic fixture generation for the catalogue.

Parquet is written via pyarrow (bundled with datacontract-cli[parquet]);
CSV via the stdlib csv module. Everything uses a fixed random seed, except
sla-freshness timestamps which are intentionally relative to *generation
time* (now minus ~1h) so the freshness SLA passes right after generation.

Use ``generate_all(root)`` to (re)create every fixture, or
``ensure_fixtures(root)`` to only fill in missing files (called on app start).
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 42

COUNTRIES = ["US", "DE", "FR", "GB", "NL", "SE", "JP", "BR"]
STATUSES = ["completed", "processing", "shipped", "cancelled"]


def _orders_rows(n: int = 50, *, null_email_idx: set[int] | None = None,
                 bad_country_idx: set[int] | None = None,
                 int_amounts: bool = False) -> list[dict]:
    """Deterministic orders rows; optionally inject quality violations.

    ``int_amounts`` produces integer total_amount values (used by
    changelog-pair so v1/v2 can declare integer/number over the same BIGINT
    column without breaking the runnable v2 contract).
    """
    rng = random.Random(SEED)
    null_email_idx = null_email_idx or set()
    bad_country_idx = bad_country_idx or set()
    rows = []
    base_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        amount = rng.uniform(5.0, 500.0)
        rows.append({
            "order_id": f"ORD-{i + 1:04d}",
            "customer_name": f"Customer {i + 1:02d}",
            "email": None if i in null_email_idx else f"customer{i + 1:02d}@example.com",
            "country": "XX" if i in bad_country_idx else rng.choice(COUNTRIES),
            "total_amount": int(amount) if int_amounts else round(amount, 2),
            "status": rng.choice(STATUSES),
            "order_date": (base_date + timedelta(days=i)).date(),
        })
    return rows


def _write_parquet(rows: list[dict], path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path)


def _write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _customers_rows(n: int = 40) -> list[dict]:
    rng = random.Random(SEED)
    rows = []
    for i in range(n):
        rows.append({
            "customer_id": f"CUST-{i + 1:04d}",
            "name": f"Customer {i + 1:02d}",
            "email": f"customer{i + 1:02d}@example.com",
            "country": rng.choice(COUNTRIES),
            "total_amount": round(rng.uniform(0.0, 2000.0), 2),  # always >= 0
            "created_at": (datetime(2024, 2, 1, tzinfo=timezone.utc)
                           + timedelta(days=i)).replace(tzinfo=None),
        })
    return rows


def _products_rows(n: int = 30) -> list[dict]:
    rng = random.Random(SEED)
    categories = ["books", "electronics", "toys", "grocery"]
    rows = []
    for i in range(n):
        # 3 products have a null description -> completeness check fails (warning)
        description = None if i in {3, 11, 22} else f"Description for product {i + 1:02d}"
        rows.append({
            "product_id": f"PROD-{i + 1:04d}",
            "sku": f"SKU-{1000 + i}",
            "name": f"Product {i + 1:02d}",
            "description": description,
            "price": round(rng.uniform(1.0, 300.0), 2),
            "category": rng.choice(categories),
        })
    return rows


def _events_rows(n: int = 100, *, now: datetime | None = None) -> list[dict]:
    """Events with updated_at = now - ~1h (relative to generation time)."""
    rng = random.Random(SEED)
    now = now or datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        updated_at = now - timedelta(hours=1, seconds=rng.randint(0, 600))
        rows.append({
            "event_id": f"EVT-{i + 1:05d}",
            "event_type": rng.choice(["click", "view", "purchase", "signup"]),
            "updated_at": updated_at.replace(tzinfo=None),
        })
    return rows


def _line_items_rows(n: int = 120) -> list[dict]:
    rng = random.Random(SEED)
    rows = []
    for i in range(n):
        rows.append({
            "line_item_id": f"LI-{i + 1:05d}",
            "order_id": f"ORD-{rng.randint(1, 50):04d}",
            "product_id": f"PROD-{rng.randint(1, 30):04d}",
            "quantity": rng.randint(1, 5),
            "unit_price": round(rng.uniform(1.0, 150.0), 2),
        })
    return rows


# slug -> {relative path: row-builder}
_FIXTURES: dict[str, dict[str, object]] = {
    "hello-orders": {
        "data/orders.parquet": lambda: _orders_rows(),
    },
    "quality-failures": {
        # ~10% null emails (5 of 50) and 5 bad countries ("XX")
        "data/orders.parquet": lambda: _orders_rows(
            null_email_idx={2, 9, 17, 31, 44},
            bad_country_idx={5, 12, 23, 36, 47},
        ),
    },
    "sql-custom-checks": {
        "data/customers.csv": _customers_rows,
    },
    "dimensions-and-severity": {
        "data/products.parquet": _products_rows,
    },
    "sla-freshness": {
        "data/events.parquet": _events_rows,
    },
    "multi-table": {
        "data/orders.parquet": lambda: _orders_rows(),
        "data/line_items.parquet": _line_items_rows,
    },
    "changelog-pair": {
        # integer amounts so v1 (integer) and v2 (number) both map to BIGINT
        "data/orders.parquet": lambda: _orders_rows(int_amounts=True),
        # v2 adds a customers table; the {model} server path needs this file
        "data/customers.parquet": _customers_rows,
    },
}


def _write(slug: str, rel_path: str, rows: list[dict], contracts_dir: Path) -> Path:
    path = contracts_dir / slug / rel_path
    if path.suffix == ".parquet":
        _write_parquet(rows, path)
    elif path.suffix == ".csv":
        _write_csv(rows, path)
    else:
        raise ValueError(f"Unsupported fixture format: {path}")
    return path


def generate_all(contracts_dir: Path) -> list[Path]:
    """Regenerate every fixture file (overwrites existing)."""
    written = []
    for slug, files in _FIXTURES.items():
        for rel_path, builder in files.items():
            written.append(_write(slug, rel_path, builder(), contracts_dir))
    return written


# Fixtures whose rows depend on generation time go stale (e.g. the 24h
# freshness SLA checked against timestamps written days ago). They are
# rewritten on every ensure_fixtures call, not just when missing.
_VOLATILE = {("sla-freshness", "data/events.parquet")}


def ensure_fixtures(contracts_dir: Path) -> list[Path]:
    """Generate missing fixture files and refresh volatile ones. Called on app start."""
    written = []
    for slug, files in _FIXTURES.items():
        for rel_path, builder in files.items():
            path = contracts_dir / slug / rel_path
            if not path.is_file() or (slug, rel_path) in _VOLATILE:
                written.append(_write(slug, rel_path, builder(), contracts_dir))
    return written
