from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "knowledge_chunks_hybrid_v1"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_document_topic: str = "knowledge.documents"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "knowledge"
    s3_region: str = "us-east-1"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768
    chat_model: str = "qwen3:8b"
    model_timeout_seconds: float = Field(default=60, gt=0, le=120)
    request_timeout_seconds: float = Field(default=90, gt=0, le=180)
    dependency_timeout_seconds: float = Field(default=5, gt=0, le=30)
    employee_concurrency: int = Field(default=48, ge=1)
    service_concurrency: int = Field(default=16, ge=1)
    tenant_employee_concurrency: int = Field(default=32, ge=1)
    tenant_service_concurrency: int = Field(default=8, ge=1)
    employee_requests_per_minute: int = Field(default=30, ge=1)
    service_requests_per_minute: int = Field(default=60, ge=1)
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    # Browser authentication is opt-in locally and enabled with AUTH_MODE=oidc
    # in deployed environments. API callers in jwt mode retain the bootstrap
    # token path used by the existing local tests.
    auth_mode: str = "jwt"
    identity_issuer: str = "https://idp.snnc.cc/realms/openidentity"
    identity_client_id: str = "sn-knowledge"
    identity_client_secret: str | None = None
    identity_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    identity_post_logout_redirect_uri: str | None = None
    identity_scope: str = "openid profile email"
    identity_default_tenant_id: str | None = None
    identity_session_secret: str | None = None
    identity_session_cookie: str = "sn_knowledge_session"
    identity_cookie_secure: bool = False
    identity_session_max_seconds: int = 28800
    identity_http_timeout_seconds: float = 8.0
    local_admin_username: str | None = None
    local_admin_password: str | None = None
    brand_name: str = Field(default="Knowledge Workspace", min_length=1, max_length=80)
    brand_mark: str = Field(default="K", min_length=1, max_length=4)
    brand_tagline: str = Field(default="企业知识助手", max_length=120)
    brand_footer: str = Field(default="Evidence first", max_length=120)
    brand_primary_color: str = Field(default="#4f5bd5", pattern=r"^#[0-9a-fA-F]{6}$")
    brand_logo_url: str = Field(default="", max_length=500)

    @field_validator("brand_name", "brand_mark")
    @classmethod
    def validate_brand_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("brand name and mark cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_oidc_runtime(self):
        if self.brand_logo_url and not self.brand_logo_url.startswith(("/", "https://", "http://")):
            raise ValueError("BRAND_LOGO_URL must be an absolute path or an HTTP(S) URL")
        auth_mode = self.auth_mode.lower()
        if auth_mode not in {"dev", "jwt", "oidc"}:
            raise ValueError("AUTH_MODE must be dev, jwt or oidc")
        if auth_mode != "dev" and len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters outside development")
        if auth_mode == "oidc":
            required = (self.identity_issuer, self.identity_client_id, self.identity_default_tenant_id,
                        self.identity_session_secret, self.identity_redirect_uri,
                        self.identity_post_logout_redirect_uri)
            if any(not value or "REPLACE_WITH" in str(value) for value in required):
                raise ValueError("OIDC production settings require issuer, client, tenant, callback and session settings")
            if len(self.identity_session_secret) < 32 or not self.identity_cookie_secure:
                raise ValueError("OIDC production requires a 32-character session secret and secure cookies")
        return self

settings = Settings()
