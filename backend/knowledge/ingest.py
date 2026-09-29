"""Document ingestion pipeline — loads, chunks, embeds, and stores hospital knowledge in ChromaDB."""

import os
import sys
import math
from pathlib import Path
from collections import Counter

# Ensure SSL certificate verification works reliably on Windows Python
try:
    import pip_system_certs.wrapt_requests
except Exception:
    pass
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings

try:
    from langchain_chroma import Chroma
except Exception as e:
    Chroma = None
    print(f"[RAG] ChromaDB not available in this environment: {e}")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

# Module-level singletons
vectorstore = None
retriever = None
bm25_retriever = None


class SimpleBM25:
    """A lightweight, zero-dependency implementation of BM25 lexical search."""
    def __init__(self, corpus: list[dict], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.corpus_size = len(corpus)
        self.doc_lens = [len(self._tokenize(d["text"])) for d in corpus]
        self.avg_doc_len = sum(self.doc_lens) / self.corpus_size if self.corpus_size > 0 else 0
        
        self.doc_freqs = {}
        self.idf = {}
        self._calculate_idf()

    def _tokenize(self, text: str) -> list[str]:
        return [w.lower() for w in text.split() if w.isalnum()]

    def _calculate_idf(self):
        for doc in self.corpus:
            doc["tokens"] = self._tokenize(doc["text"])
            unique_tokens = set(doc["tokens"])
            for t in unique_tokens:
                self.doc_freqs[t] = self.doc_freqs.get(t, 0) + 1
        
        for token, freq in self.doc_freqs.items():
            # BM25 IDF formulation
            self.idf[token] = math.log((self.corpus_size - freq + 0.5) / (freq + 0.5) + 1.0)

    def score(self, query: str, top_n: int = 5) -> list[dict]:
        query_tokens = self._tokenize(query)
        scored_docs = []
        
        for idx, doc in enumerate(self.corpus):
            doc_len = self.doc_lens[idx]
            score = 0.0
            doc_token_counts = Counter(doc["tokens"])
            
            for q in query_tokens:
                if q in doc_token_counts:
                    tf = doc_token_counts[q]
                    idf = self.idf.get(q, 0.0)
                    numerator = tf * (self.k1 + 1)
                    denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avg_doc_len)
                    score += idf * numerator / denominator
            
            scored_docs.append((doc, score))
            
        scored_docs.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in scored_docs[:top_n]]


_cached_embeddings = None

