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


_OUT_OF_DOMAIN_PATTERN = re.compile(
    r"("
    # Coding / programming requests
    r"\b(write|create|generate|give\s*me|produce|show\s*me|make)\s*(a\s*)?(code|script|program|function|class|algorithm|regex|app)\b"
    r"|\b(debug|fix|explain)\s*(this|my)?\s*code\b"
    r"|\b(code|script)\s*(in|for|to)\b"
    r"|\b(coding|programming|software\s*development|web\s*development)\b"
    # Programming languages & tech tools
    r"|\bwhat\s*is\s*(java|python|c\+\+|c#|javascript|typescript|golang|rust|ruby|php|html|css|sql|react|angular|vue|nodejs|spring\s*boot|django|flask|docker|kubernetes)\b"
    r"|\b(explain|teach\s*me|how\s*to\s*use)\s*(java|python|c\+\+|javascript|typescript|html|css|sql|programming|git)\b"
    r"|\b(java|python|c\+\+|c#|javascript|typescript|golang|rust|ruby|php|html|css|sql)\s*(code|program|script|syntax|tutorial|function|class|developer)\b"
    # General non-medical academic / homework / trivia
    r"|\b(solve|calculate)\s*(this\s*)?(math|equation|integral|derivative|algebra)\b"
    r"|\bwrite\s*(an?\s*)?(essay|poem|song|story|lyrics)\b"
    r"|\bwho\s*won\s*the\s*(world\s*cup|match|election|war)\b"
    r"|\bcapital\s*of\s*[a-z\s]+"
    # Jailbreaks, prompt injections, and persona overrides
    r"|\b(ignore\s*(all\s*)?(previous|prior|above)\s*instructions|pretend\s*(you\s*are|to\s*be)|act\s*as\s*(a\s*)?(developer|hacker|unrestricted)|jailbreak|dan\s*mode|developer\s*mode|system\s*prompt|reveal\s*(your\s*)?instructions)\b"
    r"|\b(forget\s*your\s*rules|disregard\s*(all\s*)?instructions|bypass\s*safety)\b"
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

    # Guardrail check first: reject out-of-scope / coding requests immediately
    if _OUT_OF_DOMAIN_PATTERN.search(text):
        return {"intent": "OUT_OF_DOMAIN", "confidence": 0.99}

    # Order matters: check most specific patterns next
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
