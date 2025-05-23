#!/usr/bin/env python3
"""evaluate_rag.py

Automated evaluation script for RAG pipelines using RAGAS, Constitutional-Judge, and LangGraph.
"""
import json
import os
import sys
from typing import List, Dict

import click
from ingest_rag import get_openai_client
from qdrant_client import QdrantClient

def retrieve_and_generate(
    query: str,
    qdrant_client: QdrantClient,
    openai_client,
    collection: str,
    vector_model: str,
    llm_model: str,
    k: int,
    hybrid: bool,
    alpha: float,
) -> Dict:
    """Run retrieval (hybrid BM25+vector) and optional LLM generation."""
    # Embed query
    if hasattr(openai_client, "embeddings"):
        resp = openai_client.embeddings.create(model=vector_model, input=[query])
        vector = resp.data[0].embedding
    else:
        resp = openai_client.Embedding.create(model=vector_model, input=[query])
        vector = resp["data"][0]["embedding"]  # type: ignore

    # Retrieve from Qdrant
    if hybrid:
        try:
            hits = qdrant_client.search(
                collection_name=collection,
                query_vector=vector,
                limit=k,
                with_payload=True,
                search_type="hybrid",
                params={"hybrid": {"alpha": alpha}},
            )
        except TypeError:
            # fallback if search_type not supported
            hits = qdrant_client.search(
                collection_name=collection,
                query_vector=vector,
                limit=k,
                with_payload=True,
            )
    else:
        hits = qdrant_client.search(
            collection_name=collection,
            query_vector=vector,
            limit=k,
            with_payload=True,
        )

    # Extract context snippets
    contexts: List[str] = []
    for hit in hits:
        payload = getattr(hit, "payload", {}) or {}
        snippet = payload.get("chunk_text") or payload.get("text") or ""
        contexts.append(snippet)

    # Optional answer generation
    answer = None
    if llm_model:
        context = "\n\n---\n\n".join(contexts[:k])
        system_msg = {"role": "system", "content": "You are a helpful assistant."}
        user_msg = {
            "role": "user",
            "content": f"Use the following context to answer the question.\n\nContext:\n{context}\n\nQuestion: {query}",
        }
        if hasattr(openai_client, "chat"):
            chat = openai_client.chat.completions.create(
                model=llm_model, messages=[system_msg, user_msg]
            )
            answer = chat.choices[0].message.content
        else:
            chat = openai_client.ChatCompletion.create(
                model=llm_model, messages=[system_msg, user_msg]
            )
            answer = chat.choices[0].message.content  # type: ignore

    return {"query": query, "contexts": contexts, "answer": answer, "hits": hits}


@click.command()
@click.option("--test-file", required=True, type=click.Path(exists=True), help="JSONL file with test cases. Each line: {\"query\":..., \"ground_truth\":..., \"relevant_doc_ids\": [...] }.")
@click.option("--collection", required=True, help="Qdrant collection name.")
@click.option("--qdrant-url", default=lambda: os.environ.get("QDRANT_URL", "http://localhost:6333"), help="Qdrant HTTP URL.")
@click.option("--qdrant-api-key", default=lambda: os.environ.get("QDRANT_API_KEY"), help="Qdrant API key.")
@click.option("--openai-api-key", default=lambda: os.environ.get("OPENAI_API_KEY"), help="OpenAI API key.")
@click.option("--vector-model", default="text-embedding-3-large", help="OpenAI embedding model.")
@click.option("--llm-model", default="gpt-4.1-mini", help="LLM model for answer generation (empty to skip).")
@click.option("--k", default=100, show_default=True, help="Number of contexts to retrieve.")
@click.option("--hybrid/--no-hybrid", default=True, show_default=True, help="Enable BM25+vector retrieval.")
@click.option("--alpha", default=0.35, show_default=True, help="Weight for vector score in hybrid retrieval.")
@click.option("--framework", "-f", "frameworks", multiple=True, type=click.Choice(["ragas", "cj", "langgraph"]), default=["ragas", "cj", "langgraph"], help="Evaluation frameworks to run.")
def main(test_file, collection, qdrant_url, qdrant_api_key, openai_api_key, vector_model, llm_model, k, hybrid, alpha, frameworks):
    """Run automated RAG evaluation using multiple frameworks."""
    # Initialize clients
    openai_client = get_openai_client(openai_api_key)
    qdrant_client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)

    # Load test cases
    tests: List[Dict] = []
    with open(test_file, "r") as fh:
        for line in fh:
            if line.strip():
                tests.append(json.loads(line))

    # Store per-framework results
    results: Dict[str, List[float]] = {fw: [] for fw in frameworks}

    # Iterate through test cases
    for case in tests:
        query = case.get("query", "")
        gt_answer = case.get("ground_truth")
        relevant_ids = set(case.get("relevant_doc_ids", []))

        out = retrieve_and_generate(query, qdrant_client, openai_client, collection, vector_model, llm_model, k, hybrid, alpha)

        # Evaluate with each framework
        for fw in frameworks:
            try:
                if fw == "ragas":
                    import ragas
                    evaluator = ragas.RAGASEvaluator(api_key=openai_api_key)  # placeholder API
                    score = evaluator.evaluate(query, out["answer"], gt_answer)
                elif fw == "cj":
                    import constitutional_judge as cj
                    evaluator = cj.Judge()  # placeholder API
                    score = evaluator.evaluate(out["answer"], gt_answer)
                elif fw == "langgraph":
                    import langgraph as lg
                    evaluator = lg.Evaluator()  # placeholder API
                    score = evaluator.evaluate(query, out["contexts"], out["answer"], gt_answer)
                else:
                    score = 0.0
                results[fw].append(score)
            except ImportError:
                click.echo(f"[warning] {fw} not installed; skipping {fw} evaluation.", err=True)

    # Print summary
    for fw in frameworks:
        scores = results.get(fw, [])
        if scores:
            avg = sum(scores) / len(scores)
            click.echo(f"[{fw}] Average score over {len(scores)} cases: {avg:.4f}")
        else:
            click.echo(f"[{fw}] No scores computed (check installation).")

if __name__ == "__main__":
    main()