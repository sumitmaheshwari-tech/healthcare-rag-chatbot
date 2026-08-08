"""Agent state schema for LangGraph."""

from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """State carried through every node of the agent graph.

    ``messages`` uses the built-in ``add_messages`` reducer so that each
    node's return value is *appended* rather than overwriting previous
    messages.
    """

    messages: Annotated[list, add_messages]
    authenticated_patient_uid: Optional[str]
    classified_intent: str
