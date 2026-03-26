from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Generator, List, Literal, Optional, Sequence, Tuple

import networkx as nx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from db import get_connection
from llm_engine import (
    OUT_OF_SCOPE_RESPONSE,
    classify_intent,
    generate_cypher_equivalent,
    generate_sql,
    is_domain_query,
    synthesize_answer,
)


BACKEND_DIR = Path(__file__).resolve().parent
GRAPH_DATA_PATH = BACKEND_DIR / "graph_data.json"
SCHEMA_SUMMARY_PATH = BACKEND_DIR / "schema_summary.json"


app = FastAPI(title="Dodge AI - FDE API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_graph: Optional[nx.DiGraph] = None


class QueryHistoryItem(BaseModel):
    role: str = "user"
    content: str


class QueryRequest(BaseModel):
    message: str
    history: List[QueryHistoryItem] = []


def _sse_event(event: str, data: Any) -> str:
    if isinstance(data, (dict, list)):
        data_str = json.dumps(data, ensure_ascii=False)
    else:
        data_str = str(data)
    # SSE requires each line in `data:` to be on its own line; we keep it compact.
    data_str = data_str.replace("\r\n", "\n").replace("\r", "\n")
    data_str = "\\n".join(data_str.split("\n"))
    return f"event: {event}\ndata: {data_str}\n\n"


def _sql_safety_normalize(sql: str) -> str:
    sql = sql.strip().rstrip(";")
    # Must be a SELECT only statement.
    if not sql.lower().startswith("select"):
        raise ValueError("Only SELECT statements are allowed.")

    # Block obvious non-SELECT operations.
    banned = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|truncate|pragma|attach|detach)\b", re.I)
    if banned.search(sql):
        raise ValueError("SQL contains disallowed keywords.")
    if ";" in sql:
        raise ValueError("Multiple statements are not allowed.")

    # Put an upper bound on returned rows.
    if not re.search(r"\blimit\b", sql, flags=re.I):
        sql = f"{sql} LIMIT 200"
    return sql


def _convert_identifier_to_snake(name: str) -> str:
    # Convert camelCase/PascalCase to snake_case.
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1)
    s2 = s2.replace("-", "_")
    s2 = re.sub(r"__+", "_", s2)
    return s2.lower()


def _normalize_sql_identifiers(sql: str) -> str:
    """
    Best-effort normalization from camelCase identifiers to snake_case so
    queries generated from `schema_summary.json` work with `graph.db`
    (which uses normalized snake_case column/table names).
    """

    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        # Skip SQL keywords and already-snake tokens.
        if "_" in token:
            return token
        if re.match(r"^[a-z_]+$", token.lower()):
            return token
        # Only convert if it contains an uppercase letter (camel/pascal).
        if re.search(r"[A-Z]", token):
            return _convert_identifier_to_snake(token)
        return token

    # Convert bare identifiers. This is deliberately conservative and may not
    # handle quoted identifiers.
    return re.sub(r"\b[A-Za-z][A-Za-z0-9]*\b", repl, sql)


def _row_to_jsonable_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        d = row
    else:
        # sqlite3.Row -> dict
        d = dict(row)

    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = v.decode("utf-8", errors="replace")
        else:
            out[k] = v
    return out


