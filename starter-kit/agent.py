"""SentinelZero — AI Cyber Detective Participant Agent Implementation.

This module is the starting template for participants competing in SentinelZero.
Your goal is to investigate suspicious communication threads using the 9 SentinelZero
read and defensive action tools and determine the defensive triage decision:
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

    task:
        The task supplied by SentinelZero:
        - 'task_id': unique identifier for the assigned task
        - 'customer_id': employee / recipient user identifier
        - 'customer_message': raw subject and email/message body to investigate

    tools:
        ToolsClient exposing all 9 participant-facing SentinelZero tools:
        Read tools (investigation):
            - tools.lookup_directory(identifier)
            - tools.get_approved_domains()
            - tools.get_email_headers(message_id)
            - tools.inspect_domain_reputation(domain)
            - tools.get_thread_history(thread_id)
        Action tools (server-side enforced defensive actions):
            - tools.allow_and_deliver(message_id, reason)
            - tools.apply_warning_banner(message_id, banner_type, reason)
            - tools.quarantine_message(message_id, reason)
            - tools.escalate_to_tier2_soc(message_id, reason)

    api_key:
        Active Google GenAI API key selected via round-robin rotation from .env.

    model:
        Target LLM model name (defaults to GEMINI_MODEL in .env or 'gemini-3.5-flash-lite').

    base_url:
        Optional custom base URL for Google / Gemini endpoint (from GEMINI_BASE_URL in .env).

    Participants are responsible for deciding:
        - which tools to use to gather evidence (EMP-*, DOM-*, MSG-*, THR-*, POL-*, LOG-*)
        - how to analyze impersonation, phishing, spoofing, or prompt injection
        - which defensive action to trigger via action tools
        - what final response to submit per Section 7 output contract:
            {
                "case_classification": {
                    "category": "cybersecurity_triage",
                    "issue": "phishing | impersonation | prompt_injection | legitimate",
                    "severity": "low | medium | high | critical"
                },
                "decision": {
                    "resolution": "allow | warn | quarantine | escalate",
                    "escalation_required": False | True
                },
                "evidence": ["DOM-...", "EMP-..."],
                "uncertainties": [],
                "customer_response": "Action explanation and guidance for the user.",
                "confidence": 0.95
            }
    """
    raise NotImplementedError(
        "Participant agent logic not implemented. Implement your agent investigation and triage logic in starter-kit/agent.py"
    )
