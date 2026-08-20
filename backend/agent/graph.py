"""Build and compile the LangGraph agent."""

import os
import sys

try:
    import pip_system_certs.wrapt_requests
except Exception:
    pass

from langgraph.graph import StateGraph, START
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.messages import HumanMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.state import AgentState
from agent.nodes import create_agent_node
from agent.intent_classifier import classify_intent
from tools.rag_tool import search_hospital_knowledge
from tools.appointment_tool import (
    check_doctor_availability,
    book_appointment,
    cancel_appointment,
    get_patient_appointments,
)
from tools.billing_tool import get_patient_bills, get_bill_details
from tools.patient_tool import get_patient_info, get_medical_history, register_patient, verify_patient_credentials
from config import settings


# ── Persistent Checkpointer (survives restarts) ─────────────────────
_CHECKPOINT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "checkpoints.db"
)


async def _get_async_checkpointer():
    """Create an AsyncSqliteSaver for async-compatible persistent checkpointing."""
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        import aiosqlite
        # Manually create connection (from_conn_string returns a context manager)
        conn = await aiosqlite.connect(_CHECKPOINT_DB_PATH)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        print(f"[AGENT] Checkpointer: AsyncSqliteSaver (persistent) at {_CHECKPOINT_DB_PATH}")
        return saver
    except Exception as e:
        print(f"[AGENT] AsyncSqliteSaver failed ({e}), falling back to MemorySaver (non-persistent)")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


async def build_graph():
    """Construct, compile, and return the LangGraph agent with checkpointer."""

    # Collect every tool the agent may use
    tools = [
        search_hospital_knowledge,
        check_doctor_availability,
        book_appointment,
        cancel_appointment,
        get_patient_appointments,
        get_patient_bills,
        get_bill_details,
        get_patient_info,
        get_medical_history,
        register_patient,
        verify_patient_credentials,
    ]

    # Initialize the LLM dynamically depending on the selected provider
    if settings.LLM_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        print(f"[AGENT] Initializing Gemini model on Google Cloud TPUs: {settings.LLM_MODEL} …")
        model_name = settings.LLM_MODEL.replace("models/", "")
        llm = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=settings.GOOGLE_API_KEY,
            temperature=0,
            timeout=60,
        )
    elif settings.LLM_PROVIDER == "openrouter":
        from langchain_openai import ChatOpenAI
        print(f"[AGENT] Initializing OpenRouter model on Cloud GPUs: {settings.OPENROUTER_MODEL} …")
        llm = ChatOpenAI(
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
    else:
        from langchain_openai import ChatOpenAI
        print(f"[AGENT] Initializing OpenRouter model: {settings.OPENROUTER_MODEL} …")
        llm = ChatOpenAI(
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


    # ── Intent classifier node ─────────────────────────────────────────
    def intent_classifier_node(state: AgentState) -> dict:
        """Classify the latest user message intent before the agent runs."""
        user_message_text = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                user_message_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break
        result = classify_intent(user_message_text)
        return {"classified_intent": result["intent"]}

    # ── Graph construction ────────────────────────────────────────────
    builder = StateGraph(AgentState)

    builder.add_node("intent_classifier", intent_classifier_node)
    builder.add_node("agent", create_agent_node(llm, tools))
    builder.add_node("tools", ToolNode(tools))

    builder.add_edge(START, "intent_classifier")
    builder.add_edge("intent_classifier", "agent")
    builder.add_conditional_edges("agent", tools_condition)   # → "tools" or END
    builder.add_edge("tools", "agent")                        # loop back

    # Persistent async SQLite checkpointer (survives restarts)
    memory = await _get_async_checkpointer()
    graph = builder.compile(checkpointer=memory)

    print(f"[AGENT] LangGraph agent compiled — provider: {settings.LLM_PROVIDER.upper()}.")
    print(f"[AGENT] Tools: {[t.name for t in tools]}")

    return graph
