import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import networkx as nx
import pandas as pd

try:
    import community as community_louvain  # type: ignore
except Exception:  # pragma: no cover
    community_louvain = None


@dataclass(frozen=True)
class NodeTypeSpec:
    type_name: str
    id_column_candidates: Tuple[str, ...]
    # directories whose tables represent rows belonging to this node type
    source_directories: Tuple[str, ...]
    label_from: str


REPO_ROOT = Path(__file__).resolve().parents[1]

# The user asked for `/data/sap-02c-data`. In this workspace the folder is currently
# represented as `data -> sap-o2c-data`, so we fall back gracefully.
REQUESTED_DATA_DIR = REPO_ROOT / "data" / "sap-02c-data"
FALLBACK_DATA_DIR = REPO_ROOT / "data"

DATA_DIR = REQUESTED_DATA_DIR if REQUESTED_DATA_DIR.exists() else FALLBACK_DATA_DIR

SUPPORTED_NODE_TYPES: List[NodeTypeSpec] = [
    NodeTypeSpec(
        type_name="SalesOrder",
        id_column_candidates=("salesOrder",),
        source_directories=("sales_order_headers", "sales_order_items", "sales_order_schedule_lines"),
        label_from="salesOrder",
    ),
    NodeTypeSpec(
        type_name="BillingDocument",
        id_column_candidates=("billingDocument",),
        source_directories=("billing_document_headers", "billing_document_items"),
        label_from="billingDocument",
    ),
    NodeTypeSpec(
        type_name="Customer",
        id_column_candidates=("customer",),
        source_directories=("business_partners", "customer_company_assignments", "customer_sales_area_assignments"),
        label_from="customer",
    ),
    NodeTypeSpec(
        type_name="Product",
        id_column_candidates=("product", "material"),
        source_directories=("products", "sales_order_items", "billing_document_items", "product_plants", "product_descriptions"),
        label_from="product",  # if not present, we'll fall back to the actual id column used
    ),
    NodeTypeSpec(
        type_name="Delivery",
        id_column_candidates=("deliveryDocument",),
        source_directories=("outbound_delivery_headers", "outbound_delivery_items"),
        label_from="deliveryDocument",
    ),
    NodeTypeSpec(
        type_name="Finance",
        id_column_candidates=("accountingDocument",),
        source_directories=("journal_entry_items_accounts_receivable", "payments_accounts_receivable"),
        label_from="accountingDocument",
    ),
]


def _is_nan(x: Any) -> bool:
    # pandas NA / NaN
    try:
        return pd.isna(x)
    except Exception:
        return x is None


def _to_str_id(x: Any) -> Optional[str]:
    if _is_nan(x):
        return None
    # Keep original numeric-like IDs stable as strings.
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        # Avoid "1.0" artifacts when the ID is an integer stored as float.
        if float(x).is_integer():
            return str(int(x))
    return str(x)


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".jsonl":
        return pd.read_json(path, lines=True)
    raise ValueError(f"Unsupported file extension: {path.suffix}")


def _iter_data_files(base_dir: Path) -> List[Path]:
    # Requirement says CSV, but this workspace uses JSONL. We support both so the script works.
    csv_files = sorted(base_dir.glob("**/*.csv"))
    jsonl_files = sorted(base_dir.glob("**/*.jsonl"))
    return [*csv_files, *jsonl_files]


def _table_source_directory(file_path: Path) -> str:
    # e.g. `.../sales_order_headers/part-....jsonl` -> `sales_order_headers`
    return file_path.parent.name


def _node_type_for_directory(dir_name: str) -> Optional[str]:
    for spec in SUPPORTED_NODE_TYPES:
        if dir_name in spec.source_directories:
            return spec.type_name
    return None


def _find_id_column(df: pd.DataFrame, spec: NodeTypeSpec) -> Optional[str]:
    for col in spec.id_column_candidates:
        if col in df.columns:
            return col
    return None


def _node_id(type_name: str, raw_id: str) -> str:
    # Ensure node ids are unique across types.
    return f"{type_name}:{raw_id}"


