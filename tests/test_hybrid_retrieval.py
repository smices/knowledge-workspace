from types import SimpleNamespace

import httpx
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from app import vector
from app.vector import sparse_vector


def test_sparse_vector_is_stable_and_qdrant_can_fuse_dense_and_lexical_results():
    assert sparse_vector("预算审批流程") == sparse_vector("预算审批流程")
    client = QdrantClient(":memory:")
    client.create_collection(
        "hybrid",
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
        sparse_vectors_config={"lexical": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    client.upsert("hybrid", [models.PointStruct(
        id=1, vector={"": [1.0, 0.0], "lexical": sparse_vector("预算审批流程")},
        payload={"tenant_id": "tenant-a", "allowed_roles": ["finance"]},
    )])
    filters = models.Filter(must=[
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value="tenant-a")),
        models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=["finance"])),
    ])
    result = client.query_points("hybrid", prefetch=[
        models.Prefetch(query=[1.0, 0.0], filter=filters, limit=5),
        models.Prefetch(query=sparse_vector("预算审批"), using="lexical", filter=filters, limit=5),
    ], query=models.FusionQuery(fusion=models.Fusion.RRF), limit=3)
    assert [point.id for point in result.points] == [1]


def test_collection_creation_tolerates_api_worker_startup_race(monkeypatch):
    class RacingClient:
        def get_collections(self):
            return SimpleNamespace(collections=[])

        def create_collection(self, *args, **kwargs):
            raise UnexpectedResponse(409, "Conflict", b"collection already exists", httpx.Headers())

        def create_payload_index(self, *args, **kwargs):
            return None

    monkeypatch.setattr(vector, "client", RacingClient())
    vector.ensure_collection()
