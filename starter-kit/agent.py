"""SentinelZero — AI Cyber Detective Participant Agent Implementation Template.

This module is the starting template for participants competing in SentinelZero.
Your goal is to investigate suspicious inbound communications, correlate security signals,
and determine the defensive triage decision:
    ALLOW, WARN, QUARANTINE, or ESCALATE.

Contract:
    solve(task: dict[str, Any], tools: ToolsClient, api_key: str | None = None, model: str | None = None, base_url: str | None = None) -> dict[str, Any]
"""

from typing import Any

from sdk.tools_client import ToolsClient


def solve(
    task: dict[str, Any],
    tools: ToolsClient,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Participant agent entry point.

    Args:
        task: The assigned task dictionary containing:
            - 'task_id': unique identifier for the assigned task instance
            - 'customer_id': employee / recipient user identifier
            - 'customer_message': formatted headers and message body to investigate
            - 'input_payload': raw message dict (message_id, thread_id, sender_email, etc.)

        tools: ToolsClient exposing all 9 participant-facing SentinelZero tools:
            Read Tools (Cybersecurity Investigation):
                - tools.lookup_directory(identifier: str)
                - tools.get_approved_domains()
                - tools.get_email_headers(message_id: str)
                - tools.inspect_domain_reputation(domain: str)
                - tools.get_thread_history(thread_id: str)

            Action Tools (Server-Side Enforced Defensive Actions):
                - tools.allow_and_deliver(message_id: str, reason: str)
                - tools.apply_warning_banner(message_id: str, banner_type: str, reason: str)
                - tools.quarantine_message(message_id: str, reason: str)
                - tools.escalate_to_tier2_soc(message_id: str, reason: str)

        api_key: Active Google Gemini API key selected via round-robin rotation from .env.
        model: Target LLM model name (defaults to GEMINI_MODEL in .env or 'gemini-3.5-flash-lite').
        base_url: Optional custom base URL for Gemini / Google API gateway.

    Returns:
        Structured dictionary matching the Section 7 Output Contract:
        {
            "case_classification": {
                "category": "phishing",        # 'phishing' | 'impersonation' | 'credential_harvesting' | 'malware' | 'prompt_injection' | 'legitimate'
                "issue": "spoofed_sender",     # Specific incident description
                "severity": "high"             # 'low' | 'medium' | 'high' | 'critical'
            },
            "decision": {
                "resolution": "quarantine",    # 'allow' | 'warn' | 'quarantine' | 'escalate'
                "escalation_required": False   # bool: True if Tier 2 SOC escalation required
            },
            "evidence": [                      # List of entity/document IDs observed during tool calls
                "MSG-HIDDEN-001",
                "DOM-MALICIOUS-004",
                "EMP-1002"
            ],
            "uncertainties": [],               # List of string doubts or gaps in evidence
            "customer_response": (             # Clear, actionable security advice for the recipient
                "This email originated from an unauthorized lookalike domain with failed SPF authentication. "
                "The email has been quarantined. Do not open attachments or click links."
            ),
            "confidence": 0.95                 # float: calibrated confidence between 0.0 and 1.0
        }
    """
    _ = api_key, model, base_url

    # Step 1: Extract task parameters
    task_id = str(task.get("task_id", ""))
    input_payload = task.get("input_payload") or {}
    message_id = str(input_payload.get("message_id", ""))
    sender_email = str(input_payload.get("sender_email", ""))

    # Step 2: Implement your investigation and tool execution logic here
    # Example:
    # headers = tools.get_email_headers(message_id)
    # approved = tools.get_approved_domains()
    # rep = tools.inspect_domain_reputation(sender_email.split("@")[-1])

    # Step 3: Implement defensive triage rules and take server-enforced action tool
    # Example:
    # tools.quarantine_message(message_id=message_id, reason="Malicious spoofed domain")

    # Step 4: Return structured response matching Section 7 output contract
    raise NotImplementedError(
        "Participant agent logic not implemented. "
        "Implement your autonomous investigation, tool actions, and decision in starter-kit/agent.py."
    )
