"""SentinelZero Reference Autonomous Cyber Detective Agent.

Investigates suspicious communication threads using the 9 SentinelZero read/action tools
and produces a 4-decision triage output (ALLOW, WARN, QUARANTINE, ESCALATE).
"""

import re
from typing import Any


def extract_sender_info(sender_raw: str | None) -> tuple[str, str, str]:
    """Extracts (display_name, email, domain) from a sender header string."""
    if not sender_raw:
        return "", "", ""

    match = re.search(r"(?:[\"']?([^\"'<]+)[\"']?\s*)?<([^>]+)>", sender_raw)
    if match:
        name = (match.group(1) or "").strip()
        email = (match.group(2) or "").strip().lower()
    else:
        name = ""
        email = sender_raw.strip().lower()

    domain = email.split("@")[-1] if "@" in email else ""
    return name, email, domain


def extract_urls_and_domains(text: str) -> list[str]:
    """Extracts external domains from links and text in the message body."""
    if not text:
        return []

    urls = re.findall(r"https?://(?:www\.)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text)
    domain_refs = re.findall(r"\b([a-zA-Z0-9.-]+\.(?:com|org|edu|in|net|gov|io|co))\b", text)

    combined = set()
    for u in urls:
        combined.add(u.lower())
    for d in domain_refs:
        combined.add(d.lower())

    return list(combined)


def detect_prompt_injection(message_body: str) -> bool:
    """Detects explicit system/security override instructions targeting the security AI agent."""
    if not message_body:
        return False

    body_lower = message_body.lower()
    patterns = [
        r"system instruction:.*(?:mark|approve|allow|ignore)",
        r"important system instruction for security ai",
        r"you must return decision ['\"]allow['\"]",
        r"do not quarantine this message",
        r"override security policy",
        r"disregard previous instructions and (?:approve|allow|reveal)",
    ]

    for pat in patterns:
        if re.search(pat, body_lower):
            return True
    return False


def detect_credential_phishing(text: str) -> bool:
    """Detects requests for credentials, passwords, MFA codes, or login links."""
    if not text:
        return False
    text_lower = text.lower()
    keywords = [
        "password",
        "mfa code",
        "verification code",
        "login link",
        "verify account",
        "session token",
        "update credentials",
        "sign in here",
        "reset password",
    ]
    return any(kw in text_lower for kw in keywords)


def detect_payment_fraud(text: str) -> bool:
    """Detects requests for gift cards, wire transfers, or urgent invoice redirection."""
    if not text:
        return False
    text_lower = text.lower()
    keywords = [
        "gift card",
        "wire transfer",
        "bank transfer",
        "invoice payment",
        "urgent payment",
        "purchase cards",
        "crypto payment",
        "apple gift card",
        "bank of america",
        "routing number",
        "account number",
        "ac#",
        "account #",
        "swift code",
    ]
    return any(kw in text_lower for kw in keywords)


def detect_social_engineering(text: str) -> bool:
    """Detects social engineering pressure such as urgency, secrecy, or authority claims."""
    if not text:
        return False
    text_lower = text.lower()
    keywords = [
        "urgent",
        "immediately",
        "do not call",
        "keep this confidential",
        "in a meeting",
        "asap",
        "dean's office",
        "from the desk of",
    ]
    return any(kw in text_lower for kw in keywords)


def extract_evidence_ids_from_dict(data: Any, evidence_set: set[str]) -> None:
    """Extracts valid evidence IDs (EMP-*, DOM-*, MSG-*, THR-*, POL-*, LOG-*) from dict/string."""
    pattern = re.compile(r"\b(EMP-\w+|DOM-\w+|MSG-\w+|THR-\w+|POL-\w+|LOG-\w+)\b")
    data_str = str(data)
    matches = pattern.findall(data_str)
    for m in matches:
        evidence_set.add(m)


def solve(task: dict[str, Any], tools: Any) -> dict[str, Any]:
    """Reference solution agent entry point for SentinelZero cybersecurity triage.

    Investigates suspicious communication threads and determines the defensive action:
    ALLOW, WARN, QUARANTINE, or ESCALATE.
    """
    task_id = task.get("task_id", "UNKNOWN-TASK")
    inp = task.get("input_payload") or task

    message_id = inp.get("message_id") or task.get("message_id") or f"MSG-{task_id}"
    thread_id = inp.get("thread_id") or task.get("thread_id")
    sender_raw = inp.get("sender") or inp.get("sender_email") or task.get("sender", "")
    recipient = inp.get("recipient") or task.get("recipient", "")
    subject = inp.get("subject") or task.get("subject", "")
    message_body = inp.get("message_body") or inp.get("body") or task.get("customer_message", "")

    collected_evidence: set[str] = set()
    if message_id:
        collected_evidence.add(message_id)

    # 1. Prompt Injection Detection
    prompt_injection_detected = detect_prompt_injection(message_body)

    # 2. Sender Identity & Directory Inspection
    display_name, sender_email, sender_domain = extract_sender_info(sender_raw)
    employee_record = None

    if sender_email:
        dir_resp = tools.lookup_directory(sender_email)
        extract_evidence_ids_from_dict(dir_resp, collected_evidence)
        if dir_resp.get("found"):
            employee_record = dir_resp.get("employee")

    if not employee_record and display_name:
        dir_resp_name = tools.lookup_directory(display_name)
        extract_evidence_ids_from_dict(dir_resp_name, collected_evidence)
        if dir_resp_name.get("found"):
            employee_record = dir_resp_name.get("employee")

    # 3. Approved Domains & Reputation Inspection
    approved_resp = tools.get_approved_domains()
    extract_evidence_ids_from_dict(approved_resp, collected_evidence)
    official_domains = set(approved_resp.get("official_domains", []))
    partner_domains = set(approved_resp.get("partner_domains", []))

    domain_reputation = "unknown"
    is_malicious_domain = False
    is_lookalike_domain = False

    if sender_domain and sender_domain not in official_domains:
        domain_resp = tools.inspect_domain_reputation(sender_domain)
        extract_evidence_ids_from_dict(domain_resp, collected_evidence)
        domain_reputation = domain_resp.get("reputation", "unknown")
        if domain_reputation == "malicious":
            is_malicious_domain = True
        if domain_resp.get("lookalike_of"):
            is_lookalike_domain = True

    extracted_domains = extract_urls_and_domains(message_body)
    for ext_dom in extracted_domains:
        if ext_dom not in official_domains and ext_dom != sender_domain:
            ext_rep = tools.inspect_domain_reputation(ext_dom)
            extract_evidence_ids_from_dict(ext_rep, collected_evidence)
            if ext_rep.get("reputation") == "malicious":
                is_malicious_domain = True
            if ext_rep.get("lookalike_of"):
                is_lookalike_domain = True

    # 4. Email Headers Inspection
    header_resp = tools.get_email_headers(message_id)
    extract_evidence_ids_from_dict(header_resp, collected_evidence)
    auth_results = header_resp.get("auth_results", {})
    failed_auth = any(v == "fail" for v in auth_results.values())

    # 5. Thread History Inspection
    thread_history = []
    if thread_id:
        thread_resp = tools.get_thread_history(thread_id)
        extract_evidence_ids_from_dict(thread_resp, collected_evidence)
        thread_history = thread_resp.get("messages", [])

    full_thread_text = message_body + "\n" + "\n".join([m.get("body", "") for m in thread_history if isinstance(m, dict)])

    # 6. Threat Indicators Analysis
    has_credential_phish = detect_credential_phishing(full_thread_text)
    has_payment_fraud = detect_payment_fraud(full_thread_text)
    has_social_eng = detect_social_engineering(full_thread_text)

    # 7. Decision Logic
    decision = "ALLOW"
    confidence = 0.95
    severity = "medium"
    summary = ""

    if prompt_injection_detected:
        decision = "QUARANTINE"
        severity = "high"
        confidence = 0.95
        summary = (
            f"Quarantined message {message_id}: Detected direct system instruction override attempt targeting "
            f"security AI agent in message body."
        )
        tools.quarantine_message(message_id, summary)

    elif employee_record and (is_malicious_domain or (has_credential_phish and sender_domain not in official_domains)):
        decision = "ESCALATE"
        severity = "critical"
        confidence = 0.90
        evidence_ref = next(iter(sorted(collected_evidence)), message_id)
        summary = (
            f"Escalated incident to Tier-2 SOC for message {message_id}: Verified employee {employee_record.get('id')} "
            f"appears compromised, sending malicious links or phishing content (Ref: {evidence_ref})."
        )
        tools.escalate_to_tier2_soc(message_id, summary)

    elif is_malicious_domain or is_lookalike_domain:
        decision = "QUARANTINE"
        severity = "high"
        confidence = 0.95
        summary = (
            f"Quarantined message {message_id}: Sender domain or linked domain '{sender_domain}' flagged as "
            f"malicious/lookalike domain."
        )
        tools.quarantine_message(message_id, summary)

    elif has_credential_phish or (has_payment_fraud and not employee_record):
        decision = "QUARANTINE"
        severity = "high"
        confidence = 0.90
        summary = (
            f"Quarantined message {message_id}: Detected phishing indicators (credential/session harvesting or "
            f"unverified payment request)."
        )
        tools.quarantine_message(message_id, summary)

    elif (sender_domain not in official_domains and sender_domain not in partner_domains) or failed_auth or has_social_eng:
        decision = "WARN"
        severity = "medium"
        confidence = 0.85
        summary = (
            f"Applied warning banner to message {message_id}: External sender '{sender_domain}' or unverified "
            f"social engineering pressure detected."
        )
        tools.apply_warning_banner(message_id, "EXTERNAL_SENDER", summary)

    else:
        decision = "ALLOW"
        severity = "low"
        confidence = 0.95
        summary = (
            f"Allowed and delivered message {message_id}: Verified legitimate sender identity and content "
            f"from domain '{sender_domain}'."
        )
        tools.allow_and_deliver(message_id, summary)

    if not collected_evidence:
        collected_evidence.add(message_id)

    sorted_evidence = sorted(list(collected_evidence))

    return {
        "task_id": task_id,
        "case_classification": {
            "category": "cybersecurity_triage",
            "issue": "triage",
            "severity": severity,
        },
        "decision": {
            "resolution": decision.lower(),
            "escalation_required": (decision == "ESCALATE"),
        },
        "evidence": sorted_evidence,
        "uncertainties": [],
        "customer_response": summary,
        "summary": summary,
        "confidence": confidence,
        "prompt_injection_detected": prompt_injection_detected,
    }
