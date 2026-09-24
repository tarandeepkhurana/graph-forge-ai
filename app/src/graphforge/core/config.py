"""Settings and hard limits.

Every limit lives here, not scattered through the code, so there is one place to
audit "what can a user make this app do". Values come from the environment with
safe defaults; nothing secret has a default.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Limits(BaseSettings):
    """Ingestion ceilings (feature 10).

    Chosen so one document cannot exhaust memory or the extraction budget.
    Measured basis: the extraction model runs ~2 s per 1k-token chunk on CPU and
    holds ~2.7 GB resident, so a 300-page PDF is roughly 20 minutes of work --
    which is why ingestion is a background job, never a request.
    """

    max_upload_bytes: int = 25 * 1024 * 1024      # 25 MB
    max_pdf_pages: int = 300
    max_extracted_chars: int = 1_500_000
    max_documents_per_workspace: int = 50
    max_concurrent_jobs_per_user: int = 2

    # GitHub repo ingestion
    max_repo_files: int = 2_000
    max_repo_file_bytes: int = 1024 * 1024        # 1 MB, skips minified bundles

    # Chunking
    chunk_tokens: int = 1_000
    chunk_overlap_tokens: int = 150

    # Extraction quality gate. Below this, an edge is stored but hidden by
    # default and offered as a suggestion. Basis: on a real research paper,
    # triples >=0.85 were mostly correct and <=0.60 were mostly noise.
    min_confidence: float = 0.70

    # Cost control on the LLM endpoints -- a security property, not a billing one.
    max_ai_calls_per_user_per_day: int = 200

    # --- entity resolution -------------------------------------------------
    # Cosine at or above `embed_merge` is trusted outright; the band down to
    # `embed_ask` is handed to a language model. Measured basis: on a real
    # workspace, `GraphForge` scored 0.65 against `Graph Databases` -- a product
    # and a category -- so anything below the merge line needs judgement, not a
    # threshold.
    resolution_use_embeddings: bool = True
    resolution_use_llm: bool = True
    resolution_embed_merge: float = 0.90
    # 0.55, not 0.70: measured, "PC" scores only 0.59 against "personal
    # computer" -- the textbook duplicate. A higher floor never even asks about
    # it. Sending more pairs is cheap (one request settles 40) and the model
    # rejects the near misses correctly: "IPv4"/"IPv6" lands at 0.73 and is
    # answered "different".
    resolution_embed_ask: float = 0.55
    # A pathological workspace must not turn into a large bill.
    resolution_max_llm_pairs: int = 200


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "GraphForge"
    debug: bool = False
    base_url: str = "http://localhost:8000"

    # --- secrets: no defaults on purpose ---
    secret_key: str = Field(..., min_length=32)   # session signing
    database_url: str = Field(...)                # Supabase Postgres
    neo4j_uri: str = Field(...)
    neo4j_user: str = Field(...)
    neo4j_password: str = Field(...)
    openai_api_key: str = Field(default="")       # optional: extraction/chat disabled if empty

    # --- models ---
    # Extraction builds the graph: measured 18/22 concepts against the local
    # encoder's 9/22 on a real document, so it gets the stronger model.
    extraction_model: str = "openai"
    extraction_openai_model: str = "gpt-5-mini"
    # Chat, quiz and resolution adjudication: high volume, low stakes per call.
    openai_model: str = "gpt-5-nano"

    # --- sessions ---
    session_cookie: str = "gf_session"
    session_ttl_hours: int = 24 * 7
    # Set False only for local http development; cookies must be Secure in production.
    cookie_secure: bool = True

    storage_dir: str = "./var/uploads"

    limits: Limits = Limits()


@lru_cache
def get_settings() -> Settings:
    """Full configuration. Raises if a required secret is missing."""
    return Settings()


@lru_cache
def get_limits() -> Limits:
    """Just the limits.

    Deliberately reachable without the secrets: limits are not sensitive, and
    tying them to `Settings` would mean the upload validators could not be
    tested or reasoned about without a live database and an API key.
    """
    return Limits()