def _get_embeddings():
    """Return local or cloud embeddings based on model name (cached singleton)."""
    global _cached_embeddings
    if _cached_embeddings is not None:
        return _cached_embeddings

    # 1. NVIDIA Build API (Direct — highest quality, 2048 dims)
    if settings.NVIDIA_API_KEY and ("nemotron" in settings.EMBEDDING_MODEL.lower() or "nvidia" in settings.EMBEDDING_MODEL.lower()):
        try:
            from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
            _cached_embeddings = NVIDIAEmbeddings(
                model=settings.NVIDIA_EMBED_MODEL,
                api_key=settings.NVIDIA_API_KEY,
            )
            print(f"[RAG] Using NVIDIA Build embeddings: {settings.NVIDIA_EMBED_MODEL}")
            return _cached_embeddings
        except ImportError:
            try:
                from langchain_core.embeddings import Embeddings
                import requests as _requests

                class NvidiaDirectEmbeddings(Embeddings):
                    """Direct HTTPS client for NVIDIA asymmetric embeddings with mandatory input_type."""
                    def __init__(self, model: str, api_key: str):
                        self.model = model
                        self.api_key = api_key
                        self.url = "https://integrate.api.nvidia.com/v1/embeddings"
                        self.headers = {
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        }

                    def embed_documents(self, texts: list[str]) -> list[list[float]]:
                        all_embeddings = []
                        for i in range(0, len(texts), 20):
                            batch = texts[i:i+20]
                            payload = {
                                "input": batch,
                                "model": self.model,
                                "input_type": "passage",
                            }
                            resp = _requests.post(self.url, headers=self.headers, json=payload, timeout=30)
                            if resp.status_code != 200:
                                raise RuntimeError(f"NVIDIA embeddings error {resp.status_code}: {resp.text}")
                            data = resp.json().get("data", [])
                            all_embeddings.extend([d["embedding"] for d in sorted(data, key=lambda x: x["index"])])
                        return all_embeddings

                    def embed_query(self, text: str) -> list[float]:
                        payload = {
                            "input": [text],
                            "model": self.model,
                            "input_type": "query",
                        }
                        resp = _requests.post(self.url, headers=self.headers, json=payload, timeout=30)
                        if resp.status_code != 200:
                            raise RuntimeError(f"NVIDIA embeddings error {resp.status_code}: {resp.text}")
                        data = resp.json().get("data", [])
                        return data[0]["embedding"]

                _cached_embeddings = NvidiaDirectEmbeddings(
                    model=settings.NVIDIA_EMBED_MODEL,
                    api_key=settings.NVIDIA_API_KEY,
                )
                print(f"[RAG] Using NVIDIA Build direct embeddings: {settings.NVIDIA_EMBED_MODEL}")
                return _cached_embeddings
            except Exception as e:
                print(f"[RAG WARNING] NVIDIA Build direct embeddings failed ({e}), trying OpenRouter fallback...")
        except Exception as e:
            print(f"[RAG WARNING] NVIDIA Build embeddings failed ({e}), trying OpenRouter fallback...")

    # 2. OpenRouter NVIDIA Nemotron (Fallback — 1024 dims)
    if settings.OPENROUTER_API_KEY and ("nemotron" in settings.EMBEDDING_MODEL.lower() or "openrouter" in settings.EMBEDDING_MODEL.lower() or "nvidia" in settings.EMBEDDING_MODEL.lower()):
        try:
            from langchain_openai import OpenAIEmbeddings
            _cached_embeddings = OpenAIEmbeddings(
                model="nvidia/nemotron-3-embed-1b:free",
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
                check_embedding_ctx_length=False,
                tiktoken_enabled=False,
                encoding_format="float",
            )
            print(f"[RAG] Using OpenRouter embeddings: nvidia/nemotron-3-embed-1b:free")
            return _cached_embeddings
        except Exception as e:
            print(f"[RAG WARNING] OpenRouter embeddings failed ({e}), trying next...")

    # 3. Google Gemini Embeddings
    if "gemini" in settings.EMBEDDING_MODEL.lower() or settings.EMBEDDING_MODEL.startswith("models/"):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        model_name = settings.EMBEDDING_MODEL
        if model_name.startswith("models/"):
            model_name = model_name.replace("models/", "")
        _cached_embeddings = GoogleGenerativeAIEmbeddings(
            model=model_name,
            google_api_key=settings.GOOGLE_API_KEY,
        )
        print(f"[RAG] Using Google Gemini embeddings: {model_name}")
    else:
        # 4. Local HuggingFace (Offline fallback)
        from langchain_community.embeddings import HuggingFaceEmbeddings
        _cached_embeddings = HuggingFaceEmbeddings(
            model_name=settings.EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
        )
        print(f"[RAG] Using local HuggingFace embeddings: {settings.EMBEDDING_MODEL}")
    return _cached_embeddings


