"""
Azure AI Search client for Semantic Context Retrieval (RAG).

Indexes reference data from Brokerware/TMS (carriers, locations, products,
routing rules) into a dedicated ``shipment-context`` search index, then
retrieves the most relevant snippets before each OpenAI extraction call.
The snippets are injected into the extraction prompt so the model has
domain-specific grounding it would otherwise lack.

Index schema (shipment-context):
  id            : Edm.String  (key)
  content       : Edm.String  (the context text injected into prompts)
  category      : Edm.String  ("carrier" | "location" | "product" |
                               "routing_rule" | "customer" | "other")
  tags          : Collection(Edm.String)
  contentVector : Collection(Edm.Single)  — 1 536 dims (text-embedding-3-small)

Typical usage::

    ctx = ShipmentContextClient()

    # --- indexing (run during onboarding or nightly refresh) ---
    ctx.index_documents([
        {
            "id":       "loc-chicago-dc",
            "content":  "Chicago DC: 1234 Industrial Pkwy, Chicago IL 60601. "
                        "Hours: Mon-Fri 06:00-18:00. Dock code: CHI-01.",
            "category": "location",
            "tags":     ["chicago", "illinois", "warehouse"],
        },
        {
            "id":       "carrier-ryder",
            "content":  "Ryder Transportation Services. SCAC: RYDR. "
                        "Preferred carrier for southeast lanes.",
            "category": "carrier",
            "tags":     ["ryder", "southeast"],
        },
    ])

    # --- retrieval (before each OpenAI extraction call) ---
    snippets = ctx.retrieve(
        query="pickup location Chicago freight tender dry van",
        top=5,
    )
    # snippets → list[str], inject into OpenAI prompt
"""
from __future__ import annotations

from typing import List, Optional

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Minimum cosine-similarity score to accept a context hit.
CONTEXT_SCORE_THRESHOLD: float = 0.72

# Default index name — overridable via AZURE_SEARCH_CONTEXT_INDEX_NAME env var.
_DEFAULT_CONTEXT_INDEX = "shipment-context"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_embedding_client() -> AzureOpenAI:
    return AzureOpenAI(
        api_key=Config.AZURE_OPENAI_KEY,
        api_version=Config.AZURE_OPENAI_API_VERSION,
        azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
    )


def _get_search_client() -> SearchClient:
    index_name = (
        getattr(Config, "AZURE_SEARCH_CONTEXT_INDEX_NAME", None)
        or _DEFAULT_CONTEXT_INDEX
    )
    return SearchClient(
        endpoint=Config.AZURE_SEARCH_ENDPOINT,
        index_name=index_name,
        credential=AzureKeyCredential(Config.AZURE_SEARCH_KEY),
    )


def _embed(text: str) -> list:
    client = _get_embedding_client()
    resp   = client.embeddings.create(
        input=text,
        model=Config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
    )
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# ShipmentContextClient
# ---------------------------------------------------------------------------

class ShipmentContextClient:
    """
    Indexes and retrieves shipment reference context for RAG-augmented extraction.
    """

    # ── Indexing ──────────────────────────────────────────────────────────────

    def index_documents(self, documents: List[dict]) -> int:
        """
        Embed and upsert context documents into the ``shipment-context`` index.

        Each document dict must contain:
            ``id``       — unique string key (must be stable; used for upserts)
            ``content``  — the text that will be embedded and injected into prompts
            ``category`` — one of: carrier | location | product | routing_rule |
                           customer | other
            ``tags``     — list[str] (optional, used for keyword filtering)

        Returns:
            Number of documents successfully indexed.
        """
        if not Config.AZURE_SEARCH_ENDPOINT or not Config.AZURE_SEARCH_KEY:
            logger.warning("Azure AI Search not configured — skipping context indexing")
            return 0

        search_client = _get_search_client()
        batch: List[dict] = []

        for doc in documents:
            doc_id  = doc.get("id", "")
            content = doc.get("content", "")
            if not doc_id or not content:
                logger.warning("Skipping context doc with missing id or content: %s", doc)
                continue

            try:
                vector = _embed(content)
            except Exception as exc:
                logger.error("Failed to embed context doc '%s': %s", doc_id, exc)
                continue

            batch.append({
                "id":            doc_id,
                "content":       content,
                "category":      doc.get("category", "other"),
                "tags":          doc.get("tags", []),
                "contentVector": vector,
            })

        if not batch:
            logger.warning("No valid context documents to index")
            return 0

        try:
            search_client.upload_documents(documents=batch)
            idx = getattr(Config, "AZURE_SEARCH_CONTEXT_INDEX_NAME", _DEFAULT_CONTEXT_INDEX)
            logger.info("Indexed %d context document(s) into '%s'", len(batch), idx)
            return len(batch)
        except Exception as exc:
            logger.error("Failed to upload context documents: %s", exc)
            return 0

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        top: int = 5,
        category: Optional[str] = None,
    ) -> List[str]:
        """
        Retrieve the most relevant context snippets for a free-text query.

        Args:
            query:    Query text derived from the email being processed
                      (e.g. subject + first paragraph of body).
            top:      Maximum number of snippets to return after threshold filtering.
            category: Optional filter — restrict results to a single category
                      ("carrier", "location", "product", "routing_rule", etc.).

        Returns:
            Ordered list of ``content`` strings (highest relevance first).
            Returns an empty list when search is not configured, the query is
            blank, or no results pass the score threshold.
        """
        if not Config.AZURE_SEARCH_ENDPOINT or not Config.AZURE_SEARCH_KEY:
            return []

        if not query.strip():
            return []

        try:
            query_vector = _embed(query)
        except Exception as exc:
            logger.warning("Failed to embed context query: %s", exc)
            return []

        try:
            search_client = _get_search_client()
            vector_query  = VectorizedQuery(
                vector=query_vector,
                k_nearest_neighbors=top,
                fields="contentVector",
            )
            filter_expr = f"category eq '{category}'" if category else None

            results = search_client.search(
                search_text=None,
                vector_queries=[vector_query],
                filter=filter_expr,
                select=["id", "content", "category"],
                top=top,
            )

            snippets: List[str] = []
            for r in results:
                score = r.get("@search.score", 0.0)
                if score >= CONTEXT_SCORE_THRESHOLD:
                    snippets.append(r["content"])
                    logger.debug(
                        "Context hit: id=%s category=%s score=%.3f",
                        r.get("id"), r.get("category"), score,
                    )
                else:
                    logger.debug(
                        "Context below threshold: id=%s score=%.3f — discarded",
                        r.get("id"), score,
                    )

            logger.info(
                "Context retrieval: query_len=%d results=%d/%d (threshold=%.2f)",
                len(query), len(snippets), top, CONTEXT_SCORE_THRESHOLD,
            )
            return snippets

        except Exception as exc:
            logger.warning("Context search failed (non-fatal): %s", exc)
            return []

    # ── Convenience ───────────────────────────────────────────────────────────

    def build_rag_section(self, snippets: List[str]) -> str:
        """
        Format retrieved snippets into a prompt-ready reference block.

        Args:
            snippets: Output of ``retrieve()``.

        Returns:
            A multi-line string to embed in the extraction prompt, or "" if
            ``snippets`` is empty.
        """
        if not snippets:
            return ""
        lines = "\n".join(f"  - {s}" for s in snippets)
        return (
            "\nREFERENCE CONTEXT (from TMS knowledge base — use to "
            "fill or validate extracted values):\n" + lines + "\n"
        )