def _example_value(v: Any) -> Any:
    """
    Convert potentially unhashable values (dict/list) into stable strings so
    they can be stored in `set()` for schema example collection.
    """
    try:
        hash(v)
        return v
    except TypeError:
        try:
            return json.dumps(v, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(v)


def _add_relationship(
    g: nx.DiGraph,
    source: str,
    target: str,
    relationship: str,
) -> None:
    if source == target:
        return
    if g.has_edge(source, target):
        rel = g.edges[source, target].get("relationship")
        if rel is None:
            g.edges[source, target]["relationship"] = relationship
            return
        if isinstance(rel, list):
            if relationship not in rel:
                rel.append(relationship)
        elif isinstance(rel, str):
            if rel != relationship:
                g.edges[source, target]["relationship"] = [rel, relationship]
        else:
            # Fallback: overwrite with a readable composite.
            g.edges[source, target]["relationship"] = f"{rel};{relationship}"
        return

    g.add_edge(source, target, relationship=relationship)


def build_graph_and_exports(output_dir: Path) -> None:
    if not DATA_DIR.exists():
        raise FileNotFoundError(
            f"Could not locate dataset directory. Looked for {REQUESTED_DATA_DIR} and {FALLBACK_DATA_DIR}."
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    files = _iter_data_files(DATA_DIR)
    if not files:
        raise FileNotFoundError(f"No `.csv` or `.jsonl` files found under {DATA_DIR}")

    print(f"Using dataset directory: {DATA_DIR}")
    if not REQUESTED_DATA_DIR.exists():
        print(f"Note: `{REQUESTED_DATA_DIR}` not found; using `{FALLBACK_DATA_DIR}` instead.")

    # Build nodes first, so edges can check existence.
    node_by_id: Dict[str, Dict[str, Any]] = {}
    node_fields: Dict[str, Set[str]] = {spec.type_name: set() for spec in SUPPORTED_NODE_TYPES}
    node_examples: Dict[str, Dict[str, Set[Any]]] = {spec.type_name: {} for spec in SUPPORTED_NODE_TYPES}

    # Collect known IDs by type for edge inference.
    known_node_ids: Dict[str, Set[str]] = {spec.type_name: set() for spec in SUPPORTED_NODE_TYPES}

    # Store which tables belong to which node types.
    relevant_frames: List[Tuple[str, Path, pd.DataFrame]] = []

    # 1) Read all files and print columns.
    for fp in files:
        try:
            df = _read_table(fp)
        except Exception as e:
            print(f"Skipping unreadable file {fp}: {e}")
            continue

        rel = fp.relative_to(REPO_ROOT) if fp.is_relative_to(REPO_ROOT) else fp
        print(f"\n=== {rel} ===")
        print("Columns:", list(df.columns))

        dir_name = _table_source_directory(fp)
        node_type = _node_type_for_directory(dir_name)
        if node_type is not None:
            relevant_frames.append((node_type, fp, df))

    # 2) Create nodes for the 6 requested types.
    for node_type, fp, df in relevant_frames:
        # Find the spec by type.
        spec = next(s for s in SUPPORTED_NODE_TYPES if s.type_name == node_type)
        id_col = _find_id_column(df, spec)
        if not id_col:
            print(f"WARNING: Could not find id column for type={node_type} in file={fp}. Skipping node creation for this file.")
            continue

        label_col = spec.label_from if spec.label_from in df.columns else id_col

        for _, row in df.iterrows():
            raw_id = _to_str_id(row.get(id_col))
            if not raw_id:
                continue

            nid = _node_id(node_type, raw_id)
            label_val = _to_str_id(row.get(label_col)) or raw_id

            # Metadata: include all columns except the id column(s) we used.
            metadata = {col: row[col] for col in df.columns if col not in spec.id_column_candidates and col not in (spec.label_from,)}

            # Ensure json-serializability: convert pandas scalar/NA.
            clean_metadata: Dict[str, Any] = {}
            for k, v in metadata.items():
                if _is_nan(v):
                    continue
                # Convert numpy scalar -> python scalar
                if hasattr(v, "item"):
                    try:
                        v = v.item()
                    except Exception:
                        pass
                clean_metadata[k] = v

            if nid not in node_by_id:
                node_by_id[nid] = {
                    "id": nid,
                    "label": label_val,
                    "type": node_type,
                    "cluster_id": None,
                    "metadata": clean_metadata,
                }
            else:
                # Merge metadata (preferring existing keys; fill missing ones).
                node_by_id[nid]["metadata"].update({k: v for k, v in clean_metadata.items() if k not in node_by_id[nid]["metadata"]})

            known_node_ids[node_type].add(nid)

            # Schema summary collection.
            node_fields[node_type].update(clean_metadata.keys())
            ex_map = node_examples[node_type]
            for field, value in clean_metadata.items():
                if field not in ex_map:
                    ex_map[field] = set()
                # cap examples per field
                if len(ex_map[field]) < 3:
                    ex_map[field].add(_example_value(value))

    print("\nNode creation complete.")
    for spec in SUPPORTED_NODE_TYPES:
        print(f"  {spec.type_name}: {len([nid for nid in node_by_id if nid.startswith(spec.type_name + ':')])} nodes")

    # 3) Build edges (second pass over relevant tables).
    g = nx.DiGraph()

    # Add nodes to the DiGraph.
    for nid, attrs in node_by_id.items():
        g.add_node(nid, type=attrs["type"], label=attrs["label"], cluster_id=None, metadata=attrs["metadata"])

    # Helper to map known foreign keys to target types.
    # This is tuned to the observed SAP-ish column names in this dataset.
    FOREIGN_KEY_TO_TYPE: Dict[str, str] = {
        "soldToParty": "Customer",
        "customer": "Customer",
        "material": "Product",
        "product": "Product",
        "referenceSdDocument": "SalesOrder",
        "referenceSdDocumentItem": "SalesOrder",
        "salesDocument": "SalesOrder",
        "deliveryDocument": "Delivery",
        "billingDocument": "BillingDocument",
        "invoiceReference": "BillingDocument",
        "referenceDocument": "BillingDocument",  # seen in journal entry items
        "accountingDocument": "Finance",
        "clearingAccountingDocument": "Finance",
        "clearingDocFiscalYear": "Finance",
        "cancelledBillingDocument": "BillingDocument",
    }

    # Which source type a table contributes edges from.
    # (We use the node_type_for_directory mapping, so it matches the node creation pass.)
    for node_type, fp, df in relevant_frames:
        source_spec = next(s for s in SUPPORTED_NODE_TYPES if s.type_name == node_type)
        source_id_col = _find_id_column(df, source_spec)
        if not source_id_col:
            continue

        for _, row in df.iterrows():
            raw_source_id = _to_str_id(row.get(source_id_col))
            if not raw_source_id:
                continue
            source_nid = _node_id(node_type, raw_source_id)
            if source_nid not in g:
                continue

            # Add edges for each foreign-key-looking column.
            for col in df.columns:
                if col not in FOREIGN_KEY_TO_TYPE:
                    continue
                target_type = FOREIGN_KEY_TO_TYPE[col]
                raw_target_id = _to_str_id(row.get(col))
                if not raw_target_id:
                    continue
                target_nid = _node_id(target_type, raw_target_id)
                if target_nid not in g:
                    continue

                _add_relationship(g, source_nid, target_nid, relationship=col)

    print("Edge creation complete.")

    # 4) Run Louvain community detection and store cluster_id on each node.
    if g.number_of_nodes() == 0:
        communities: List[Set[str]] = []
    else:
        # Louvain is defined for undirected graphs; use an undirected view.
        undirected = g.to_undirected()

        if community_louvain is not None:
            # python-louvain returns a mapping node->cluster_id
            partition = community_louvain.best_partition(undirected, weight=None)
            communities = []
            for nid, cid in partition.items():
                while len(communities) <= int(cid):
                    communities.append(set())
                communities[int(cid)].add(nid)
        else:
            # NetworkX includes a built-in Louvain implementation.
            communities = list(nx.community.louvain_communities(undirected, weight=None, seed=42))

    # Store `cluster_id` on each node.
    for cid, nodes_set in enumerate(communities):
        for nid in nodes_set:
            if nid in g.nodes:
                g.nodes[nid]["cluster_id"] = int(cid)

    # 5) Export `graph_data.json` and `schema_summary.json`.
    nodes_export: List[Dict[str, Any]] = []
    for nid, attrs in g.nodes(data=True):
        meta = attrs.get("metadata") or {}
        node_obj: Dict[str, Any] = {
            "id": nid,
            "label": attrs.get("label"),
            "type": attrs.get("type"),
            "cluster_id": attrs.get("cluster_id"),
        }
        node_obj.update(meta)
        nodes_export.append(node_obj)

    edges_export: List[Dict[str, Any]] = []
    for u, v, attrs in g.edges(data=True):
        rel = attrs.get("relationship", "")
        if isinstance(rel, list):
            rel = ";".join(str(x) for x in rel)
        edges_export.append({"source": u, "target": v, "relationship": rel})

    graph_data = {"nodes": nodes_export, "edges": edges_export}

    graph_path = output_dir / "graph_data.json"
    schema_path = output_dir / "schema_summary.json"

    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(graph_data, f, ensure_ascii=False, indent=2)

    schema_out = {"node_types": []}
    for spec in SUPPORTED_NODE_TYPES:
        type_name = spec.type_name
        examples_out: Dict[str, List[Any]] = {}
        for field, values_set in node_examples[type_name].items():
            # Convert set -> stable list for JSON
            examples_out[field] = sorted(list(values_set), key=lambda x: str(x))[:3]

        schema_out["node_types"].append(
            {
                "type": type_name,
                "fields": sorted(list(node_fields[type_name])),
                "examples": examples_out,
            }
        )

    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(schema_out, f, ensure_ascii=False, indent=2)

    # 6) Print a summary: how many nodes and edges per type were created.
    node_counts: Dict[str, int] = {spec.type_name: 0 for spec in SUPPORTED_NODE_TYPES}
    for nid in g.nodes:
        t = g.nodes[nid].get("type")
        if t in node_counts:
            node_counts[t] += 1

    edge_counts_by_source: Dict[str, int] = {spec.type_name: 0 for spec in SUPPORTED_NODE_TYPES}
    for u, _, _ in g.edges(data=True):
        t = g.nodes[u].get("type")
        if t in edge_counts_by_source:
            edge_counts_by_source[t] += 1

    print("\n===== Summary =====")
    for spec in SUPPORTED_NODE_TYPES:
        print(
            f"{spec.type_name}: nodes={node_counts[spec.type_name]}, edges_from_type={edge_counts_by_source[spec.type_name]}"
        )
    print(f"\nWrote:\n- {graph_path}\n- {schema_path}")


if __name__ == "__main__":
    # Run from `/backend` as requested: `python3 graph_builder.py`
    build_graph_and_exports(output_dir=Path(__file__).resolve().parent)

