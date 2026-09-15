"""Standard authoritative and stale policies for SupportOps.

Stale Policy Architecture (5 distinct documents across 6 task families):
There are five named stale policy documents defined across the SupportOps domain:
- DOC-0991: Legacy Refund Policy (used by 'refund_request' AND intentionally reused by
  'previous_agent_was_wrong' because the historical ticket precedent models an erroneous
  refund granted under legacy rules that bypassed chargeback holds).
- DOC-0992: Legacy Duplicate Billing Guidelines (used by 'duplicate_payment')
- DOC-0993: Legacy Subscription Policy (used by 'subscription_cancellation')
- DOC-0994: Legacy Non-Receipt Customer Resolution (used by 'delivery_dispute')
- DOC-0995: Legacy Fraud Handling Guidelines (used by 'account_lock_fraud')
"""

POLICIES: list[dict] = [
    {
        "id": "DOC-1001",
        "title": "Authoritative Customer Refund Policy",
        "category": "refund",
        "version": "2.1",
        "updated_at": "2026-06-01T00:00:00Z",
        "is_authoritative": True,
        "rules": {
            "refund_window_days": 30,
            "max_automated_amount": 500.0,
            "allow_during_chargeback": False,
        },
        "content": (
            "Section 1: General Refund Eligibility.\n"
            "Customers may request a refund for eligible purchases within thirty (30) days "
            "of the original transaction date. Requests beyond 30 days are ineligible for automatic refund.\n"
            "Section 2: Maximum Amount.\n"
            "Automated support agents may issue refunds up to $500.00 USD per transaction. "
            "Amounts exceeding $500.00 require human billing specialist escalation.\n"
            "Section 3: Duplicate Charges.\n"
            "Legitimate duplicate transactions will be refunded in full for the secondary transaction.\n"
            "Section 4: Active Investigations.\n"
            "Transactions with an active chargeback, fraud dispute, or external bank inquiry "
            "cannot be refunded automatically under any circumstances (see DOC-1842)."
        ),
    },
    {
        "id": "DOC-1842",
        "title": "Fraud, Chargeback and Active Dispute Restrictions",
        "category": "dispute_hold",
        "version": "2.0",
        "updated_at": "2026-01-10T00:00:00Z",
        "is_authoritative": True,
        "rules": {
            "allow_during_chargeback": False,
            "escalation_team": "billing_specialists",
        },
        "content": (
            "Section 1: Scope.\n"
            "This policy applies to all accounts and transactions with pending or active bank disputes.\n"
            "Section 2: Active Chargeback Hold.\n"
            "Policy DOC-1842 §4: Customers with an active fraud or chargeback investigation "
            "cannot receive an automatic refund. Any agent receiving an ineligibility code "
            "'chargeback_investigation_active' must escalate the case to billing_specialists citing "
            "DOC-1842 and the relevant transaction ID. Do not promise an immediate refund."
        ),
    },
    {
        "id": "DOC-1002",
        "title": "Duplicate Payment and Billing Reconciliation Policy",
        "category": "duplicate_payment",
        "version": "1.4",
        "updated_at": "2026-05-15T00:00:00Z",
        "is_authoritative": True,
        "rules": {
            "match_fields": ["amount", "timestamp", "invoice_id"],
        },
        "content": (
            "Section 1: Duplicate Charge Criteria.\n"
            "A duplicate charge occurs when two or more transactions share the same invoice ID, "
            "identical amount, and timestamps within 10 minutes of each other on the same payment method.\n"
            "Section 2: Legitimate Multi-Transactions.\n"
            "Transactions with distinct invoice IDs (such as a subscription base renewal and an add-on "
            "license or usage charge) are legitimate separate billings and must not be refunded as duplicates. "
            "The agent must explain the breakdown of charges to the customer citing the invoice IDs."
        ),
    },
    {
        "id": "DOC-1003",
        "title": "Subscription Cancellation and Contractual Lock-in Policy",
        "category": "cancellation",
        "version": "3.0",
        "updated_at": "2026-07-01T00:00:00Z",
        "is_authoritative": True,
        "rules": {
            "enforce_lock_in": True,
            "exception_requires_approval": True,
        },
        "content": (
            "Section 1: Standard Cancellation.\n"
            "Customers on month-to-month plans may cancel at any time with immediate effect.\n"
            "Section 2: Contractual Lock-in Period.\n"
            "Annual and discounted term plans include a contractual lock-in commitment. "
            "Subscriptions within an active lock-in period cannot be cancelled unless an approved "
            "exception has been recorded on the account by account management. In the absence of an "
            "approved exception, cancellation must be denied or escalated to retention_specialists.\n"
            "Section 3: Unresolved Disputes.\n"
            "Accounts with open payment disputes cannot cancel until the dispute is resolved."
        ),
    },
    {
        "id": "DOC-1004",
        "title": "Physical Delivery and Courier Dispute Handling",
        "category": "delivery_dispute",
        "version": "2.0",
        "updated_at": "2026-04-10T00:00:00Z",
        "is_authoritative": True,
        "rules": {
            "require_courier_investigation_if_signed": True,
        },
        "content": (
            "Section 1: Non-Receipt Claims.\n"
            "When a customer claims physical goods or hardware were not received, the agent must check "
            "the courier tracking record.\n"
            "Section 2: Signed Delivery Records.\n"
            "If courier records indicate delivery confirmed with signature or GPS verification at the customer's "
            "address, automatic refund is prohibited. The case must be escalated to logistics_investigations.\n"
            "Section 3: Confirmed Courier Loss.\n"
            "If the courier status indicates 'lost in transit' or 'delivery failed', the agent may issue a refund."
        ),
    },
    {
        "id": "DOC-1005",
        "title": "Account Security, Fraud Suspicion and Verification Protocol",
        "category": "account_security",
        "version": "1.8",
        "updated_at": "2026-03-20T00:00:00Z",
        "is_authoritative": True,
        "rules": {
            "require_verification_fallback": True,
        },
        "content": (
            "Section 1: Unrecognized Activity Reports.\n"
            "When a customer claims unauthorized activity, check login history and device fingerprints.\n"
            "Section 2: Safe Fallback.\n"
            "If customer identity or activity cannot be definitively confirmed, agents must invoke "
            "request_verification to initiate secondary MFA/identity verification before taking state actions.\n"
            "Section 3: Escalation.\n"
            "Confirmed compromise across multiple sessions must be escalated to security_operations."
        ),
    },
    # --- Stale / Superseded Policies ---
    {
        "id": "DOC-0991",
        "title": "Legacy Refund Policy (SUPERSEDED)",
        "category": "refund",
        "version": "1.0",
        "updated_at": "2024-01-15T00:00:00Z",
        "is_authoritative": False,
        "rules": {
            "refund_window_days": 60,
            "max_automated_amount": 1000.0,
            "allow_during_chargeback": True,
        },
        "content": (
            "SUPERSEDED BY DOC-1001 ON 2026-06-01.\n"
            "Old Section 1: Refunds were allowed within 60 days of purchase.\n"
            "Old Section 2: Automated refunds permitted up to $1,000.\n"
            "Old Section 3: Chargeback holds were not checked automatically.\n"
            "WARNING: Do NOT apply this policy to current transactions."
        ),
    },
    {
        "id": "DOC-0992",
        "title": "Legacy Duplicate Billing Guidelines (SUPERSEDED)",
        "category": "duplicate_payment",
        "version": "1.0",
        "updated_at": "2024-02-10T00:00:00Z",
        "is_authoritative": False,
        "rules": {
            "match_amount_only": True,
        },
        "content": (
            "SUPERSEDED BY DOC-1002 ON 2026-05-15.\n"
            "Old rule: Any two charges with identical amounts within 7 days should be automatically refunded "
            "as duplicate payments without checking invoice IDs or renewal breakdown.\n"
            "WARNING: Deprecated policy. Causes erroneous refunds on legitimate multi-product purchases."
        ),
    },
    {
        "id": "DOC-0993",
        "title": "Legacy Subscription Policy (SUPERSEDED)",
        "category": "cancellation",
        "version": "1.0",
        "updated_at": "2024-03-01T00:00:00Z",
        "is_authoritative": False,
        "rules": {
            "enforce_lock_in": False,
            "exception_requires_approval": False,
        },
        "content": (
            "SUPERSEDED BY DOC-1003 ON 2026-07-01.\n"
            "Old rule: Any customer could cancel at any time regardless of annual lock-in period.\n"
            "WARNING: Deprecated policy. No longer valid."
        ),
    },
    {
        "id": "DOC-0994",
        "title": "Legacy Non-Receipt Customer Resolution (SUPERSEDED)",
        "category": "delivery_dispute",
        "version": "1.0",
        "updated_at": "2024-04-01T00:00:00Z",
        "is_authoritative": False,
        "rules": {
            "immediate_refund_on_claim": True,
        },
        "content": (
            "SUPERSEDED BY DOC-1004 ON 2026-04-10.\n"
            "Old rule: Customer word of non-receipt was accepted without checking courier proof of delivery.\n"
            "WARNING: Deprecated policy. Requires courier delivery verification."
        ),
    },
    {
        "id": "DOC-0995",
        "title": "Legacy Fraud Handling Guidelines (SUPERSEDED)",
        "category": "account_security",
        "version": "1.0",
        "updated_at": "2023-11-01T00:00:00Z",
        "is_authoritative": False,
        "rules": {},
        "content": (
            "SUPERSEDED BY DOC-1005.\n"
            "Old rule: Immediately suspend all customer transactions upon receiving any message containing "
            "the word 'unrecognized'. Deprecated due to excessive false positives."
        ),
    },
]
