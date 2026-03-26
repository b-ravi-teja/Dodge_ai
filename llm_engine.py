from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Generator, List, Literal, Optional, Sequence
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = Path(__file__).resolve().parent

SCHEMA_PATH = BACKEND_DIR / "schema_summary.json"
GUARDRAIL_SUFFIX = (
    "If this question is not about the provided business dataset, respond only with:\n"
    "SYSTEM: This question is outside the scope of this dataset."
)

OUT_OF_SCOPE_RESPONSE = "SYSTEM: This question is outside the scope of this dataset."


def _load_env() -> None:
    # Load root `.env` so `GEMINI_API_KEY` is available.
    load_dotenv(dotenv_path=str(REPO_ROOT / ".env"), override=False)


_load_env()
import os

_API_KEY = os.getenv("GEMINI_API_KEY")
_MODEL_NAME = "gemini-2.0-flash-exp"
_model: Optional[Any] = None


def _get_model():
    global _model
    if _model is not None:
        return _model

    if not _API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to the root .env as `GEMINI_API_KEY=your_key_here`."
        )

    # Lazy import to avoid noisy SDK warnings during backend startup.
    import google.generativeai as genai  # type: ignore

    genai.configure(api_key=_API_KEY)
    _model = genai.GenerativeModel(_MODEL_NAME)
    return _model


def _format_history(history: Sequence[Any], max_turns: int = 6) -> str:
    """
    Accepts history as a list of strings or objects like `{role, content}`.
    Returns a compact, prompt-friendly transcript.
    """
    tail = list(history)[-max_turns:]
    lines: List[str] = []
    for item in tail:
        if isinstance(item, str):
            lines.append(item.strip())
            continue
        if isinstance(item, dict) and "content" in item:
            role = str(item.get("role", "user"))
            content = str(item.get("content", ""))
            lines.append(f"{role}: {content}".strip())
            continue
        lines.append(str(item))
    return "\n".join([l for l in lines if l])


def load_schema_context() -> str:
    """
    Reads `schema_summary.json` and formats it as a compact string for prompt injection.
    """
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Missing schema summary at: {SCHEMA_PATH}")
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=1024)
def is_domain_query(user_query: str) -> bool:
    """
    Uses a fast Gemini call to check whether the query is about the provided dataset.
    Cached for identical strings.
    """
    user_query = user_query.strip()
    if not user_query:
        return False

    schema_hint = (
        "Dataset domain keywords include: orders, deliveries, billing, payments, "
        "customers, products, finance."
    )

    system_prompt = (
        "You are a routing assistant. Determine whether the user query is about the provided business dataset.\n"
        f"{schema_hint}\n\n"
        f"{GUARDRAIL_SUFFIX}\n\n"
        "Respond with exactly ONE token: YES or NO."
    )

    resp = _get_model().generate_content(system_prompt + "\n\nUSER_QUERY:\n" + user_query)
    text = getattr(resp, "text", None) or str(resp)
    text = text.strip().upper()
    if text.startswith("YES"):
        return True
    if text.startswith("NO"):
        return False
    # Fallback: conservative.
    return False


@lru_cache(maxsize=1024)
def classify_intent(user_query: str) -> Literal["sql", "graph_traversal", "hybrid"]:
    """
    Classify intent into one of: sql | graph_traversal | hybrid

    - sql: aggregations, counts, filters, rankings
    - graph_traversal: tracing flows, finding paths, incomplete chains
    - hybrid: both
    """
    user_query = user_query.strip()
    if not user_query:
        return "sql"

    system_prompt = (
        "You are a routing assistant. Classify the intent of the user's request.\n"
        "- sql: aggregations, counts, filters, rankings\n"
        "- graph_traversal: tracing flows, finding paths, incomplete chains\n"
        "- hybrid: both sql and graph traversal\n\n"
        f"{GUARDRAIL_SUFFIX}\n\n"
        "Return exactly one token from: sql, graph_traversal, hybrid."
    )

    resp = _get_model().generate_content(system_prompt + "\n\nUSER_QUERY:\n" + user_query)
    text = (getattr(resp, "text", None) or str(resp)).strip().lower()

    if "hybrid" in text:
        return "hybrid"
    if "graph" in text or "traversal" in text:
        return "graph_traversal"
    return "sql"


def _expect_select_only(sql_text: str) -> str:
    # Remove common wrappers the model might add.
    cleaned = sql_text.strip()
    cleaned = cleaned.replace("```sql", "").replace("```", "").strip()

    # Extract the first SELECT ... (best-effort).
    idx = cleaned.lower().find("select")
    if idx != -1:
        cleaned = cleaned[idx:]

    # Guardrails: must start with SELECT and not include non-SQL prose.
    first = cleaned.splitlines()[0].strip().upper() if cleaned else ""
    if not first.startswith("SELECT"):
        raise ValueError("Model did not return a SELECT statement.")
    return cleaned


