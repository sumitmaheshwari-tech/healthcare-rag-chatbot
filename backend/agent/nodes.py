"""Agent graph nodes with LLM failover gateway and retries."""

import os
import sys
import time
import asyncio

try:
    import pip_system_certs.wrapt_requests
except Exception:
    pass

from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, AIMessage

def sanitize_messages_for_llm(messages):
    """
    Sanitize history for LLMs (especially Gemini):
    1. Removes ToolMessages and tool-calling AIMessages from completed turns.
    2. Keeps plain HumanMessages and text-only AIMessages.
    3. Keeps all messages in the current active turn (from the last HumanMessage onwards)
       so the active tool execution loop works.
    """
    sanitized = []
    
    # Identify the index of the last HumanMessage
    last_human_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human_idx = i
            break
            
    for i, msg in enumerate(messages):
        # Always skip SystemMessages from the history; we will prepend the fresh injected one
        if isinstance(msg, SystemMessage):
            continue
            
        if i < last_human_idx:
            # Completed turns: Skip ToolMessages and AIMessages with tool calls
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                continue
            if isinstance(msg, ToolMessage):
                continue
            sanitized.append(msg)
        else:
            # Active turn: Keep everything (HumanMessage, AIMessage with tool calls, ToolMessages)
            sanitized.append(msg)
            
    return sanitized


def flatten_tool_calls(messages):
    """
    Convert ToolMessages and tool-calling AIMessages into plain text messages.
    This eliminates API-specific tool call IDs and constraints, guaranteeing
    cross-compatibility and strict role alternation for Gemini/OpenAI failover.
    """
    flat = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            flat.append(msg)
        elif isinstance(msg, HumanMessage):
            flat.append(msg)
        elif isinstance(msg, AIMessage):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                text_content = msg.content if isinstance(msg.content, str) and msg.content else "Checking hospital records..."
                flat.append(AIMessage(content=text_content))
            else:
                flat.append(msg)
        elif isinstance(msg, ToolMessage):
            flat.append(HumanMessage(content=f"[Retrieved Hospital Database Fact]\n{msg.content}"))

    # Clean up and merge consecutive messages of the same role for strict API compliance
    cleaned = []
    for m in flat:
        if cleaned and type(cleaned[-1]) == type(m) and not isinstance(m, SystemMessage):
            cleaned[-1] = type(m)(content=str(cleaned[-1].content) + "\n" + str(m.content))
        else:
            cleaned.append(m)
    return cleaned


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.prompts import SYSTEM_PROMPT
from config import settings

# Module-level set to remember providers that failed with auth errors (persists for server lifetime)
_disabled_providers = set()


def get_fallback_llm(provider: str):
    """Dynamically instantiate fallback LLM based on provider string."""
    try:
        if provider == "nvidia" and settings.NVIDIA_API_KEY:
            try:
                from langchain_nvidia_ai_endpoints import ChatNVIDIA
                return ChatNVIDIA(
                    model=settings.NVIDIA_LLM_MODEL,
                    api_key=settings.NVIDIA_API_KEY,
                    temperature=0,
                )
            except ImportError:
                from langchain_openai import ChatOpenAI
                return ChatOpenAI(
                    model=settings.NVIDIA_LLM_MODEL,
                    api_key=settings.NVIDIA_API_KEY,
                    base_url="https://integrate.api.nvidia.com/v1",
                    temperature=0,
                    request_timeout=60,
                )
        elif provider == "gemini" and settings.GOOGLE_API_KEY:
            from langchain_google_genai import ChatGoogleGenerativeAI
            model_name = settings.LLM_MODEL.replace("models/", "")
            return ChatGoogleGenerativeAI(
                model=model_name,
                google_api_key=settings.GOOGLE_API_KEY,
                temperature=0,
                timeout=30,
            )
        elif provider == "ollama":
            from langchain_ollama import ChatOllama
            return ChatOllama(
                model=settings.OLLAMA_MODEL,
                base_url=settings.OLLAMA_BASE_URL,
                temperature=0,
                num_ctx=4096,
            )
        elif provider == "openrouter" and settings.OPENROUTER_API_KEY:
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=settings.OPENROUTER_MODEL,
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
                temperature=0,
                request_timeout=45,
                default_headers={
                    "HTTP-Referer": "http://localhost:8000",
                    "X-Title": "MedCare Chatbot",
                }
            )
    except Exception as e:
        print(f"[FAILOVER] Error importing or setting up provider '{provider}': {e}")
    return None


