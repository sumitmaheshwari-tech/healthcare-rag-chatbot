    # 🏥 MedCare RAG Chatbot

An **Agentic RAG (Retrieval-Augmented Generation) Chatbot** for patient queries, built with **LangGraph**, **FastAPI**, **ChromaDB**, and a premium web interface.

The agent intelligently decides whether to search the hospital knowledge base (RAG), query live databases (appointments, billing, patient records), or both — and merges everything into a single coherent response.

---

## ✨ Features

- **Agentic RAG** — LangGraph StateGraph routes between 9 tools automatically
- **Hospital Knowledge Base** — departments, doctors, treatments, insurance, FAQs stored in ChromaDB
- **Real-Time Appointments** — check availability, book, cancel with slot-level conflict detection
- **Billing Queries** — view bills, payment history, outstanding balances
- **Medical Records** — past visits, lab results, prescriptions, imaging reports
- **Multi-Turn Conversations** — session memory via LangGraph MemorySaver
- **Patient Profiles** — switch between demo patients to test different scenarios
- **Premium Dark UI** — glassmorphism, animations, responsive design

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| **Agent Orchestration** | LangGraph (StateGraph + ToolNode) |
| **LLM** | OpenAI GPT-4o-mini (configurable) |
| **Vector Store (RAG)** | ChromaDB with OpenAI Embeddings |
| **Backend API** | FastAPI + Uvicorn |
| **Database** | SQLite + SQLAlchemy |
| **Frontend** | Vanilla HTML / CSS / JavaScript |

---

## 📋 Prerequisites

- **Python 3.10+**
- **OpenAI API Key** (get one at https://platform.openai.com/api-keys)

---

## 🚀 Quick Start

### 1. Navigate to the project

```bash
cd healthcare-rag-chatbot
```

### 2. Create your `.env` file

```bash
copy .env.example .env
# Then edit .env and paste your OpenAI API key
```

### 3. Install dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 4. Run the server

```bash
python main.py
```

### 5. Open in browser

```
http://localhost:8000
```

---

## 🏗️ Project Structure

```
healthcare-rag-chatbot/
├── backend/
│   ├── main.py                     # FastAPI entry point
│   ├── config.py                   # Environment configuration
│   ├── agent/
│   │   ├── graph.py                # LangGraph StateGraph
│   │   ├── nodes.py                # Agent node factory
│   │   ├── prompts.py              # System prompt
│   │   └── state.py                # AgentState schema
│   ├── tools/
│   │   ├── rag_tool.py             # ChromaDB knowledge search
│   │   ├── appointment_tool.py     # Book / cancel / check slots
│   │   ├── billing_tool.py         # Bill queries
│   │   └── patient_tool.py         # Patient info & medical history
│   ├── database/
│   │   ├── models.py               # SQLAlchemy models (6 tables)
│   │   ├── connection.py           # Engine & session management
│   │   └── setup.py                # Seed data (doctors, patients, etc.)
│   ├── knowledge/
│   │   ├── ingest.py               # Document → chunk → embed → ChromaDB
│   │   └── documents/              # Markdown knowledge files
│   │       ├── hospital_info.md
│   │       ├── departments.md
│   │       ├── doctors.md
│   │       ├── treatments.md
│   │       ├── insurance.md
│   │       └── faqs.md
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
├── .env.example
└── README.md
```

---

## 🔄 How It Works

```
User Query
    │
    ▼
┌──────────────┐   Has tool calls?
│  Agent Node  │──────────────────────┐
│  (LLM + tools│                      │ YES
│   bound)     │                      ▼
└──────┬───────┘              ┌──────────────┐
       │ NO                   │  Tool Node   │
       ▼                      │  (executes   │
┌──────────┐                  │   the tool)  │
│   END    │                  └──────┬───────┘
│ (respond)│                         │ loops back
└──────────┘                         ▼
                              Agent Node …
```

**Data routing decision by the agent:**

| Query Type | Tool Used | Data Source |
|-----------|-----------|-------------|
| "What departments do you have?" | `search_hospital_knowledge` | ChromaDB (RAG) |
| "Is Dr. Reddy available Monday?" | `check_doctor_availability` | SQLite DB |
| "Book me at 10:00 AM" | `book_appointment` | SQLite DB (write) |
| "Show my bills" | `get_patient_bills` | SQLite DB |
| "What's my blood group?" | `get_patient_info` | SQLite DB |
| "What does Cardiology treat?" | `search_hospital_knowledge` | ChromaDB (RAG) |

---

## 💬 Sample Queries to Try

1. **"What departments does the hospital have?"** — RAG retrieval
2. **"Tell me about Dr. Ananya Reddy"** — RAG retrieval
3. **"Check Dr. Rajesh Mehta's availability for next Monday"** — DB query
4. **"Book an appointment with Dr. Kavitha for dental cleaning"** — Multi-turn booking
5. **"Show me my upcoming appointments"** — DB query
6. **"What are my pending bills?"** — DB query
7. **"Show my medical history"** — DB query
8. **"How much does an MRI cost?"** — RAG retrieval
9. **"What insurance plans do you accept?"** — RAG retrieval
10. **"Cancel appointment #3"** — DB write

---

## 📄 License

MIT License — feel free to use and modify for your projects.
cd /d c:\Users\HP\Desktop\PROJECTS\healthcare-rag-chatbot\backend
python main.py