def ingest_documents() -> Chroma | None:
    """Load, chunk, embed, and store hospital knowledge documents in ChromaDB & BM25."""
    global vectorstore, retriever, bm25_retriever

    docs_dir = settings.KNOWLEDGE_DIR
    embeddings = _get_embeddings()

    # Extract actual dimension dynamically to set suffix
    try:
        test_vec = embeddings.embed_query("dimension_check")
        dim_suffix = str(len(test_vec))
    except Exception as e:
        print(f"[RAG WARNING] Failed to dynamically check embedding dimension: {e}")
        dim_suffix = "3072" if "gemini" in settings.EMBEDDING_MODEL.lower() else "384"

    persist_dir = Path(settings.CHROMA_PERSIST_DIR + "_" + dim_suffix)

    # ── Re-use existing store if present ──────────────────────────────
    if Chroma is not None and os.path.exists(persist_dir) and os.listdir(persist_dir):
        print("[RAG] Loading existing ChromaDB vector store …")
        try:
            vectorstore = Chroma(
                persist_directory=str(persist_dir),
                embedding_function=embeddings,
                collection_name=settings.CHROMA_COLLECTION,
            )
            # Force verification of embedding model dimension compatibility during startup
            test_vec = embeddings.embed_query("verification")
            vectorstore.similarity_search_by_vector(test_vec, k=1)
            
            retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
            
            # Load corpus from Chroma DB to feed the BM25 index
            db_data = vectorstore.get()
            corpus = []
            if db_data and "documents" in db_data and db_data["documents"]:
                for text, meta in zip(db_data["documents"], db_data["metadatas"]):
                    corpus.append({"text": text, "metadata": meta})
            
            if not corpus:
                raise ValueError("Loaded vector store contains 0 documents")

            bm25_retriever = SimpleBM25(corpus)
            print(f"[RAG] Loaded existing vector store & BM25 index with {len(corpus)} documents.")
            return vectorstore
        except Exception as e:
            print(f"[RAG WARNING] Mismatch or error with existing database: {e}. Re-indexing knowledge base...")
            import shutil
            shutil.rmtree(persist_dir, ignore_errors=True)

    # ── Fresh ingestion ───────────────────────────────────────────────
    loader = DirectoryLoader(
        str(docs_dir),
        glob="**/*.md",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"},
    )
    documents = loader.load()
    print(f"[RAG] Loaded {len(documents)} documents.")

    if not documents:
        print("[RAG] WARNING: No documents found!")
        return None

    # Tag each doc with its source filename
    for doc in documents:
        filename = Path(doc.metadata.get("source", "")).stem
        doc.metadata["category"] = filename

    # Chunk
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
        separators=["\\n## ", "\\n### ", "\\n\\n", "\\n", ". ", " ", ""],
    )
    chunks = text_splitter.split_documents(documents)
    print(f"[RAG] Split into {len(chunks)} chunks.")

    # Initialize BM25 corpus from chunks (pure in-memory, infallible fallback)
    corpus = [{"text": chunk.page_content, "metadata": chunk.metadata} for chunk in chunks]
    bm25_retriever = SimpleBM25(corpus)
    print(f"[RAG] In-memory BM25 index created successfully with {len(chunks)} chunks.")

    # Embed & store using embeddings (ChromaDB)
    if Chroma is not None:
        try:
            vectorstore = Chroma.from_documents(
                documents=chunks,
                embedding=embeddings,
                persist_directory=str(persist_dir),
                collection_name=settings.CHROMA_COLLECTION,
            )
            retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
            print(f"[RAG] ChromaDB vector store created successfully with {len(chunks)} chunks.")
        except Exception as e:
            print(f"[RAG WARNING] ChromaDB vectorstore creation failed: {e}. Falling back to BM25-only lexical retriever.")
            vectorstore = None
            retriever = None
    else:
        print("[RAG] ChromaDB is unavailable. Operating in BM25-only lexical mode.")
        vectorstore = None
        retriever = None

    return vectorstore


def get_retriever():
    """Return the document retriever, initialising if necessary."""
    global retriever
    if retriever is None:
        ingest_documents()
    return retriever


def get_bm25_retriever():
    """Return the BM25 retriever, initialising if necessary."""
    global bm25_retriever
    if bm25_retriever is None:
        ingest_documents()
    return bm25_retriever


def get_vectorstore():
    """Return the vector store instance."""
    global vectorstore
    if vectorstore is None:
        ingest_documents()
    return vectorstore
