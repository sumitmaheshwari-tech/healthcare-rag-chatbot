"""Lightweight regex-based intent classifier for skip-search optimization.

Classifies user queries into intent categories to allow the agent to skip
unnecessary tool calls (e.g. RAG search for simple greetings).
Runs in <1ms using pre-compiled regex patterns.
"""

import re
from typing import Dict

# ── Pre-compiled regex patterns (compiled once at import time) ────────────

_CHITCHAT_PATTERN = re.compile(
    r"\b("
    r"h(i|ello|ey|owdy|ola)"
    r"|good\s*(morning|afternoon|evening|night|day)"
    r"|what'?s\s*up"
    r"|sup\b"
    r"|yo\b"
    r"|how\s*are\s*you"
    r"|how'?s\s*it\s*going"
    r"|thank(s|\s*you)?"
    r"|bye|goodbye|see\s*you|take\s*care"
    r"|nice\s*to\s*meet"
    r"|pleased\s*to\s*meet"
    r"|ok(ay)?"
    r"|sure|great|cool|cheers"
    r")\b",
    re.IGNORECASE,
)

_BOOKING_PATTERN = re.compile(
    r"("
    r"book\s*(an?\s*)?(appointment|slot|session|consultation|visit)"
    r"|schedule\s*(an?\s*)?(appointment|slot|session|consultation|visit)"
    r"|cancel\s*(my\s*)?(appointment|slot|session|consultation|visit|booking)"
    r"|reschedule\s*(my\s*)?(appointment|slot|session|consultation|visit|booking)"
    r"|available\s*(slots?|times?|appointments?|doctors?)"
    r"|doctor\s*availab"
    r"|when\s*(is|are)\s*(dr\.?|doctor)\s*\w+\s*available"
    r"|i\s*(want|need|would\s*like)\s*to\s*(book|schedule|cancel|reschedule)"
    r"|appointment"
    r"|make\s*(an?\s*)?(booking|reservation)"
    r"|check\s*(doctor\s*)?availability"
    r")",
    re.IGNORECASE,
)

_PROFILE_PATTERN = re.compile(
    r"("
    r"my\s*(name|profile|details|info|information|data|account)"
    r"|my\s*(appointment|appointments|booking|bookings)"
    r"|my\s*(bill|bills|billing|invoice|invoices|payment|payments)"
    r"|my\s*(record|records|medical\s*record|medical\s*records)"
    r"|my\s*(history|medical\s*history|health\s*history)"
    r"|my\s*(prescription|prescriptions|medication|medications)"
    r"|my\s*(report|reports|test\s*result|test\s*results|lab\s*result)"
    r"|show\s*me\s*my"
    r"|what\s*(is|are)\s*my"
    r"|who\s*am\s*i"
    r"|patient\s*(info|profile|details)"
    r"|view\s*(my|patient)\s*(profile|details|info)"
    r")",
    re.IGNORECASE,
)

_DIRECT_ACTION_PATTERN = re.compile(
    r"("
    r"\blog\s*in\b"
    r"|\blogin\b"
    r"|\blog\s*out\b"
    r"|\blogout\b"
    r"|\bsign\s*(in|up|out)\b"
    r"|\bregister\b"
    r"|\bcreate\s*(an?\s*)?account\b"
    r"|\bverify\b"
    r"|\bverification\b"
    r"|\bauthenticat"
    r"|\breset\s*(my\s*)?password\b"
    r"|\bforgot\s*(my\s*)?password\b"
    r")",
    re.IGNORECASE,
)

_HOSPITAL_INFO_PATTERN = re.compile(
    r"("
    r"(what|which|list|tell|show|how many)\s*.*(department|departments|specialt|specialit|division)"
    r"|department\s*(list|available|do you have|does the hospital)"
    r"|(what|which)\s*doctors?"
    r"|(who|which)\s*(are|is)\s*(the|your)\s*doctors?"
    r"|list\s*(of\s*)?(all\s*)?(doctors?|departments?|specialt)"
    r"|(what|tell)\s*.*\s*(facilities|services|visiting\s*hours|timings|address|location|contact)"
    r"|hospital\s*(info|information|details|about|overview)"
    r"|(about|describe)\s*(the|this|your)\s*hospital"
    r")",
    re.IGNORECASE,
)


def classify_intent(query: str) -> Dict[str, object]:
    """Classify the user query intent using pre-compiled regex patterns.

    Args:
        query: The raw user message text.

    Returns:
        A dict with ``"intent"`` (str) and ``"confidence"`` (float).
        Confidence is 0.95 for a regex match, 0.50 for the default fallback.
    """
    text = query.strip()

    if not text:
        return {"intent": "CHITCHAT", "confidence": 0.90}

    # Order matters: check most specific patterns first
    if _DIRECT_ACTION_PATTERN.search(text):
        return {"intent": "DIRECT_ACTION", "confidence": 0.95}

    if _BOOKING_PATTERN.search(text):
        return {"intent": "BOOKING_INTENT", "confidence": 0.95}

    if _HOSPITAL_INFO_PATTERN.search(text):
        return {"intent": "HOSPITAL_INFO", "confidence": 0.95}

    if _PROFILE_PATTERN.search(text):
        return {"intent": "PROFILE_QUERY", "confidence": 0.95}

    # Chitchat uses fullmatch-style (anchored) so it only fires on pure greetings
    if _CHITCHAT_PATTERN.match(text):
        return {"intent": "CHITCHAT", "confidence": 0.95}

    return {"intent": "GENERAL_KNOWLEDGE", "confidence": 0.50}