async def agent_node(state, config, primary_llm, tools):
    """Invoke the LLM on the trimmed message history with automatic retry and failover."""
    messages = list(state["messages"])

    # 1. Sanitize messages to prevent Gemini turn errors and reduce token bloat
    sanitized_messages = sanitize_messages_for_llm(messages)

    # 2. Message Trimming (History Window)
    MAX_HISTORY = 10
    if len(sanitized_messages) > MAX_HISTORY:
        trimmed_messages = sanitized_messages[-MAX_HISTORY:]
    else:
        trimmed_messages = sanitized_messages

    # Ensure message history always starts with a HumanMessage to prevent orphaned Tool/AI turns for Gemini
    first_human_idx = -1
    for idx, msg in enumerate(trimmed_messages):
        if isinstance(msg, HumanMessage):
            first_human_idx = idx
            break
    if first_human_idx != -1:
        trimmed_messages = trimmed_messages[first_human_idx:]

    # Dynamic System Prompt injection based on Auth state
    auth_uid = state.get("authenticated_patient_uid")
    if auth_uid:
        auth_ctx = (
            f"\n\n=== ACTIVE SESSION — AUTHENTICATED PATIENT ===\n"
            f"IMPORTANT: This patient IS logged in and verified. Their Patient UID is '{auth_uid}'.\n"
            f"You MUST use tools to answer their questions about their profile, appointments, bills, and medical history.\n"
            f"DO NOT refuse or say they are not authorized. They ARE authorized.\n"
            f"DO NOT tell them to login or register. They are ALREADY logged in.\n"
            f"When calling tools, always pass patient_id='{auth_uid}'.\n"
        )
    else:
        auth_ctx = (
            f"\n\n=== ACTIVE SESSION — ANONYMOUS GUEST ===\n"
            f"This user is NOT logged in. Do NOT access or disclose any personal medical data.\n"
            f"If they ask for personal records, appointments, or bills, instruct them to Login or Register first.\n"
        )

    # ── Intent-aware prompt optimization (Phase 1: Skip-Search) ───────
    classified_intent = state.get("classified_intent", "GENERAL_KNOWLEDGE")

    intent_directive = ""
    if classified_intent == "CHITCHAT":
        intent_directive = (
            "\n\nIMPORTANT: The user is making casual conversation (greeting/thanks/small talk). "
            "Respond warmly and directly. DO NOT call any tools."
        )
    elif classified_intent == "BOOKING_INTENT":
        if auth_uid:
            intent_directive = (
                f"\n\n=== APPOINTMENT BOOKING DIRECTIVE ===\n"
                f"The patient is authenticated with Patient UID '{auth_uid}'.\n"
                f"• When the user specifies or confirms a doctor, date, and time slot, YOU MUST IMMEDIATELY CALL the `book_appointment` tool with patient_id='{auth_uid}'.\n"
                f"• When the user asks for availability for a doctor or department, call `check_doctor_availability`.\n"
                f"• CRITICAL: NEVER output a confirmation message or simulate a booking in text without ACTUALLY calling the `book_appointment` tool!\n"
                f"• Use ONLY appointment tools (book_appointment, check_doctor_availability, cancel_appointment, get_patient_appointments)."
            )
        else:
            intent_directive = (
                "\n\nIMPORTANT: The user wants to book or manage appointments, but they are NOT logged in. "
                "You must ask them to Sign In or Register first using the 'Sign In' or 'Register' button in the top navigation bar. "
                "Explain that they will receive a secure 6-digit authorization token from our official Telegram bot (@MedCare_Verify_Auth_bot) "
                "to verify their identity. DO NOT attempt to book appointments until they are authenticated."
            )
    elif classified_intent == "PROFILE_QUERY":
        if auth_uid:
            intent_directive = (
                "\n\nIMPORTANT: The user is asking about their personal data. Use ONLY profile/billing tools "
                "(get_patient_info, get_patient_bills, get_bill_details, get_medical_history). "
                "DO NOT call search_hospital_knowledge."
            )
        else:
            intent_directive = (
                "\n\nIMPORTANT: The user is asking about their personal records, but they are NOT logged in. "
                "You must explain that they need to Sign In or Register first using the 'Sign In' button in the navigation header "
                "to receive their 6-digit authorization code from @MedCare_Verify_Auth_bot on Telegram."
            )
    elif classified_intent == "DIRECT_ACTION":
        intent_directive = (
            "\n\nIMPORTANT: The user wants to login, register, or verify their account. DO NOT call any tools. "
            "Politely guide them to click the 'Sign In' or 'Register' button in the top header. "
            "Explain that clicking the button will open our official Telegram bot (@MedCare_Verify_Auth_bot) "
            "to generate their secure 6-digit verification token."
        )
    elif classified_intent == "HOSPITAL_INFO":
        intent_directive = (
            "\n\nIMPORTANT: The user is asking about hospital departments, doctors, facilities, or services. "
            "You already have this information in the APPOINTMENT BOOKING GUIDELINES above. "
            "Answer directly and comprehensively from your built-in knowledge. DO NOT call any tools — "
            "especially DO NOT call search_hospital_knowledge. List the departments and their doctors clearly."
        )
    elif classified_intent == "OUT_OF_DOMAIN":
        intent_directive = (
            "\n\nCRITICAL OUT-OF-SCOPE GUARDRAIL DIRECTIVE:\n"
            "The user is asking an off-topic question (such as computer programming, coding, software development, math, or non-healthcare trivia).\n"
            "You MUST NOT answer their off-topic query. You MUST NOT generate any code or technical explanations. You MUST NOT call any tools.\n"
            "Politely decline and redirect them back to MedCare healthcare services:\n"
            "\"I am MedCare Hospital's clinical assistant. I am specifically designed to assist with healthcare inquiries, hospital services, doctor consultations, appointments, and medical department guidance. I cannot assist with programming, software code, or non-healthcare topics.\n\n"
            "How may I assist you with your health or hospital services today?\""
        )

    # ── Proactive embedding pre-warm (reduces RAG latency) ────────────
    if classified_intent == "GENERAL_KNOWLEDGE" and messages:
        import threading
        def _prewarm_embedding():
            try:
                from knowledge.cache import embedding_cache
                from knowledge.ingest import _get_embeddings
                user_msg = messages[-1].content if hasattr(messages[-1], 'content') else str(messages[-1])
                cache_key = f"embed:{user_msg[:200]}"
                if not embedding_cache.get(cache_key):
                    embeddings = _get_embeddings()
                    vec = embeddings.embed_query(user_msg)
                    embedding_cache.set(cache_key, vec)
            except Exception:
                pass  # Non-critical — RAG tool will compute it if pre-warm fails
        threading.Thread(target=_prewarm_embedding, daemon=True).start()

    # ── Live Real-Time Calendar Context Injection ─────────────────────
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
        now_dt = datetime.now(tz)
    except Exception:
        from datetime import datetime
        now_dt = datetime.now()

    live_date_str = now_dt.strftime("%A, %B %d, %Y")
    live_iso_date = now_dt.strftime("%Y-%m-%d")
    live_time_str = now_dt.strftime("%I:%M %p")
    live_day_name = now_dt.strftime("%A")

    calendar_ctx = (
        f"\n\n=== LIVE REAL-TIME CALENDAR CONTEXT ===\n"
        f"• Today's Date: {live_date_str} (ISO: {live_iso_date})\n"
        f"• Current Hospital Time: {live_time_str} (IST, Asia/Kolkata)\n"
        f"• Day of Week: {live_day_name}\n"
        f"• CALENDAR REFERENCE RULES:\n"
        f"  1. ALWAYS calculate relative dates ('today', 'tomorrow', 'this Friday', 'next Monday') strictly starting from today's date ({live_iso_date}).\n"
        f"  2. NEVER book or check availability for dates in the past.\n"
        f"  3. When invoking appointment tools (check_doctor_availability, book_appointment), pass the date in YYYY-MM-DD format (e.g. date='{live_iso_date}')."
    )

    full_system_prompt = SYSTEM_PROMPT + calendar_ctx + auth_ctx + intent_directive

    # Ensure system prompt is always the first message in the payload
    trimmed_messages = [SystemMessage(content=full_system_prompt)] + trimmed_messages

    # ── Multi-Provider Failover Gateway ───────────────────────────────
    providers_to_try = [settings.LLM_PROVIDER, "nvidia", "gemini", "openrouter"]
    
    # Remove duplicates while keeping order
    seen = set()
    providers_to_try = [p for p in providers_to_try if not (p in seen or seen.add(p))]

    # Skip providers that previously failed with auth errors
    providers_to_try = [p for p in providers_to_try if p not in _disabled_providers]

    last_error = None
    for provider in providers_to_try:
        llm = None
        # Try using pre-instantiated primary model if it matches the current provider
        if provider == settings.LLM_PROVIDER:
            llm = primary_llm
            payload_messages = trimmed_messages
        else:
            llm = get_fallback_llm(provider)
            payload_messages = flatten_tool_calls(trimmed_messages)

        if not llm:
            continue

        try:
            llm_with_tools = llm.bind_tools(tools)
            
            # Retry twice with exponential backoff for this provider
            for attempt in range(2):
                try:
                    response = await asyncio.wait_for(
                        llm_with_tools.ainvoke(payload_messages, config=config),
                        timeout=15.0
                    )
                    if response is None:
                        raise RuntimeError(f"Provider '{provider}' returned empty response")
                    return {"messages": [response]}
                except asyncio.TimeoutError:
                    last_error = TimeoutError(f"Provider '{provider}' timed out after 15s")
                    print(f"[FAILOVER] Provider '{provider}' timed out after 15s — immediately moving to next provider")
                    break
                    break  # Don't retry timeouts, move to next provider
                except Exception as inner_e:
                    last_error = inner_e
                    error_str = str(inner_e)
                    print(f"[FAILOVER] Attempt {attempt + 1} failed for provider '{provider}': {inner_e}")
                    # Skip retries for rate limit errors (429) — they won't recover quickly
                    if '429' in error_str or 'rate_limit' in error_str.lower() or 'resource' in error_str.lower() and 'exhausted' in error_str.lower():
                        print(f"[FAILOVER] Rate limit detected for '{provider}' — skipping retries")
                        break
                    # Skip retries for timeout-like errors
                    if any(kw in error_str.lower() for kw in ['timeout', 'timed out', 'deadline exceeded']):
                        print(f"[FAILOVER] Timeout for '{provider}' — moving to next provider")
                        break
                    # Skip retries for authentication errors (invalid/expired API keys)
                    if any(kw in error_str.lower() for kw in [
                        'invalid api key', 'api key not valid', 'api_key_invalid',
                        'unauthorized', '401', '403', 'forbidden',
                        'authentication', 'permission denied', 'credentials'
                    ]):
                        print(f"[FAILOVER] Auth error for '{provider}' — skipping retries")
                        _disabled_providers.add(provider)
                        break
                    # Skip retries for payment/billing errors (no credits)
                    if any(kw in error_str.lower() for kw in [
                        '402', 'insufficient credits', 'payment required',
                        'billing', 'purchase', 'exceeded your current quota'
                    ]):
                        print(f"[FAILOVER] Payment/quota error for '{provider}' — disabling permanently")
                        _disabled_providers.add(provider)
                        break
                    await asyncio.sleep(1.0 * (attempt + 1))
        except Exception as e:
            last_error = e
            print(f"[FAILOVER] Provider '{provider}' initialization or execution failed: {e}")

    # If all fail, raise HTTPException or return fallback message
    print(f"[FAILOVER CRITICAL] All LLM providers exhausted! Last error: {last_error}")
    raise RuntimeError("I'm sorry, all language model services are temporarily unavailable. Please try again in a moment.")


def create_agent_node(primary_llm, tools):
    """Return a closure suitable for StateGraph.add_node."""

    async def _node(state, config):
        return await agent_node(state, config, primary_llm, tools)

    return _node
