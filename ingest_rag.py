#!/usr/bin/env python3

"""ingest_rag.py

CLI utility for building a Retrieval‑Augmented Generation (RAG) vector
database with Qdrant.

The tool is intentionally *source‑agnostic* and *schema‑agnostic* – it relies
on the `docling` project to ingest documents from *any* kind of data source
(local files, URLs, databases, etc.).  After the documents are loaded, their
content is embedded with OpenAI’s `text-embedding-3-large` model and stored in
a Qdrant collection.

Example
-------
    $ OPENAI_API_KEY=... python ingest_rag.py --source ./my_corpus \
          --collection my_rag_collection

Requirements
------------
    pip install qdrant-client docling openai tqdm

Environment variables
---------------------
OPENAI_API_KEY
    Your OpenAI API key.  It can also be passed explicitly with the
    ``--openai-api-key`` CLI option (the environment variable takes
    precedence).
QDRANT_API_KEY
    If you are using a managed Qdrant Cloud instance, set your API key here or
    use the ``--qdrant-api-key`` option.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Sequence

# Core dependencies
import click
from tqdm.auto import tqdm
import re
from dateutil.parser import parse as _parse_date

# Regex to detect ISO dates (YYYY-MM-DD) in text
DATE_REGEX = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# deterministic UUID generation does not require hashlib

# Optional dependencies – import lazily so that the error message is clearer.


def _lazy_import(name: str):
    try:
        return __import__(name)
    except ImportError as exc:  # pragma: no cover – dev convenience
        click.echo(
            f"[fatal] The Python package '{name}' is required but not installed.\n"
            f"Install it with: pip install {name}",
            err=True,
        )
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Document:
    """A minimal representation of a document to be embedded."""

    content: str
    metadata: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def iter_batches(seq: Sequence[Document], batch_size: int) -> Iterable[List[Document]]:
    """Yield items *seq* in lists of length *batch_size* (the last one may be shorter)."""

    batch: list[Document] = []
    for item in seq:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch
 
def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split *text* into chunks of up to *max_chars* characters, breaking on whitespace."""
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        # If not at end, try to break at last whitespace before end
        if end < length:
            split_at = text.rfind(' ', start, end)
            if split_at > start:
                end = split_at
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks
    
