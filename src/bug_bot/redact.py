"""Redact / rephrase developer messages before they are posted to reporter-facing channels.

The #bug-summaries channel is internal, so it always receives the original text.
The #bug-reports channel is reporter-facing; closure reasons are passed through this
module to strip PII and sensitive org information while keeping the core reason intact.
"""

import logging
import re

import anthropic

from bug_bot.config import settings

logger = logging.getLogger(__name__)

# Regex-based fallback: redact the most obvious PII patterns when Claude is unavailable.
_PII_RE = re.compile(
    r'\b\d{10,}\b'                      # long digit strings (phone numbers, numeric IDs)
    r'|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b'  # formatted phone numbers e.g. 950-055-3377
    r'|[\w.+\-]+@[\w\-]+\.\w+'          # email addresses
    r'|\buser_\w+\b'                     # internal user handle patterns (user_010 etc.)
    r'|\b[A-Z]{2,}\d{4,}\b',            # internal ID patterns e.g. ORD12345
    re.IGNORECASE,
)

_REDACT_SYSTEM_PROMPT = """\
You are a privacy and information-security filter for a customer-support bug-tracking system.

A developer has written a message explaining why a bug report is being closed. \
You must produce a safe version of that message for the **reporter-facing** channel.

Rules:
1. Remove ALL personally-identifiable information (PII): phone numbers, email addresses, \
   user/account/customer IDs, numeric identifiers.
2. Remove sensitive internal org information: database table names, internal service names, \
   internal process names, system internals the customer should not know about.
3. Preserve the core reason in clear, professional, plain language (e.g. "The account is \
   currently restricted and cannot receive OTP messages." is fine).
4. Keep the output to 1–2 sentences.
5. Return ONLY the rephrased text. No preamble, no quotes, no explanation."""


async def redact_for_reporters(text: str) -> str:
    """Return a PII-free, reporter-safe version of a developer closure message.

    Uses Claude for intelligent redaction/rephrasing.  Falls back to a
    regex-based redaction if the API key is absent or the call fails.
    """
    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key — falling back to regex redaction.")
        return _regex_redact(text)

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=_REDACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Redaction API call failed — falling back to regex redaction.")
        return _regex_redact(text)


def _regex_redact(text: str) -> str:
    """Lightweight fallback: replace common PII patterns with [redacted]."""
    return _PII_RE.sub("[redacted]", text)
