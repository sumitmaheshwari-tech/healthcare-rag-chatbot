"""RAG retrieval tool — searches the hospital knowledge base using hybrid search (Vector + BM25) and caching."""

import os
import sys
import time
import concurrent.futures
from typing import Annotated
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from langchain_core.documents import Document

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowledge.cache import embedding_cache, retrieval_cache

# Module-level executor reused across all calls (avoids 10-40ms per-creation overhead on Windows)
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def reciprocal_rank_fusion(vector_docs, bm25_docs, k_rrf=60, top_n=5):
    """
    Combine vector semantic search results and BM25 lexical search results
    using Reciprocal Rank Fusion (RRF).
    """
    scores = {}
    doc_map = {}

    # Rank and score from semantic vector search
    for rank, doc in enumerate(vector_docs):
        content = doc.page_content
        doc_map[content] = doc
        scores[content] = scores.get(content, 0.0) + 1.0 / (k_rrf + rank + 1)

    # Rank and score from lexical BM25 search
    for rank, doc_dict in enumerate(bm25_docs):
        content = doc_dict["text"]
        if content not in doc_map:
            doc_map[content] = Document(page_content=content, metadata=doc_dict["metadata"])
        scores[content] = scores.get(content, 0.0) + 1.0 / (k_rrf + rank + 1)

    # Sort documents by RRF score descending
    sorted_contents = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [doc_map[content] for content in sorted_contents[:top_n]]


@tool
def search_hospital_knowledge(
    query: str,
    state: Annotated[dict, InjectedState] = None
) -> str:
    """Search the hospital's knowledge base for information about departments,
    doctors, treatments, insurance policies, hospital facilities, FAQs, and
    general hospital information.

    [CRITICAL LATENCY DIRECTIVE: DO NOT use this tool if the user is simply stating
    they want to book an appointment, login, register, check appointments, or make
    greetings. This tool makes slow external web requests. Only use it when the
    user has a specific factual query about policies, facilities, or treatments.]

    Use this tool when the user asks about:
    - Hospital services, facilities, visiting hours, parking, cafeteria
    - Department details, conditions treated, procedures offered
    - Doctor profiles, qualifications, specializations, consultation fees
    - Treatment procedures, preparation instructions, costs
    - Insurance coverage, accepted providers, claim process
    - General frequently asked questions
    """
    from knowledge.ingest import get_vectorstore, get_bm25_retriever, _get_embeddings

    # 1. Namespacing the cache key by patient_uid to prevent cross-tenant leaks
    patient_uid = "guest"
    if state and state.get("authenticated_patient_uid"):
        patient_uid = state.get("authenticated_patient_uid")

    cache_key = f"{patient_uid}:{query.strip()}"

    # 2. Check Retrieval Cache
    cached_result = retrieval_cache.get(cache_key)
    if cached_result is not None:
        print(f"[CACHE HIT] Retrieval results found for cache key: '{cache_key[:30]}...'")
        return cached_result

    print(f"[CACHE MISS] Executing hybrid RAG search for key: '{cache_key[:30]}...'")

    vectorstore = get_vectorstore()
    bm25 = get_bm25_retriever()

    if vectorstore is None and bm25 is None:
        return "Knowledge base is not available. Please try again later."

    # If vectorstore is unavailable (e.g., serverless cold start or SQLite constraint), use BM25
    if vectorstore is None:
        print(f"[RAG] Vectorstore unavailable, falling back to BM25 lexical retrieval.")
        bm25_docs = bm25.score(query, top_n=3)
        if not bm25_docs:
            return "No relevant information found in the hospital knowledge base."
        results = [f"[Source: {d.get('metadata', {}).get('category', 'unknown')}] {d.get('text', '')}" for d in bm25_docs]
        final_response = "\n\n".join(results)
        retrieval_cache.set(cache_key, final_response)
        return final_response

    # 3. Check / Populate Embedding Cache to skip expensive embedding computation
    query_vector = embedding_cache.get(cache_key)
    if query_vector is None:
        try:
            embeddings = _get_embeddings()
            t0 = time.perf_counter()
            future = _executor.submit(embeddings.embed_query, query)
            query_vector = future.result()
            t1 = time.perf_counter()
            print(f"[PERF] Embedding: {(t1-t0)*1000:.1f}ms")
            embedding_cache.set(cache_key, query_vector)
        except Exception as e:
            print(f"[RAG ERROR] Failed to generate query embedding: {e}. Falling back to BM25.")
            if bm25 is not None:
                bm25_docs = bm25.score(query, top_n=3)
                if bm25_docs:
                    results = [f"[Source: {d.get('metadata', {}).get('category', 'unknown')}] {d.get('text', '')}" for d in bm25_docs]
                    final_response = "\n\n".join(results)
                    retrieval_cache.set(cache_key, final_response)
                    return final_response
            return "Failed to query the knowledge base due to embedding generation error."

    # 4. Execute Semantic and Lexical Search concurrently (thread-safe)
    try:
        t1 = time.perf_counter()
        vector_future = _executor.submit(vectorstore.similarity_search_by_vector, query_vector, k=3)
        bm25_future = _executor.submit(bm25.score, query, top_n=3)
        concurrent.futures.wait([vector_future, bm25_future])
        vector_docs = vector_future.result()
        bm25_docs = bm25_future.result()
        t2 = time.perf_counter()
        print(f"[PERF] Hybrid search: {(t2-t1)*1000:.1f}ms")
        
        # Merge results using Reciprocal Rank Fusion
        merged_docs = reciprocal_rank_fusion(vector_docs, bm25_docs, top_n=3)
        t3 = time.perf_counter()
        print(f"[PERF] RRF fusion: {(t3-t2)*1000:.1f}ms")
        
        if not merged_docs:
            return "No relevant information found in the hospital knowledge base."

        results = []
        for doc in merged_docs:
            source = doc.metadata.get("category", "unknown")
            results.append(f"[Source: {source}] {doc.page_content}")

        final_response = "\n\n".join(results)
        
        # 5. Populate Retrieval Cache
        retrieval_cache.set(cache_key, final_response)
        
        return final_response

    except Exception as e:
        print(f"[RAG ERROR] Hybrid search failed: {e}")
        return f"Error searching the knowledge base: {e}"