def _smart_chunk_text(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    """
    Chunk text on paragraph and sentence boundaries up to max_chars,
    and apply character-level overlap between chunks.
    """
    # Split into paragraphs
    paragraphs = re.split(r'\n\s*\n', text)
    chunks: list[str] = []
    current_paras: list[str] = []
    current_len = 0
    for para in paragraphs:
        p = para.strip()
        if not p:
            continue
        # If paragraph itself is too long, split by sentences
        if len(p) > max_chars:
            # flush existing
            if current_paras:
                chunk = "\n\n".join(current_paras).strip()
                if chunk:
                    chunks.append(chunk)
                current_paras = []
                current_len = 0
            # split paragraph into sentences
            sentences = re.split(r'(?<=[\.!?])\s+', p)
            curr = ""
            for sent in sentences:
                s = sent.strip()
                if not s:
                    continue
                if len(curr) + len(s) <= max_chars:
                    curr = f"{curr} {s}".strip() if curr else s
                else:
                    if curr:
                        chunks.append(curr)
                    curr = s
            if curr:
                chunks.append(curr)
        else:
            # Add paragraph to current group
            if current_len + len(p) + 2 <= max_chars:
                current_paras.append(p)
                current_len += len(p) + 2
            else:
                # flush existing
                chunk = "\n\n".join(current_paras).strip()
                if chunk:
                    chunks.append(chunk)
                current_paras = [p]
                current_len = len(p) + 2
    # flush remainder
    if current_paras:
        chunk = "\n\n".join(current_paras).strip()
        if chunk:
            chunks.append(chunk)
    # apply overlap (character-level)
    if overlap > 0 and len(chunks) > 1:
        overlapped: list[str] = []
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                overlapped.append(chunk)
            else:
                prev = overlapped[idx-1]
                # take last overlap chars from previous chunk
                ov = prev[-overlap:] if len(prev) >= overlap else prev
                overlapped.append(f"{ov} {chunk}")
        return overlapped
    return chunks


def get_openai_client(api_key: str):
    openai = _lazy_import("openai")
    # lazily set api key; property name changed in v1 openai lib
    try:
        openai.api_key = api_key
    except AttributeError:  # openai>=1.0
        client = openai.OpenAI(api_key=api_key)
        return client
    return openai


# ---------------------------------------------------------------------------
# Main ingestion routine
# ---------------------------------------------------------------------------


def load_documents(source: str, chunk_size: int = 500, overlap: int = 50, crawl_depth: int = 0) -> List[Document]:
    """
    Use *docling* to load and chunk documents from *source*.

    The function tries to stay *schema-agnostic*.  For every item docling
    yields, we keep its raw representation as the ``payload`` (metadata), and
    attempt to locate a reasonable textual representation for embedding.
    """
    # ---------------------------------------------------------------------
    # Helper: chunk arbitrary raw *text* into smaller passages.  We keep the
    # helper nested so it can implicitly capture the *chunk_size* / *overlap*
    # parameters from the enclosing ``load_documents`` call, but we define it
    # right at the beginning of the function so that every subsequent code
    # path can freely reference it.
    # ---------------------------------------------------------------------

    def _chunk_text_tokenwise(text: str, metadata: dict[str, object]) -> List[Document]:
        """Split *text* into token-aware chunks, falling back to character
        splitting if the preferred tokenisers are unavailable.  A small helper
        that always returns a ``List[Document]`` with a ``chunk_index``
        injected into the copied *metadata* for downstream processing."""

        docs_out: list[Document] = []

        # 1. Try docling's GPT-aware splitter.
        try:
            from docling.text import TextSplitter

            splitter = TextSplitter.from_model(  # type: ignore[attr-defined]
                model="gpt-4.1-mini",
                chunk_size=chunk_size,
                chunk_overlap=overlap,
            )
            chunks = splitter.split(text)  # type: ignore[attr-defined]
        except Exception:
            # 2. Try LangChain's recursive character splitter.
            try:
                from langchain.text_splitter import RecursiveCharacterTextSplitter  # type: ignore

                lc_splitter = RecursiveCharacterTextSplitter(
                    chunk_size=chunk_size,
                    chunk_overlap=overlap,
                    separators=["\n\n", "\n", " "],
                )
                chunks = lc_splitter.split_text(text)
            except Exception:
                # 3. Absolute last-ditch fallback – a very dumb char splitter.
                chunks = _smart_chunk_text(text, chunk_size, overlap)

        for idx, chunk in enumerate(chunks):
            new_meta = dict(metadata)
            new_meta["chunk_index"] = idx
            docs_out.append(Document(content=chunk, metadata=new_meta))

        return docs_out

    # ------------------------------------------------------------------
    # If *source* is a URL, prefer the unstructured/LangChain loader.
    # ------------------------------------------------------------------

    if source.lower().startswith(("http://", "https://")):
        # Attempt LangChain UnstructuredURLLoader
        try:
            # ------------------------------------------------------------------
            # LangChain split up into *langchain* (core) and *langchain-community*.
            # The URL loader we need was moved to the latter.  We therefore try
            # the new location first, then fall back to the pre-split paths so
            # that older installations remain compatible.
            # ------------------------------------------------------------------

            try:  # New ≥0.1.0 structure
                from langchain_community.document_loaders import (  # type: ignore
                    UnstructuredURLLoader,
                )

            except ImportError:
                try:  # Legacy structure (≤0.0.x)
                    from langchain.document_loaders import UnstructuredURLLoader  # type: ignore
                except ImportError:
                    from langchain.document_loaders.unstructured_url import (  # type: ignore
                        UnstructuredURLLoader,
                    )

            loader = UnstructuredURLLoader(urls=[source])
            raw_docs = loader.load()
            documents: list[Document] = []
            for raw in raw_docs:
                text = raw.page_content
                meta = raw.metadata or {}
                documents.extend(_chunk_text_tokenwise(text, meta))
            return documents
        except ImportError:
            click.echo(
                "[warning] langchain or UnstructuredURLLoader not available; cannot load URL."
                " Please install langchain and unstructured.",
                err=True,
            )
        except Exception as e:
            click.echo(f"[warning] LangChain URL load failed for '{source}': {e}", err=True)
        # Fallback: Unstructured.io partition of remote HTML
        try:
            import requests, tempfile, os as _os
            from unstructured.partition.html import partition_html
            from unstructured.documents.elements import Table
            # Fetch remote HTML
            resp = requests.get(source, timeout=60)
            resp.raise_for_status()
            # Write to temp file
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
            tmp_path = tmp.name
            tmp.write(resp.content)
            tmp.close()
            # Partition HTML into elements
            elements = partition_html(tmp_path)
            docs_url: list[Document] = []
            for elem in elements:
                if isinstance(elem, Table):
                    try:
                        md = elem.to_markdown()
                    except Exception:
                        md = elem.get_text()
                    docs_url.append(Document(content=md, metadata={"source": source, "is_table": True}))
                elif hasattr(elem, 'text') and isinstance(elem.text, str):
                    txt = elem.text
                    docs_url.extend(_chunk_text_tokenwise(txt, {"source": source}))
            try:
                _os.remove(tmp_path)
            except Exception:
                pass
            return docs_url
        except Exception as e2:
            click.echo(f"[warning] Unstructured URL fallback failed: {e2}", err=True)
        # Fallback to generic extractor

        # If source is a local PDF, use Unstructured.io for layout-aware parsing with fallback
    if os.path.isfile(source):
        ext = os.path.splitext(source)[1].lower()
        # PDF parsing
        if ext == '.pdf':
            try:
                from unstructured.partition.pdf import partition_pdf
                from unstructured.documents.elements import Table
                elements = partition_pdf(source)
                docs_pdf: list[Document] = []
                for elem in elements:
                    if isinstance(elem, Table):
                        try:
                            md = elem.to_markdown()
                        except Exception:
                            md = elem.get_text()
                        docs_pdf.append(Document(content=md, metadata={"source": source, "is_table": True}))
                    elif hasattr(elem, 'text') and isinstance(elem.text, str):
                        txt = elem.text
                        docs_pdf.extend(_chunk_text_tokenwise(txt, {"source": source}))
                return docs_pdf
            except Exception as e:
                click.echo(f"[warning] Unstructured PDF parse failed for '{source}': {e}", err=True)
                # Fallback: plain-text chunking
                try:
                    import io, os as _os
                    from unstructured.partition.text import partition_text
                    text_elems = partition_text(source)
                    texts = [e.text for e in text_elems if hasattr(e, 'text')]
                    return [d for t in texts for d in _chunk_text_tokenwise(t, {"source": source})]
                except Exception:
                    # Final fallback: read raw text
                    try:
                        with open(source, 'r', encoding='utf-8', errors='ignore') as fh:
                            full_text = fh.read()
                        return [Document(content=chunk, metadata={"source": source, "chunk_index": idx})
                                for idx, chunk in enumerate(_smart_chunk_text(full_text, chunk_size, overlap))]
                    except Exception:
                        pass
        # HTML parsing
        if ext in ('.html', '.htm'):
            try:
                from unstructured.partition.html import partition_html
                from unstructured.documents.elements import Table
                elements = partition_html(source)
                docs_html: list[Document] = []
                for elem in elements:
                    if isinstance(elem, Table):
                        try:
                            md = elem.to_markdown()
                        except Exception:
                            md = elem.get_text()
                        docs_html.append(Document(content=md, metadata={"source": source, "is_table": True}))
                    elif hasattr(elem, 'text') and isinstance(elem.text, str):
                        txt = elem.text
                        docs_html.extend(_chunk_text_tokenwise(txt, {"source": source}))
                return docs_html
            except Exception as e:
                click.echo(f"[warning] Unstructured HTML parse failed for '{source}': {e}", err=True)
                # Fallback: read raw HTML text
                try:
                    from bs4 import BeautifulSoup
                    with open(source, 'r', encoding='utf-8', errors='ignore') as fh:
                        soup = BeautifulSoup(fh, 'html.parser')
                    text = soup.get_text(separator='\n')
                    return [Document(content=chunk, metadata={"source": source, "chunk_index": idx})
                            for idx, chunk in enumerate(_smart_chunk_text(text, chunk_size, overlap))]
                except Exception:
                    pass


    # Try docling extract to get raw text, then chunk by tokens (fallback to char-chunks)
    # (definition moved to top of function so that it can be referenced from
    # earlier code paths – see above.)

    try:
        try:
            import docling.extract as dlextract
        except ImportError:
            import docling_core.extract as dlextract
        extractor = dlextract.TextExtractor(path=source, include_comments=True)
        extracted = extractor.run()
        documents: list[Document] = []
        for doc in extracted:
            if hasattr(doc, "text") and isinstance(doc.text, str):
                txt = doc.text
            elif hasattr(doc, "content") and isinstance(doc.content, str):
                txt = doc.content
            else:
                txt = str(doc)
            meta = getattr(doc, "metadata", {}) or {}
            documents.extend(_chunk_text_tokenwise(txt, meta))
        return documents
    except ImportError:
        # docling.extract not available; fall back to legacy loader
        pass
    except Exception as e:
        click.echo(f"[warning] docling extract failed: {e}", err=True)
        click.echo("[warning] Falling back to legacy loader...", err=True)
    # Otherwise, delegate to legacy docling if available
    try:
        docling = _lazy_import("docling")
    except SystemExit:
        # docling not installed – fall back to naïve whitespace chunking of the
        # entire file.  This avoids a hard dependency on docling when the user
        # simply wants to ingest a plain‑text transcript.
        try:
            with open(source, "r", encoding="utf-8", errors="replace") as fh:
                full_text = fh.read()
        except Exception as e:
            click.echo(f"[fatal] Could not read source '{source}': {e}", err=True)
            sys.exit(1)

        # Chunk with smarter boundaries and overlap
        text_chunks = _smart_chunk_text(full_text, chunk_size, overlap)
        return [
            Document(content=chunk, metadata={"source": source, "chunk_index": idx})
            for idx, chunk in enumerate(text_chunks)
        ]

    # Try old docling API: load() (pass chunk_size and overlap if supported)
    if hasattr(docling, "load"):
        try:
            dataset = docling.load(source, chunk_size=chunk_size, overlap=overlap)  # type: ignore[attr-defined]
        except TypeError:
            dataset = docling.load(source)  # type: ignore[attr-defined]
    # Try legacy API: DocumentSet
    elif hasattr(docling, "DocumentSet"):
        dataset = docling.DocumentSet(source)  # type: ignore
    # Try new API in submodule: DocumentConverter
    else:
        try:
            dc_mod = __import__("docling.document_converter", fromlist=["DocumentConverter"])
            converter = dc_mod.DocumentConverter()
            conv_res = converter.convert(source)
            doc_obj = conv_res.document
            if hasattr(doc_obj, "export_to_text") and callable(doc_obj.export_to_text):
                text = doc_obj.export_to_text()
            else:
                text = str(doc_obj)
            meta = {"source": source}
            # Chunk converted document into tokens
            return _chunk_text_tokenwise(text, meta)
        except Exception as e:
            click.echo(f"[warning] docling.document_converter failed for '{source}': {e}. Falling back to plain-text chunking.", err=True)
            try:
                with open(source, "r", encoding="utf-8", errors="replace") as fh:
                    full_text = fh.read()
            except Exception as e2:
                click.echo(f"[fatal] Could not read source '{source}': {e2}", err=True)
                sys.exit(1)
            # Chunk with smarter boundaries and overlap
            chunks = _smart_chunk_text(full_text, chunk_size, overlap)
            return [Document(content=chunk, metadata={"source": source, "chunk_index": idx})
                    for idx, chunk in enumerate(chunks)]

    documents: list[Document] = []

    # Docling objects are generally iterable; we guard for attribute names.
    if hasattr(dataset, "documents"):
        iterable = dataset.documents
    else:
        iterable = dataset  # assume it's already iterable

    for doc in iterable:
        # heuristically determine textual content
        text = None

        # If doc has attribute 'text' or 'content', use it; otherwise str(doc)
        if hasattr(doc, "text") and isinstance(doc.text, str):
            text = doc.text
        elif hasattr(doc, "content") and isinstance(doc.content, str):
            text = doc.content
        else:
            text = str(doc)

        metadata = {}

        # If doc has id, title etc, capture them as metadata
        for key in ("id", "title", "name", "source", "path", "url"):
            if hasattr(doc, key):
                metadata[key] = getattr(doc, key)

        # store full raw json of doc (might not be serialisable). We attempt safe conversion.
        try:
            raw_json = json.loads(json.dumps(doc, default=str))
            metadata["raw"] = raw_json
        except (TypeError, ValueError):
            metadata["raw"] = str(doc)

        # Chunk legacy documents token-wise (fallback by char if token splitter missing)
        for chunk_doc in _chunk_text_tokenwise(text, metadata):
            documents.append(chunk_doc)

    return documents


def ensure_collection(client, collection_name: str, vector_size: int, distance: str = "Cosine") -> None:
    qdrant_client = _lazy_import("qdrant_client")
    from qdrant_client.http import models as rest

    existing_collections = {c.name for c in client.get_collections().collections}
    if collection_name in existing_collections:
        return

    click.echo(f"[info] Creating collection '{collection_name}' (size={vector_size}, distance={distance})")

    client.create_collection(
        collection_name=collection_name,
        vectors_config=rest.VectorParams(size=vector_size, distance=distance),
    )


def embed_and_upsert(
    client,
    collection: str,
    docs: List[Document],
    openai_client,
    batch_size: int = 100,
    model_name: str = "text-embedding-3-large",
    deterministic_id: bool = False,
):
    """Embed *docs* in batches and upsert them into Qdrant."""

    # Determine which OpenAI binding style is active
    is_openai_v1 = hasattr(openai_client, "embeddings")  # v1+ has .embeddings.create

    from qdrant_client.http import models as rest

    doc_iter = tqdm(iter_batches(docs, batch_size), total=(len(docs) + batch_size - 1) // batch_size, desc="Embedding & upserting")

    for batch in doc_iter:
        texts = [d.content for d in batch]

        if is_openai_v1:
            embeddings_response = openai_client.embeddings.create(model=model_name, input=texts)
            embeddings = [record.embedding for record in embeddings_response.data]
        else:  # old openai<=0.28 style
            embeddings_response = openai_client.Embedding.create(model=model_name, input=texts)
            embeddings = [record["embedding"] for record in embeddings_response["data"]]

        points = []
        for doc, vector in zip(batch, embeddings):
            metadata = doc.metadata.copy()
            content = doc.content
            if deterministic_id:
                # Compute a deterministic UUID5 from metadata and content
                try:
                    meta_str = json.dumps(metadata, sort_keys=True, default=str)
                except Exception:
                    meta_str = str(metadata)
                id_input = meta_str + "\n" + content
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, id_input))
            else:
                point_id = str(uuid.uuid4())
            payload = metadata
            payload["chunk_text"] = content
            points.append(
                rest.PointStruct(id=point_id, vector=vector, payload=payload)
            )

        client.upsert(collection_name=collection, points=points)


