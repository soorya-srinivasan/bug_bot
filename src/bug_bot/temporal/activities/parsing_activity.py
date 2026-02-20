import re

from temporalio import activity

from bug_bot.temporal import BugReportInput, ParsedBug

# Fallback keyword -> service mapping used only when AI matching fails or returns empty
_FALLBACK_KEYWORDS = {
    "payment": "Payment.API",
    "bill": "Bill.API",
    "invoice": "Bill.API",
    "inventory": "Inventory.API",
    "stock": "Inventory.API",
    "auth": "Auth.Server",
    "login": "Auth.Server",
    "subscription": "Subscription.API",
    "company": "Company.API",
    "vconnect": "vconnect",
    "aft": "vconnect-aft",
    "audit": "vconnect-audit",
}

SEVERITY_PATTERNS = {
    "P0": [r"\bcritical\b", r"\bdown\b", r"\boutage\b", r"\bblocking\b", r"\ball users\b"],
    "P1": [r"\burgent\b", r"\bsevere\b", r"\bmajor\b", r"\bproduction\b"],
    "P2": [r"\bbug\b", r"\bissue\b", r"\berror\b", r"\bfailing\b"],
}


@activity.defn
async def parse_bug_report(input: BugReportInput) -> ParsedBug:
    """Parse a raw bug report to extract severity, services, and keywords."""
    text_lower = input.message_text.lower()

    # Detect severity
    severity = "P3"  # default
    for sev, patterns in SEVERITY_PATTERNS.items():
        if any(re.search(p, text_lower) for p in patterns):
            severity = sev
            break

    # AI-powered service detection
    services = []
    try:
        from bug_bot.service_matcher import match_services
        services = await match_services(input.message_text)
    except Exception as e:
        activity.logger.warning(f"AI service matching failed for {input.bug_id}: {e}")

    # Fall back to keyword matching if AI returned nothing
    if not services:
        for keyword, service in _FALLBACK_KEYWORDS.items():
            if keyword in text_lower and service not in services:
                services.append(service)

    # Extract keywords (simple approach)
    keywords = re.findall(r"\b(?:error|exception|timeout|500|404|null|crash|slow|fail)\b", text_lower)

    activity.logger.info(f"Parsed bug {input.bug_id}: severity={severity}, services={services}")

    return ParsedBug(
        bug_id=input.bug_id,
        severity=severity,
        relevant_services=services,
        keywords=list(set(keywords)),
    )
