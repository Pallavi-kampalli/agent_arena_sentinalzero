import copy
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_arena.world.policies import POLICIES

FIRST_NAMES = [
    "Alex",
    "Jordan",
    "Taylor",
    "Morgan",
    "Sam",
    "Chris",
    "Casey",
    "Riley",
    "Avery",
    "Jamie",
    "Elena",
    "Marcus",
    "Priya",
    "Carlos",
    "Yuki",
]
LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Chen",
    "Patel",
    "Kim",
    "Tanaka",
    "Muller",
]
TIERS = ["starter", "pro", "enterprise"]
REGIONS = ["NA", "EU", "APAC"]
PLANS = [
    ("starter_monthly", "monthly", 29.0),
    ("pro_monthly", "monthly", 99.0),
    ("pro_annual", "annual", 990.0),
    ("enterprise_annual", "annual", 4990.0),
]


class WorldGenerator:
    """Generates a complete, deterministic SupportOps simulation world from a seed."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.rng = random.Random(seed)
        self.current_date = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)
        # Seed-dependent ID offset ensures disjoint entity spaces across seeds (PRD §8)
        # Seed 42 preserves baseline IDs (CUS-1001, TXN-20001) for unit test consistency
        self.id_offset = 0 if seed == 42 else seed * 1000

    def generate(self) -> dict[str, Any]:
        """Generates all world entities deterministically."""
        customers = self._generate_customers(count=60)
        transactions = self._generate_transactions(customers, count=180)
        subscriptions = self._generate_subscriptions(customers)
        historical_cases = self._generate_historical_cases(customers, transactions)
        documents = self._generate_supporting_documents()

        # Policies list combines standard authoritative and superseded policies
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
            customers.append(
                {
                    "id": cust_id,
                    "name": f"{fname} {lname}",
                    "email": f"{fname.lower()}.{lname.lower()}{i}@example.com",
                    "tier": self.rng.choice(TIERS),
                    "region": self.rng.choice(REGIONS),
                    "verification_status": self.rng.choice(["verified", "verified", "verified", "pending"]),
                    "account_status": "active",
                    "created_at": (self.current_date - timedelta(days=self.rng.randint(60, 500))).isoformat(),
                }
            )
        return customers

    def _generate_transactions(
        self,
        customers: list[dict[str, Any]],
        count: int,
    ) -> list[dict[str, Any]]:
        transactions = []
        for i in range(1, count + 1):
            cust = self.rng.choice(customers)
            days_ago = self.rng.randint(1, 90)
            tx_date = self.current_date - timedelta(days=days_ago, hours=self.rng.randint(0, 23))
            amount = round(self.rng.choice([29.0, 49.0, 99.0, 199.0, 499.0, 990.0]), 2)
            invoice_num = 10000 + self.id_offset + i
            tx_id = f"TXN-{20000 + self.id_offset + i}"

            # 10% chance of active chargeback hold
            has_chargeback = i % 10 == 0

            transactions.append(
                {
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
                }
            )
        return transactions

    def _generate_subscriptions(
        self,
        customers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        subscriptions = []
        for i, cust in enumerate(customers, start=1):
            plan_name, cycle, price = self.rng.choice(PLANS)
            start_days_ago = self.rng.randint(30, 365)
            start_date = self.current_date - timedelta(days=start_days_ago)

            # Annual plans have a 1-year lock-in period
            is_annual = cycle == "annual"
            lock_in_until = (start_date + timedelta(days=365)).isoformat() if is_annual else None

            # Some annual plans have an approved exception recorded
            has_exception = (i % 7 == 0) if is_annual else False

            subscriptions.append(
                {
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
                }
            )
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
            is_wrong = i % 4 == 0  # 25% of cases had incorrect agent actions (PS §3, §6)

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

            cases.append(
                {
                    "case_id": f"CASE-{8000 + self.id_offset + i}",
                    "customer_id": cust["id"],
                    "date": case_date.isoformat(),
                    "category": category,
                    "resolution": resolution,
                    "agent_id": f"AGT-{100 + (i % 5)}",
                    "notes": notes,
                    "was_correct": not is_wrong,
                    "evidence_used": ["DOC-1001"] if not is_wrong else ["DOC-0991"],
                }
            )
        return cases

    def _generate_supporting_documents(self) -> list[dict[str, Any]]:
        """Generates courier logs, shipping receipts, and FAQs."""
        return [
            {
                "id": "DOC-2001",
                "title": "Hardware Courier Delivery SOP",
                "category": "delivery_dispute",
                "updated_at": "2026-02-10T00:00:00Z",
                "content": "All physical shipments require signed proof of delivery (POD) uploaded to the carrier portal.",
            },
            {
                "id": "DOC-2002",
                "title": "Account Verification FAQ",
                "category": "account_security",
                "updated_at": "2026-03-01T00:00:00Z",
                "content": "Identity verification links remain valid for 48 hours and send an automated SMS challenge code.",
            },
        ]


def generate_world(seed: int = 42) -> dict[str, Any]:
    """Top-level deterministic world generator (PRD §8)."""
    return WorldGenerator(seed=seed).generate()