# ---------------------------------------------------------------------------
# Command‑line interface
# ---------------------------------------------------------------------------


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, readable=True),
    default=None,
    help="Path to .env file with environment variables (if present, loaded before other options)."
)
@click.option("--source", required=True, help="Path/URL/DSN pointing to the corpus to ingest.")
@click.option("--collection", default="rag_data", show_default=True, help="Qdrant collection name to create/use.")
@click.option("--batch-size", type=int, default=100, show_default=True, help="Embedding batch size.")
@click.option("--openai-api-key", envvar="OPENAI_API_KEY", help="Your OpenAI API key (can also use env var OPENAI_API_KEY)")
@click.option("--qdrant-host", default="localhost", show_default=True, help="Qdrant host (ignored when --qdrant-url is provided).")
@click.option("--qdrant-port", type=int, default=6333, show_default=True, help="Qdrant port (ignored when --qdrant-url is provided).")
@click.option("--qdrant-url", help="Full Qdrant URL (e.g. https://*.qdrant.io:6333). Overrides host/port.")
@click.option("--qdrant-api-key", envvar="QDRANT_API_KEY", help="Qdrant API key if required (Cloud).")
@click.option("--distance", type=click.Choice(["Cosine", "Dot", "Euclid"], case_sensitive=False), default="Cosine", help="Vector distance metric.")
@click.option("--chunk-size", type=int, default=500, show_default=True, help="Chunk size (tokens) for docling chunker.")
@click.option("--chunk-overlap", type=int, default=50, show_default=True, help="Overlap (tokens) between chunks.")
@click.option("--crawl-depth", type=int, default=0, show_default=True, help="When SOURCE is a URL, crawl hyperlinks up to this depth (0=no crawl).")
@click.option("--generate-summaries/--no-generate-summaries", default=False, show_default=True,
              help="Generate and index brief summaries of each chunk for multi-granularity retrieval.")