def _extract_node_ids_from_rows(rows: List[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    seen = set()

    # Per requirement: columns ending in `_id` or `_doc`.
    for row in rows:
        for k, v in row.items():
            if not (k.endswith("_id") or k.endswith("_doc")):
                continue
            if v is None:
                continue
            if isinstance(v, list):
                for item in v:
                    sid = str(item)
                    mapped = _map_raw_id_to_graph_node_id(k, sid)
                    sid = mapped or sid
                    if sid not in seen:
                        ids.append(sid)
                        seen.add(sid)
            else:
                sid = str(v)
                mapped = _map_raw_id_to_graph_node_id(k, sid)
                sid = mapped or sid
                if sid not in seen:
                    ids.append(sid)
                    seen.add(sid)

    # Fallback: if nothing found, return empty so the LLM can respond without citations.
    # (Still conforms to requirement’s primary extraction rule.)
    return ids


def _map_raw_id_to_graph_node_id(column_name: str, raw_id: str) -> Optional[str]:
    """
    Best-effort conversion from raw ids (numeric/string) into the canonical
    node ids used by `graph_data.json`, which are formatted as `Type:<raw_id>`.
    """
    c = column_name.lower()
    rid = str(raw_id)

    if ":" in rid:
        return rid
    if "sales_order" in c or "salesorder" in c:
        return f"SalesOrder:{rid}"
    if "billing_document" in c or "billingdocument" in c:
        return f"BillingDocument:{rid}"
    if c == "customer" or "customer" in c:
        return f"Customer:{rid}"
    if "product" in c or "material" in c:
        return f"Product:{rid}"
    if "delivery_document" in c or "deliverydocument" in c:
        return f"Delivery:{rid}"
    if "accounting_document" in c or "accountingdocument" in c or "journal" in c:
        return f"Finance:{rid}"
    return None


def _load_graph_into_memory() -> nx.DiGraph:
    if not GRAPH_DATA_PATH.exists():
        raise FileNotFoundError(f"Missing graph_data.json at: {GRAPH_DATA_PATH}")

    raw = json.loads(GRAPH_DATA_PATH.read_text(encoding="utf-8"))
    g = nx.DiGraph()
    for n in raw.get("nodes", []):
        nid = n["id"]
        attrs = {k: v for k, v in n.items() if k != "id"}
        g.add_node(nid, **attrs)
    for e in raw.get("edges", []):
        g.add_edge(e["source"], e["target"], relationship=e.get("relationship"))
    return g


def _traverse_graph(traversal_plan_json: str) -> List[Dict[str, Any]]:
    """
    Perform a simple, bounded traversal based on:
    { start_node_type, start_id_hint, traverse_to, max_depth }
    """
    if _graph is None:
        raise RuntimeError("Graph not loaded.")

    plan_raw = traversal_plan_json.strip()
    cleaned = plan_raw.replace("```json", "").replace("```", "").strip()
    try:
        plan = json.loads(cleaned)
    except Exception:
        # If the SDK/model returns something non-JSON, fallback to empty.
        return []

    start_node_type = plan.get("start_node_type")
    start_id_hint = plan.get("start_id_hint")
    traverse_to = plan.get("traverse_to")
    max_depth = int(plan.get("max_depth") or 2)
    max_depth = max(1, min(max_depth, 5))

    allowed_types = {"SalesOrder", "BillingDocument", "Customer", "Product", "Delivery", "Finance"}
    target_type: Optional[str] = traverse_to if traverse_to in allowed_types else None
    relationship_filter: Optional[str] = None
    if target_type is None and isinstance(traverse_to, str) and traverse_to:
        relationship_filter = traverse_to

    # Candidate start nodes by type (and optional id hint).
    start_candidates: List[str] = []
    for nid, attrs in _graph.nodes(data=True):
        if attrs.get("type") != start_node_type:
            continue
        if start_id_hint is None:
            start_candidates.append(nid)
            continue
        hint = str(start_id_hint)
        # node ids in graph_data are like "SalesOrder:<raw_id>"
        if nid.endswith(":" + hint):
            start_candidates.append(nid)
            continue
        # Try metadata/label inclusion as a weak match.
        label = str(attrs.get("label", ""))
        if hint in label:
            start_candidates.append(nid)

    # Bound start candidates for performance.
    if len(start_candidates) > 20:
        start_candidates = start_candidates[:20]

    if not start_candidates:
        return []

    # BFS up to max_depth.
    visited = set(start_candidates)
    frontier = [(nid, 0) for nid in start_candidates]
    reached = set(start_candidates)

    while frontier:
        nid, depth = frontier.pop(0)
        if depth >= max_depth:
            continue

        for _, nbr, edata in _graph.out_edges(nid, data=True):
            rel = edata.get("relationship")
            if relationship_filter is not None:
                rel_str = "" if rel is None else str(rel)
                if relationship_filter.lower() not in rel_str.lower():
                    continue
            if nbr not in visited:
                visited.add(nbr)
                reached.add(nbr)
                frontier.append((nbr, depth + 1))

    # Produce rows. To support `*_id` extraction, we always include `node_id`.
    rows: List[Dict[str, Any]] = []
    for nid in reached:
        attrs = _graph.nodes[nid]
        if target_type and attrs.get("type") != target_type:
            continue
        rows.append(
            {
                "node_id": nid,
                "node_type": attrs.get("type"),
                "cluster_id": attrs.get("cluster_id"),
                "label": attrs.get("label"),
            }
        )
    return rows


@app.on_event("startup")
def startup_event() -> None:
    global _graph
    _graph = _load_graph_into_memory()


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/graph")
def get_graph_data() -> JSONResponse:
    raw = json.loads(GRAPH_DATA_PATH.read_text(encoding="utf-8"))
    # Explicit header (in addition to CORSMiddleware).
    headers = {"Access-Control-Allow-Origin": "http://localhost:5173"}
    return JSONResponse(content=raw, headers=headers)


@app.get("/api/schema")
def get_schema_summary() -> JSONResponse:
    raw = json.loads(SCHEMA_SUMMARY_PATH.read_text(encoding="utf-8"))
    return JSONResponse(content=raw)


@app.post("/api/query")
def query_stream(req: QueryRequest) -> StreamingResponse:
    message = req.message
    history = [h.model_dump() for h in req.history]  # list of {role, content}

    def event_generator() -> Generator[str, None, None]:
        # 1) Domain gate.
        try:
            if not is_domain_query(message):
                rows: List[Dict[str, Any]] = []
                node_ids = _extract_node_ids_from_rows(rows)
                yield _sse_event("node_ids", node_ids)

                # Stream a short rejection via token events.
                msg = OUT_OF_SCOPE_RESPONSE
                parts = msg.split(" ")
                for i, tok in enumerate(parts):
                    if i < len(parts) - 1:
                        tok = tok + " "
                    yield _sse_event("token", tok)

                yield _sse_event(
                    "done",
                    {"generated_query": None, "row_count": 0},
                )
                return
        except Exception as e:
            # If domain routing fails (e.g., missing API key), reject safely.
            node_ids: List[str] = []
            yield _sse_event("node_ids", node_ids)
            yield _sse_event("token", "SYSTEM: Domain routing failed.")
            yield _sse_event("done", {"generated_query": None, "row_count": 0})
            return

        # 2) Intent routing.
        intent = classify_intent(message)

        # 3) Generate query(s).
        sql_text: Optional[str] = None
        sql_text_norm: Optional[str] = None
        traversal_plan_text: Optional[str] = None
        all_rows: List[Dict[str, Any]] = []

        if intent in ("sql", "hybrid"):
            sql_text = generate_sql(message, history)
            sql_text_norm = _normalize_sql_identifiers(_sql_safety_normalize(sql_text))

            conn = get_connection()
            try:
                cur = conn.execute(sql_text_norm)
                rows = cur.fetchall()
                all_rows.extend([_row_to_jsonable_dict(r) for r in rows])
            finally:
                conn.close()

        if intent in ("graph_traversal", "hybrid"):
            traversal_plan_text = generate_cypher_equivalent(message, history)
            traversal_rows = _traverse_graph(traversal_plan_text)
            all_rows.extend(traversal_rows)

        # 4) Extract node_ids.
        node_ids = _extract_node_ids_from_rows(all_rows)

        # 6) Stream with SSE.
        yield _sse_event("node_ids", node_ids)

        # 8) Tokens from synthesize_answer.
        generated_query_for_done: Any
        if intent == "sql":
            generated_query_for_done = sql_text_norm
        elif intent == "graph_traversal":
            generated_query_for_done = traversal_plan_text
        else:
            generated_query_for_done = {"sql": sql_text, "traversal_plan": traversal_plan_text}

        # synthesize_answer() streams Gemini tokens.
        try:
            for token in synthesize_answer(
                message,
                query_result=all_rows,
                node_ids=node_ids,
                history=history,
            ):
                yield _sse_event("token", token)
        except Exception as e:
            # Avoid breaking the SSE stream on LLM errors.
            yield _sse_event("token", f"SYSTEM: Answer generation failed: {e}")

        # 9) Final event.
        yield _sse_event(
            "done",
            {"generated_query": generated_query_for_done, "row_count": len(all_rows)},
        )

    return StreamingResponse(event_generator(), media_type="text/event-stream")

