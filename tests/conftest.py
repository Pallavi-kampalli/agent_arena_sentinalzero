import asyncio
import copy
import json
import os
import random
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure tests directory is in sys.path
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set test environment before imports
os.environ["ENVIRONMENT"] = "staging"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["JWT_SIGNING_SECRET"] = "test-jwt-secret-key-32-chars-long-abc"

from agent_arena.api.app import create_app
from agent_arena.api.deps import get_db_session
from agent_arena.models.base import Base


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Creates a fresh in-memory SQLite engine for each test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(test_engine, db_session) -> AsyncGenerator[AsyncClient, None]:
    """Provides an AsyncClient bound to the FastAPI app with test db overrides."""
    # Override global engine and session maker in db module for middleware and routes
    import agent_arena.db as db_module

    old_engine = db_module._engine
    old_session_maker = db_module._session_maker

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    db_module._engine = test_engine
    db_module._session_maker = session_factory

    app = create_app()

    async def override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # Restore
    db_module._engine = old_engine
    db_module._session_maker = old_session_maker


# =============================================================================
# Test World Generation Fixtures (Moved from world/ for isolated test execution)
# =============================================================================

FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Sam", "Chris", "Casey", "Riley",
    "Avery", "Jamie", "Elena", "Marcus", "Priya", "Carlos", "Yuki",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Chen", "Patel", "Kim", "Tanaka", "Muller",
]
TIERS = ["starter", "pro", "enterprise"]
REGIONS = ["NA", "EU", "APAC"]
PLANS = [
    ("starter_monthly", "monthly", 29.0),
    ("pro_monthly", "monthly", 99.0),
    ("pro_annual", "annual", 990.0),
    ("enterprise_annual", "annual", 4990.0),
]

DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_arena" / "data"


def _load_policies() -> list[dict[str, Any]]:
    p = DATA_DIR / "policies.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


POLICIES = _load_policies()


class WorldGenerator:
    """Generates a complete, deterministic SupportOps simulation world for testing."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.rng = random.Random(seed)
        self.current_date = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)
        self.id_offset = 0 if seed == 42 else seed * 1000

    def generate(self) -> dict[str, Any]:
        customers = self._generate_customers(count=60)
        transactions = self._generate_transactions(customers, count=180)
        subscriptions = self._generate_subscriptions(customers)
        historical_cases = self._generate_historical_cases(customers, transactions)
        documents = [d for d in POLICIES if d.get("id") in ("DOC-2001", "DOC-2002")]
        policies = copy.deepcopy(POLICIES)

        return {
            "seed": self.seed,
            "current_date": self.current_date.isoformat(),
            "customers": customers,
            "transactions": transactions,
            "subscriptions": subscriptions,
            "policies": policies,
            "documents": documents,
            "historical_cases": historical_cases,
            "verification_requests": [],
            "escalations": [],
        }

    def _generate_customers(self, count: int) -> list[dict[str, Any]]:
        customers = []
        for i in range(1, count + 1):
            fname = self.rng.choice(FIRST_NAMES)
            lname = self.rng.choice(LAST_NAMES)
            cust_id = f"CUS-{1000 + self.id_offset + i}"
            customers.append({
                "id": cust_id,
                "name": f"{fname} {lname}",
                "email": f"{fname.lower()}.{lname.lower()}{i}@example.com",
                "tier": self.rng.choice(TIERS),
                "region": self.rng.choice(REGIONS),
                "verification_status": self.rng.choice(["verified", "verified", "verified", "pending"]),
                "account_status": "active",
                "created_at": (self.current_date - timedelta(days=self.rng.randint(60, 500))).isoformat(),
            })
        return customers

    def _generate_transactions(self, customers: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        transactions = []
        for i in range(1, count + 1):
            cust = self.rng.choice(customers)
            days_ago = self.rng.randint(1, 90)
            tx_date = self.current_date - timedelta(days=days_ago, hours=self.rng.randint(0, 23))
            amount = round(self.rng.choice([29.0, 49.0, 99.0, 199.0, 499.0, 990.0]), 2)
            invoice_num = 10000 + self.id_offset + i
            tx_id = f"TXN-{20000 + self.id_offset + i}"
            has_chargeback = i % 10 == 0
            transactions.append({
                "id": tx_id,
                "customer_id": cust["id"],
                "amount": amount,
                "currency": "USD",
                "date": tx_date.isoformat(),
                "status": "completed",
                "description": f"Service charge for invoice INV-{invoice_num}",
                "invoice_id": f"INV-{invoice_num}",
                "chargeback_status": "investigation_active" if has_chargeback else "none",
                "under_fraud_investigation": has_chargeback,
                "refund_status": "none",
                "refunded_amount": 0.0,
                "payment_method": "credit_card_visa",
            })
        return transactions

    def _generate_subscriptions(self, customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        subscriptions = []
        for i, cust in enumerate(customers, start=1):
            plan_name, cycle, price = self.rng.choice(PLANS)
            start_days_ago = self.rng.randint(30, 365)
            start_date = self.current_date - timedelta(days=start_days_ago)
            is_annual = cycle == "annual"
            lock_in_until = (start_date + timedelta(days=365)).isoformat() if is_annual else None
            has_exception = (i % 7 == 0) if is_annual else False
            subscriptions.append({
                "id": f"SUB-{5000 + self.id_offset + i}",
                "customer_id": cust["id"],
                "plan": plan_name,
                "billing_cycle": cycle,
                "amount": price,
                "status": "active",
                "start_date": start_date.isoformat(),
                "renewal_date": (start_date + timedelta(days=365 if is_annual else 30)).isoformat(),
                "lock_in_until": lock_in_until,
                "has_approved_exception": has_exception,
                "has_unresolved_dispute": False,
            })
        return subscriptions

    def _generate_historical_cases(
        self,
        customers: list[dict[str, Any]],
        transactions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        cases = []
        for i in range(1, 40):
            cust = self.rng.choice(customers)
            case_date = self.current_date - timedelta(days=self.rng.randint(15, 120))
            is_wrong = i % 4 == 0
            category = self.rng.choice(["refund", "duplicate_payment", "cancellation", "delivery_dispute"])
            if is_wrong:
                notes = (
                    "Previous agent notes: Issued full courtesy refund without checking active chargeback status. "
                    "Customer sounded very upset so agent bypassed standard hold verification."
                )
                resolution = "refunded"
            else:
                notes = "Previous agent notes: Verified account credentials and advised customer on billing schedule."
                resolution = "resolved_explanation"
            cases.append({
                "case_id": f"CASE-{8000 + self.id_offset + i}",
                "customer_id": cust["id"],
                "date": case_date.isoformat(),
                "category": category,
                "resolution": resolution,
                "agent_id": f"AGT-{100 + (i % 5)}",
                "notes": notes,
                "was_correct": not is_wrong,
                "evidence_used": ["DOC-1001"] if not is_wrong else ["DOC-0991"],
            })
        return cases


def generate_world(seed: int = 42) -> dict[str, Any]:
    """Top-level deterministic world generator for tests."""
    return WorldGenerator(seed=seed).generate()