def generate_sql(user_query: str, history: list) -> str:
    """
    Generate a SQL SELECT statement using the full schema context.
    History: last 6 conversation turns.
    """
    if not is_domain_query(user_query):
        return OUT_OF_SCOPE_RESPONSE

    schema_ctx = load_schema_context()
    transcript = _format_history(history, max_turns=6)

    few_shot: List[str] = [
        # COUNT query
        "User: How many sales orders are there?\nAssistant: SELECT COUNT(*) AS sales_order_count FROM sales_order_headers",
        # JOIN across 3 tables
        "User: How many billing and delivery records reference sales orders?\n"
        "Assistant: SELECT COUNT(*) AS billed_delivery_record_count\n"
        "FROM sales_order_headers soh\n"
        "JOIN billing_document_items bdi ON bdi.reference_sd_document = soh.sales_order\n"
        "JOIN outbound_delivery_items odi ON odi.reference_sd_document = soh.sales_order\n"
        "WHERE bdi.material IS NOT NULL",
        # WHERE filter
        "User: Show sales orders where sales_organization is not null.\nAssistant: SELECT * FROM sales_order_headers WHERE sales_organization IS NOT NULL",
    ]

    system_prompt = (
        "You are an expert analytics engineer. Generate correct SQLite-compatible SQL.\n"
        "Use the provided schema_summary.json as the authoritative schema.\n\n"
        f"SCHEMA_SUMMARY_JSON (full): {schema_ctx}\n\n"
        "Few-shot examples:\n"
        + "\n\n".join(few_shot)
        + "\n\n"
        "History (last 6 turns):\n"
        f"{transcript}\n\n"
        "Rules:\n"
        "- Return ONLY a valid SQL SELECT statement. No explanation. No markdown.\n"
        "- Use table names and column names exactly as defined in the schema.\n"
        f"{GUARDRAIL_SUFFIX}"
    )

    resp = _get_model().generate_content(system_prompt + "\n\nUSER_QUERY:\n" + user_query)
    text = getattr(resp, "text", None) or str(resp)
    return _expect_select_only(text)


def generate_cypher_equivalent(user_query: str, history: list) -> str:
    """
    For graph traversal — returns a Python NetworkX traversal plan as JSON:
    { start_node_type, start_id_hint, traverse_to, max_depth }
    """
    if not is_domain_query(user_query):
        return OUT_OF_SCOPE_RESPONSE

    transcript = _format_history(history, max_turns=6)
    system_prompt = (
        "You are a planning assistant for graph traversal.\n"
        "Return a JSON object only with these keys:\n"
        "- start_node_type (one of SalesOrder, BillingDocument, Customer, Product, Delivery, Finance)\n"
        "- start_id_hint (string; if no id is provided in the query, use null)\n"
        "- traverse_to (string; which other node types/relationships to follow)\n"
        "- max_depth (integer; small if unsure)\n\n"
        f"{GUARDRAIL_SUFFIX}\n\n"
        "JSON ONLY. No markdown."
    )

    resp = _get_model().generate_content(
        system_prompt + "\n\nHISTORY:\n" + transcript + "\n\nUSER_QUERY:\n" + user_query
    )
    text = getattr(resp, "text", None) or str(resp)
    # Best-effort: strip markdown wrappers.
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    # Validate it's JSON; if not, return raw cleaned.
    try:
        json.loads(cleaned)
    except Exception:
        pass
    return cleaned


def synthesize_answer(
    user_query: str,
    query_result: list,
    node_ids: list,
    history: list,
) -> Generator[str, None, None]:
    """
    Streaming generator. Uses Gemini streaming to yield tokens one by one.
    """
    transcript = _format_history(history, max_turns=6)

    system_prompt = (
        "You are a business data analyst. Answer using ONLY the provided data rows.\n"
        "Always cite the specific IDs you reference.\n"
        "If data is empty, say no matching records were found.\n\n"
        "Guardrails:\n"
        "- Do not invent data.\n"
        "- If user asks for something outside the dataset, follow the guardrail suffix.\n\n"
        f"{GUARDRAIL_SUFFIX}"
    )

    # Keep the payload compact but complete.
    data_payload = json.dumps(
        {
            "user_query": user_query,
            "query_result": query_result,
            "node_ids": node_ids,
            "history": transcript,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    contents = system_prompt + "\n\nDATA_PAYLOAD:\n" + data_payload

    stream = _get_model().generate_content(contents, stream=True)

    for chunk in stream:
        token = getattr(chunk, "text", None)
        if token:
            yield token
        else:
            # Fallback for some SDK versions.
            try:
                parts = getattr(chunk, "parts", None)
                if parts:
                    for p in parts:
                        if getattr(p, "text", None):
                            yield p.text
            except Exception:
                continue