@click.option("--quality-checks/--no-quality-checks", default=False, show_default=True,
              help="Perform post-ingest quality checks on chunk sizes and entity consistency.")
@click.option(
    "--bm25-index",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Path to write BM25 index JSON mapping point IDs to chunk_text."
         " Defaults to '<collection>_bm25_index.json'.",
)
def cli(
    env_file: str | None,
    source: str,
    collection: str,
    batch_size: int,
    openai_api_key: str | None,
    qdrant_host: str,
    qdrant_port: int,
    qdrant_url: str | None,
    qdrant_api_key: str | None,
    distance: str,
    chunk_size: int,
    chunk_overlap: int,
    crawl_depth: int,
    generate_summaries: bool,
    quality_checks: bool,
    bm25_index: str | None,
) -> None:
    """Ingest *SOURCE* into a Qdrant RAG database using OpenAI embeddings."""

    # ---------------------------------------------------------------------
    # ---------------------------------------------------------------------
    # Load .env file (if present) BEFORE reading env vars / flags
    # ---------------------------------------------------------------------

    if env_file and os.path.isfile(env_file):
        try:
            dotenv = _lazy_import("dotenv")
            dotenv.load_dotenv(env_file, override=False)
            click.echo(f"[info] Environment variables loaded from {env_file}")
        except SystemExit:
            raise
        except Exception:  # pragma: no cover – edge‑case, continue silently
            pass

    # Accept lower‑case variants (e.g. `openai_api_key=`) for convenience
    if "OPENAI_API_KEY" not in os.environ and "openai_api_key" in os.environ:
        os.environ["OPENAI_API_KEY"] = os.environ["openai_api_key"]
    if "QDRANT_API_KEY" not in os.environ and "qdrant_api_key" in os.environ:
        os.environ["QDRANT_API_KEY"] = os.environ["qdrant_api_key"]

    # ---------------------------------------------------------------------
    # Validate & set up dependencies
    # ---------------------------------------------------------------------

    # If the CLI flag wasn't provided, fall back to the environment
    if openai_api_key is None:
        openai_api_key = os.environ.get("OPENAI_API_KEY")

    if openai_api_key is None:
        click.echo(
            "[fatal] OPENAI_API_KEY is not set. Provide --openai-api-key, set it in the"\
            " environment, or put it in the .env file.",
            err=True,
        )
        sys.exit(1)

    qdrant_client = _lazy_import("qdrant_client")

    # Build Qdrant client
    if qdrant_url:
        client = qdrant_client.QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    else:
        client = qdrant_client.QdrantClient(host=qdrant_host, port=qdrant_port, api_key=qdrant_api_key)

    # ---------------------------------------------------------------------
    # Load documents via docling
    # ---------------------------------------------------------------------

    click.echo(f"[info] Loading documents from source: {source} (chunk_size={chunk_size}, overlap={chunk_overlap}, crawl_depth={crawl_depth})")
    documents = load_documents(source, chunk_size, chunk_overlap, crawl_depth)
    click.echo(f"[info] Loaded {len(documents)} document(s)")

    if not documents:
        click.echo("[warning] No documents found – nothing to do.")
        return

    # -----------------------------------------------------------------
    # Enhanced metadata, quality checks, and optional summarization
    # -----------------------------------------------------------------
    # Annotate each chunk with enhanced metadata
    for idx, doc in enumerate(documents):
        # Source file or URL
        doc.metadata.setdefault("source", source)
        # Section title: first line of chunk
        first_line = doc.content.split("\n", 1)[0].strip()
        doc.metadata.setdefault("section", first_line[:100])
        # Neighboring chunk indices for stitching
        doc.metadata.setdefault("neighbor_prev", idx - 1 if idx > 0 else None)
        doc.metadata.setdefault("neighbor_next", idx + 1 if idx < len(documents) - 1 else None)
        # Date detection: ISO or general date parsing
        if "date" not in doc.metadata:
            m = DATE_REGEX.search(doc.content)
            if m:
                # Found ISO-format date
                doc.metadata["date"] = m.group(1)
            else:
                # Try fuzzy parsing for other date formats
                try:
                    dt = _parse_date(doc.content, fuzzy=True)
                    # Only accept reasonable years
                    if dt.year and dt.year >= 1900:
                        doc.metadata["date"] = dt.date().isoformat()
                except Exception:
                    pass

    # Post-ingest quality checks on chunk sizes
    if quality_checks:
        for doc in documents:
            token_count = len(doc.content.split())
            if token_count < chunk_overlap or token_count > chunk_size * 2:
                click.echo(
                    f"[warning] Chunk index={doc.metadata.get('chunk_index')} "
                    f"token_count={token_count} out of bounds (<{chunk_overlap} or >{chunk_size * 2})",
                    err=True,
                )

    # LLM-assisted summarization for multi-granularity indexing
    summary_docs: list[Document] = []
    if generate_summaries:
        click.echo(f"[info] Generating summaries for {len(documents)} chunks...")
        llm = get_openai_client(openai_api_key)
        for doc in documents:
            # Skip very short chunks
            if len(doc.content) < 200:
                continue
            system_msg = {"role": "system", "content": "You are a helpful assistant."}
            user_msg = {
                "role": "user",
                "content": f"Provide a concise (1-2 sentences) summary of the following text:\n\n{doc.content}",
            }
            try:
                if hasattr(llm, "chat"):
                    resp = llm.chat.completions.create(
                        model="gpt-4.1-nano", messages=[system_msg, user_msg]
                    )
                    summary = resp.choices[0].message.content.strip()
                else:
                    resp = llm.ChatCompletion.create(
                        model="gpt-4.1-nano", messages=[system_msg, user_msg]
                    )
                    summary = resp.choices[0].message.content.strip()  # type: ignore
            except Exception as e:
                click.echo(f"[warning] Summary generation failed: {e}", err=True)
                continue
            meta = doc.metadata.copy()
            meta["is_summary"] = True
            summary_docs.append(Document(content=summary, metadata=meta))
        # Append summary-level Documents for indexing
        documents.extend(summary_docs)

    # ---------------------------------------------------------------------
    # Create collection if it does not exist
    # ---------------------------------------------------------------------

    VECTOR_SIZE = 3072  # text-embedding-3-large output dimension

    ensure_collection(client, collection, vector_size=VECTOR_SIZE, distance=distance)

    # ---------------------------------------------------------------------
    # Embed & upsert
    # ---------------------------------------------------------------------

    openai_client = get_openai_client(openai_api_key)

    embed_and_upsert(client, collection, documents, openai_client, batch_size=batch_size)

    click.secho(f"\n[success] Ingestion completed. Collection '{collection}' now holds the embeddings.", fg="green")
    # ---------------------------------------------------------------------
    # Build BM25 index JSON mapping point IDs to chunk_text
    # ---------------------------------------------------------------------
    # Determine output path
    index_path = bm25_index or f"{collection}_bm25_index.json"
    click.echo(f"[info] Building BM25 index JSON at {index_path}")
    id2text: dict[str, str] = {}
    offset = None
    # Scroll through entire collection to collect chunk_text
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            scroll_filter=None,
            limit=1000,
            offset=offset,
            with_payload=True,
        )
        if not records:
            break
        for rec in records:
            payload = getattr(rec, 'payload', {}) or {}
            text = payload.get("chunk_text")
            if isinstance(text, str) and text:
                id2text[rec.id] = text
        if offset is None:
            break
    try:
        with open(index_path, "w") as f:
            json.dump(id2text, f)
        click.secho(f"[success] BM25 index written to {index_path}", fg="green")
    except Exception as e:
        click.echo(f"[warning] Failed to write BM25 index: {e}", err=True)


if __name__ == "__main__":
    cli()
