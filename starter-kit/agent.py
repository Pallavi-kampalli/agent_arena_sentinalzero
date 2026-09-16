from typing import Any

from sdk.tools_client import ToolsClient


def solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Participant agent entry point.

    task:
        The task supplied by Agent Arena:
        - 'task_id': unique identifier for the assigned task
        - 'customer_id': identifier of customer requesting support
        - 'customer_message': customer's inbound inquiry / problem description

    tools:
        ToolsClient exposing all 10 participant-facing SupportOps tools:
        Read tools:
            - tools.search_knowledge(query, top_k=5)
            - tools.get_document(document_id)
            - tools.get_customer(customer_id)
            - tools.get_transactions(customer_id, start_date=None, end_date=None)
            - tools.get_subscription(customer_id)
            - tools.get_previous_cases(customer_id, limit=5)
        Action tools (server-side enforced):
            - tools.issue_refund(transaction_id, amount, reason)
            - tools.cancel_subscription(customer_id, subscription_id)
            - tools.escalate_case(case_id, team, reason)
            - tools.request_verification(customer_id, verification_type="identity")

    Participants are responsible for deciding:
        - which tools to use
        - how to reason about the task
        - what evidence to collect
        - what action to take
        - what final response to submit per Section 7 output contract
    """
    raise NotImplementedError
