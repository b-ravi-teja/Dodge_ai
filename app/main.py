import os
from typing import List, Optional

import networkx as nx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Optional LLM deps (used only when GEMINI_API_KEY is present)
try:
    import google.generativeai as genai
except Exception:  # pragma: no cover
    genai = None

# Community detection (python-louvain)
try:
    import community as community_louvain  # type: ignore
except Exception:  # pragma: no cover
    community_louvain = None


load_dotenv()

app = FastAPI(title="Dodge AI - FDE Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str = "ok"


class Edge(BaseModel):
    source: str
    target: str
    weight: float = 1.0


class CommunityResponse(BaseModel):
    communities: List[List[str]] = Field(
        description="List of communities; each community is a list of node ids."
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


class LouvainRequest(BaseModel):
    edges: List[Edge]
    resolution: float = 1.0


@app.post("/api/graph/louvain", response_model=CommunityResponse)
def louvain_communities(req: LouvainRequest) -> CommunityResponse:
    if community_louvain is None:
        raise HTTPException(status_code=500, detail="python-louvain not available")

    g = nx.Graph()
    for e in req.edges:
        g.add_edge(e.source, e.target, weight=float(e.weight))

    if g.number_of_nodes() == 0:
        return CommunityResponse(communities=[])

    part = community_louvain.best_partition(g, weight="weight", resolution=req.resolution)

    # Build communities in a stable order
    comm_map: dict[int, List[str]] = {}
    for node, cid in part.items():
        comm_map.setdefault(int(cid), []).append(str(node))

    communities = [sorted(nodes) for _, nodes in sorted(comm_map.items(), key=lambda x: x[0])]
    return CommunityResponse(communities=communities)


class ExplainRequest(BaseModel):
    prompt: str
    context: Optional[str] = None


class ExplainResponse(BaseModel):
    explanation: str


@app.post("/api/llm/explain", response_model=ExplainResponse)
def llm_explain(req: ExplainRequest) -> ExplainResponse:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not set. Add it to the root .env file.",
        )

    if genai is None:
        raise HTTPException(status_code=500, detail="google-generativeai not available")

    # Keep model selection configurable later; for now use a reasonable default.
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    full_prompt = req.prompt
    if req.context:
        full_prompt = f"{req.context}\n\n{req.prompt}"

    try:
        resp = model.generate_content(full_prompt)
        text = getattr(resp, "text", None) or str(resp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini request failed: {e}")

    # Normalize weird whitespace for UI
    explanation = " ".join(text.split())
    return ExplainResponse(explanation=explanation)


if __name__ == "__main__":
    # For local dev without uvicorn CLI.
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
