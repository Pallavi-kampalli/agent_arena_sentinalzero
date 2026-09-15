from typing import Any

from sdk.tools_client import ToolsClient


def solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Autonomous customer support agent solving SupportOps tasks.

    Investigates account history and authoritative policies using read tools,
    handles server-side enforcement feedback on action tools, and returns
    a structured output matching the SupportOps Section 7 contract.

    Args:
        task: Dictionary containing 'task_id', 'customer_id', 'customer_message'.
        tools: Initialized ToolsClient for invoking SupportOps read and action tools.

    Returns:
        Structured dictionary conforming to the Section 7 contract.
    """
    task_id = task["task_id"]
    customer_id = task["customer_id"]
    message = task.get("customer_message", "").lower()

    evidence: list[str] = []
    uncertainties: list[str] = []

    # 1. Gather baseline customer profile
    try:
        cust_resp = tools.get_customer(customer_id)
        if "customer" in cust_resp and "id" in cust_resp["customer"]:
            evidence.append(cust_resp["customer"]["id"])
    except Exception as e:
        uncertainties.append(f"Failed to fetch customer record: {e}")

    # 2. Gather transaction history
    transactions = []
    try:
        tx_resp = tools.get_transactions(customer_id)
        transactions = tx_resp.get("transactions", [])
        for tx in transactions[:3]:
            if "id" in tx:
                evidence.append(tx["id"])
    except Exception as e:
        uncertainties.append(f"Failed to fetch transactions: {e}")

    # 3. Retrieve relevant policies via knowledge search
    policy_doc_id = None
    try:
        search_resp = tools.search_knowledge("refund cancellation dispute policy", top_k=3)
        results = search_resp.get("results", [])
        if results:
            policy_doc_id = results[0]["id"]
            evidence.append(policy_doc_id)
            # Read full document
            doc_resp = tools.get_document(policy_doc_id)
            if "document" in doc_resp and "id" in doc_resp["document"]:
                evidence.append(doc_resp["document"]["id"])
    except Exception as e:
        uncertainties.append(f"Failed to retrieve policy document: {e}")

    # 4. Reason and decide action
    # Determine issue classification
    category = "billing"
    issue = "general_inquiry"
    severity = "medium"

    if "duplicate" in message or "charged twice" in message:
        issue = "duplicate_payment"
    elif "cancel" in message:
        category = "account"
        issue = "subscription_cancellation"
    elif "fraud" in message or "unauthorized" in message or "stolen" in message:
        category = "security"
        issue = "fraud_suspicion"
        severity = "high"
    elif "refund" in message:
        issue = "refund_request"

    resolution = "deny"
    escalation_required = False
    customer_response = "Thank you for contacting support. We have reviewed your account and records."

    # Execute action if appropriate and eligible
    if issue in ("duplicate_payment", "refund_request") and transactions:
        target_tx = transactions[0]
        tx_id = target_tx.get("id")
        amount = float(target_tx.get("amount", 0.0))

        # Attempt refund through server-side enforced action tool
        refund_result = tools.issue_refund(
            transaction_id=tx_id,
            amount=amount,
            reason=f"Customer requested refund for transaction {tx_id}",
        )

        if refund_result.get("error") == "INELIGIBLE":
            # Server enforcement rejected the refund (e.g. chargeback hold or outside policy window)
            reason_code = refund_result.get("reason", "policy_ineligible")
            ref = refund_result.get("policy_ref")
            if ref:
                evidence.append(ref)

            if "chargeback" in reason_code or "investigation" in reason_code:
                # Active dispute requires human specialist escalation
                esc_result = tools.escalate_case(
                    case_id=task_id,
                    team="billing_specialists",
                    reason=f"Active investigation blocks automatic refund: cited {tx_id} and {ref or 'policy'}",
                )
                if esc_result.get("status") == "escalated":
                    resolution = "escalate"
                    escalation_required = True
                    customer_response = (
                        "Your transaction is currently subject to an active inquiry and has been escalated "
                        "to our billing specialist team for manual review."
                    )
            else:
                resolution = "deny"
                customer_response = (
                    f"We are unable to process a refund for this transaction under policy rules: {reason_code}."
                )
        else:
            # Refund succeeded
            resolution = "refund"
            customer_response = f"Your refund of ${amount:.2f} for transaction {tx_id} has been processed successfully."

    elif issue == "fraud_suspicion":
        # Request verification as safe fallback
        tools.request_verification(customer_id, verification_type="identity")
        resolution = "request_info"
        customer_response = "To protect your account security, please complete identity verification."

    # Return final structured output conforming to Section 7 contract
    # Ensure evidence IDs are unique and sorted
    unique_evidence = sorted(list(set(evidence)))

    return {
        "case_classification": {
            "category": category,
            "issue": issue,
            "severity": severity,
        },
        "decision": {
            "resolution": resolution,
            "escalation_required": escalation_required,
        },
        "evidence": unique_evidence,
        "uncertainties": uncertainties,
        "customer_response": customer_response,
        "confidence": 0.85,
    }
