"""Agent graph nodes with LLM failover gateway and retries."""

import os
import sys
import time
import asyncio
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
    cross-compatibility when falling over from one LLM provider to another.
    """
    flat = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            flat.append(msg)
        elif isinstance(msg, HumanMessage):
            flat.append(msg)
        elif isinstance(msg, AIMessage):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                flat.append(AIMessage(content="Checking the hospital records database..."))
            else:
                flat.append(msg)
        elif isinstance(msg, ToolMessage):
            flat.append(AIMessage(content=f"[Retrieved Hospital Database Fact]\n{msg.content}"))
    return flat


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.prompts import SYSTEM_PROMPT
from config import settings

# Module-level set to remember providers that failed with auth errors (persists for server lifetime)
_disabled_providers = set()


def get_fallback_llm(provider: str):
    """Dynamically instantiate fallback LLM based on provider string."""
    try:
        if provider == "gemini" and settings.GOOGLE_API_KEY:
            from langchain_google_genai import ChatGoogleGenerativeAI
            model_name = settings.LLM_MODEL.replace("models/", "")
            return ChatGoogleGenerativeAI(
                model=model_name,
                google_api_key=settings.GOOGLE_API_KEY,
                temperature=0,
            )
        elif provider == "ollama":
            from langchain_ollama import ChatOllama
            return ChatOllama(
                model=settings.OLLAMA_MODEL,
                base_url=settings.OLLAMA_BASE_URL,
                temperature=0,
                num_ctx=4096,
            )
        elif provider == "groq" and settings.GROQ_API_KEY:
            from langchain_groq import ChatGroq
            return ChatGroq(
                model=settings.GROQ_LLM_MODEL,
                groq_api_key=settings.GROQ_API_KEY,
                temperature=0,
            )
        elif provider == "openrouter" and settings.OPENROUTER_API_KEY:
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=settings.OPENROUTER_MODEL,
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
                temperature=0,
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
                "\n\nIMPORTANT: The user wants to manage appointments. Follow the APPOINTMENT BOOKING GUIDELINES "
                "in the system prompt — you MUST go through all 7 steps (understand concern, recommend doctor, share "
                "doctor info, ask preferred date, check availability, present slots, confirm & book). "
                "Do NOT skip steps or book directly. First ask about their health concern if they haven't shared it. "
                "Use ONLY appointment-related tools (book_appointment, cancel_appointment, get_patient_appointments, "
                "check_doctor_availability). DO NOT call search_hospital_knowledge."
            )
        else:
            intent_directive = (
                "\n\nIMPORTANT: The user wants to book or manage appointments, but they are NOT logged in. "
                "You must ask them to Login or Register first. If they want to log in, prompt them for Name, "
                "Patient ID, and DOB, then call verify_patient_credentials. If they want to register, call register_patient. "
                "DO NOT call book_appointment until they are logged in."
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
                "You must explain that they need to Login or Register first. If they want to log in, ask for Name, "
                "Patient ID, and DOB, then call verify_patient_credentials."
            )
    elif classified_intent == "DIRECT_ACTION":
        intent_directive = (
            "\n\nIMPORTANT: The user wants to login/register/verify. DO NOT call any tools. "
            "Guide them through the authentication process using the UI."
        )
    elif classified_intent == "HOSPITAL_INFO":
        intent_directive = (
            "\n\nIMPORTANT: The user is asking about hospital departments, doctors, facilities, or services. "
            "You already have this information in the APPOINTMENT BOOKING GUIDELINES above. "
            "Answer directly and comprehensively from your built-in knowledge. DO NOT call any tools — "
            "especially DO NOT call search_hospital_knowledge. List the departments and their doctors clearly."
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

    full_system_prompt = SYSTEM_PROMPT + auth_ctx + intent_directive

    # Ensure system prompt is always the first message in the payload
    trimmed_messages = [SystemMessage(content=full_system_prompt)] + trimmed_messages

    # ── Multi-Provider Failover Gateway ───────────────────────────────
    providers_to_try = [settings.LLM_PROVIDER, "gemini", "openrouter"]
    
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
                    response = None
                    async for chunk in llm_with_tools.astream(payload_messages, config=config):
                        if response is None:
                            response = chunk
                        else:
                            response += chunk
                    if response is None:
                        raise RuntimeError(f"Provider '{provider}' returned empty response")
                    return {"messages": [response]}
                except Exception as inner_e:
                    last_error = inner_e
                    error_str = str(inner_e)
                    print(f"[FAILOVER] Attempt {attempt + 1} failed for provider '{provider}': {inner_e}")
                    # Skip retries for rate limit errors (429) — they won't recover in 1.5s
                    if '429' in error_str or 'rate_limit' in error_str.lower() or 'resource' in error_str.lower() and 'exhausted' in error_str.lower():
                        print(f"[FAILOVER] Rate limit detected for '{provider}' — skipping retries")
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
                    await asyncio.sleep(1.5 * (attempt + 1))
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
