# 🏥 MedCare Assistant — Technical Architecture & Technology Justification

This document details the software engineering choices, architecture, and system design justifications for the **MedCare Assistant Healthcare Agentic RAG Chatbot**.

---

## 🏗️ Architectural Overview

The application is structured as a **Retrieval-Augmented Generation (RAG)** system powered by an agentic orchestration graph. It blends unstructured static data search (RAG) with live structured transactions (SQL queries) into a unified conversational interface.

```
                  ┌───────────────────────┐
                  │    User Interface     │
                  │   (HTML/CSS/JS Web)   │
                  └───────────┬───────────┘
                              │ HTTP Requests
                              ▼
                  ┌───────────────────────┐
                  │    FastAPI Server     │
                  │       (main.py)       │
                  └───────────┬───────────┘
                              │ State Session Invoke
                              ▼
  ┌────────────────────────────────────────────────────────┐
  │                 LangGraph Agentic Flow                 │
  │                                                        │
  │                    ┌───────────────┐                   │
  │                    │  Agent Node   │◄─────────┐        │
  │                    │   (LLM Core)  │          │        │
  │                    └───────┬───────┘          │        │
  │                            │                  │        │
  │                            ▼ Route Decision   │ Loop   │
  │                  [tools_condition edge]       │ Back   │
  │                     /             \           │        │
  │             No tools/               Tool Calls│        │
  │             Response                 \        │        │
  │                   ▼                   ▼       │        │
  │                [ END ]           ┌──────────┐ │        │
  │                                  │Tool Node │─┘        │
  │                                  └────┬─────┘          │
  └───────────────────────────────────────┼────────────────┘
                                          │ Executes
                                          ▼
                ┌───────────────────────────────────┐
                │          Available Tools          │
                ├─────────────────┬─────────────────┤
                │     RAG tool    │  Database tools │
                │   (ChromaDB)    │    (SQLite)     │
                └─────────────────┴─────────────────┘
```

---

## 🛠️ Technology Stack & Justification

### 1. Agent Orchestration: **LangGraph (0.2+)**
* **What it does:** Manages the conversational StateGraph, routes execution between the LLM and DB tools, and preserves session memory using `MemorySaver`.
* **Why it was chosen:**
  * **Cycles and Loops:** Standard LangChain chains are linear. Healthcare queries require loops (e.g., checking doctor availability, finding out the time is taken, returning to ask the user for another time, and then booking). LangGraph naturally supports cyclic state graphs.
  * **Thread Memory:** LangGraph's `MemorySaver` partitions history dynamically by `thread_id` (session key), allowing multiple users to chat simultaneously with isolated states.

### 2. Large Language Model (LLM): **Groq (ChatGroq) & Ollama (ChatOllama)**
* **What it does:** Processes user messages, determines which tool to execute, and writes conversational responses.
* **Why they were chosen:**
  * **Dual Setup (Cloud + Local):** Prototyping is done via Groq for high-speed cloud responses (`llama-3.3-70b-versatile`).
  * **Ollama for Unlimited Local Deployment:** By downloading Ollama locally, you run the `llama3.1` model directly on your CPU/GPU. This eliminates rate limits, maintains 100% data privacy inside the local computer, and works offline without internet costs.

### 3. Embeddings Model: **Google Gemini (`gemini-embedding-001`)**
* **What it does:** Translates text chunks from the hospital guides into a high-dimensional vector space.
* **Why it was chosen:**
  * **High Dimension Accuracy:** It captures semantic similarity far better than smaller CPU-based embedding models.
  * **Cost-Efficient:** Google's AI Studio provides a free tier for embeddings, making it cost-free to process indexing.

### 4. Vector Database: **ChromaDB**
* **What it does:** Stores the vectorized chunks of the hospital manuals and performs semantic searches.
* **Why it was chosen:**
  * **Zero Setup Overheads:** ChromaDB is a lightweight serverless database. It runs inside the Python process and writes simple files to the `chroma_db/` directory, avoiding the need to host external cloud vector databases (like Pinecone or Milvus).

### 5. Relational Database: **SQLite & SQLAlchemy ORM**
* **What it does:** Manages Patient profiles, Doctor schedules, active Appointments, Medical history records, and Billing logs.
* **Why it was chosen:**
  * **SQLite File-on-disk:** SQLite stores all data locally in the **[hospital.db](file:///c:/Users/HP/Desktop/PROJECTS/healthcare-rag-chatbot/backend/hospital.db)** file. There are no port configs, servers, or external credentials needed.
  * **SQLAlchemy ORM:** Provides an Object-Relational Mapper that protects database queries from SQL Injection attacks and provides programmatic transactional control.
  * **ACID Transactions:** Ensures bookings are safe from race conditions, preventing double-bookings.

### 6. Backend Framework: **FastAPI & Uvicorn**
* **What it does:** Exposes conversational REST APIs (`/api/chat`), feeds dynamic patient records to the UI (`/api/patients`), and hosts static frontend files.
* **Why it was chosen:**
  * **Speed:** Asynchronous request loops make FastAPI one of the fastest Python frameworks.
  * **Static File Mounting:** It mounts the `frontend/` folder directly to `/static/` so the entire app (backend and frontend) runs from a single unified server port (`8000`).

### 7. UI System: **Vanilla HTML5, CSS3 & JavaScript**
* **What it does:** The browser interface used by patients.
* **Why it was chosen:**
  * **Zero Compilation overhead:** Avoids heavy JavaScript runtimes (like Node.js, Webpack, or Next.js builds). It can be run on any machine immediately with just Python installed.
  * **Reassuring Psychology Theme:** Redesigned into a calm light-themed wellness interface (soft sage green and light peach sunrise gradient) specifically configured to lower patient anxiety.

---

## 🌐 API Integrations Reference

The system uses a mix of internal backend endpoints (called by the frontend) and external service APIs (called by the backend).

### 1. Internal API Endpoints (FastAPI)
The frontend client communicates with the python backend via the following routes:

| Method | Endpoint | Request Body | Response JSON | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **POST** | `/api/chat` | `{ "message": str, "session_id": str, "patient_id": int }` | `{ "response": str, "session_id": str }` | Routes the user query to the LangGraph agent, carrying user and patient ID contexts. |
| **GET** | `/api/patients` | *None* | `[ { "id": int, "name": str, "age": int, "gender": str } ]` | Fetches patient profiles from SQLite to populate the dropdown. |
| **GET** | `/api/health` | *None* | `{ "status": "healthy", "agent_ready": true }` | Verifies the server is live and the agent is compiled. |
| **GET** | `/` | *None* | *HTML File* | Serves the main UI dashboard. |
| **GET** | `/static/*` | *None* | *Asset Files (CSS/JS)* | Mounts static files folder. |

---

### 2. External API Connections (Backend Integrations)

* **Groq Cloud API** (`https://api.groq.com/openai/v1`)
  * **Model:** `llama-3.1-70b-versatile` or `llama-3.3-70b-versatile`
  * **Purpose:** Remote LLM inference and reasoning, parsing instructions, and calling system tools.

* **Ollama Local API** (`http://localhost:11434`)
  * **Model:** `llama3.1` (or local preference)
  * **Purpose:** Locally-hosted alternative LLM engine for unlimited offline processing without cost or rate limits.

* **Google Gemini API** (`https://generativelanguage.googleapis.com`)
  * **Model:** `models/gemini-embedding-001`
  * **Purpose:** Generates semantic text embeddings used to create indices and search the ChromaDB vector database.

