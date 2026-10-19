from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv
from .source_registry import OFFICIAL_SITES

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    workflow_api_key: str = field(default_factory=lambda: os.getenv('WORKFLOW_API_KEY',''),repr=False)
    workflow_client_id: str = field(default_factory=lambda: os.getenv('WORKFLOW_CLIENT_ID','workflow'))
    mode: str = field(default_factory=lambda: os.getenv("PROVIDER_MODE", "demo"))
    deepseek_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_base: str = field(
        default_factory=lambda: os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).rstrip("/")
    )
    chat_model: str = field(
        default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    )
    disable_thinking: bool = field(
        default_factory=lambda: os.getenv("DEEPSEEK_DISABLE_THINKING", "true").lower()
        == "true"
    )
    embedding_key: str = field(
        default_factory=lambda: os.getenv("SILICONFLOW_API_KEY", "")
    )
    embedding_base: str = field(
        default_factory=lambda: os.getenv(
            "SILICONFLOW_BASE_URL", "https://api.siliconflow.com/v1"
        ).rstrip("/")
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv(
            "EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"
        )
    )
    rag_backend: str = field(default_factory=lambda: os.getenv("RAG_BACKEND", "json"))
    dimensions: int = field(
        default_factory=lambda: int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
    )
    timeout: float = field(
        default_factory=lambda: float(os.getenv("API_TIMEOUT_SECONDS", "30"))
    )
    history_max_turns: int = field(
        default_factory=lambda: int(os.getenv("HISTORY_MAX_TURNS", "6"))
    )

    history_max_chars: int = field(
        default_factory=lambda: int(os.getenv("HISTORY_MAX_CHARS", "6000"))
    )
    rag_rerank_enabled: bool = field(default_factory=lambda: os.getenv('RAG_RERANK_ENABLED','true').lower()=='true')
    public_rag_lexical_weight: float = field(default_factory=lambda: float(os.getenv('PUBLIC_RAG_LEXICAL_WEIGHT','1')))
    public_citations_enabled: bool = field(default_factory=lambda: os.getenv('PUBLIC_CITATIONS_ENABLED','true').lower()=='true')
    retrieval_min_score: float = field(
        default_factory=lambda: float(os.getenv("RETRIEVAL_MIN_SCORE", "0.2"))
    )
    dev_token: str = field(default_factory=lambda: os.getenv("DEV_TOKEN", ""))
    knowledge_path: Path = ROOT / "data" / "knowledge.json"
    index_path: Path = ROOT / ".cache" / "vectors.json"
    log_path: Path = ROOT / ".cache" / "events.jsonl"
    cors: list[str] = field(
        default_factory=lambda: os.getenv(
            "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",")
    )
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "sqlite:///" + (ROOT / ".cache" / "carebrief.db").as_posix()
        )
    )
    cookie_secure: bool = field(
        default_factory=lambda: os.getenv("COOKIE_SECURE", "false").lower() == "true"
    )
    ocr_enabled: bool = field(
        default_factory=lambda: os.getenv("OCR_ENABLED", "false").lower() == "true"
    )
    recognition_provider: str = field(default_factory=lambda: os.getenv('RECOGNITION_PROVIDER','disabled'))
    asr_enabled: bool = field(default_factory=lambda: os.getenv('ASR_ENABLED','false').lower()=='true')
    tencent_secret_id: str = field(default_factory=lambda: os.getenv('TENCENTCLOUD_SECRET_ID',''),repr=False)
    tencent_secret_key: str = field(default_factory=lambda: os.getenv('TENCENTCLOUD_SECRET_KEY',''),repr=False)
    tencent_region: str = field(default_factory=lambda: os.getenv('TENCENTCLOUD_REGION','ap-guangzhou'))
    tencent_search_enabled: bool = field(default_factory=lambda: os.getenv('TENCENT_SEARCH_ENABLED','false').lower()=='true')
    tencent_asr_engine: str = field(default_factory=lambda: os.getenv('TENCENT_ASR_ENGINE','16k_zh'))
    external_enabled: bool = field(
        default_factory=lambda: os.getenv("EXTERNAL_SEARCH_ENABLED", "false").lower()
        == "true"
    )
    hospital_search_max_rounds: int = field(default_factory=lambda: int(os.getenv('HOSPITAL_SEARCH_MAX_ROUNDS','3')))
    hospital_page_limit: int = field(default_factory=lambda: int(os.getenv('HOSPITAL_PAGE_LIMIT','8')))
    hospital_service_page_limit: int = field(default_factory=lambda: int(os.getenv('HOSPITAL_SERVICE_PAGE_LIMIT','16')))
    hospital_browser_limit: int = field(default_factory=lambda: int(os.getenv('HOSPITAL_BROWSER_LIMIT','2')))
    hospital_lookup_timeout: float = field(default_factory=lambda: float(os.getenv('HOSPITAL_LOOKUP_TIMEOUT','75')))
    bocha_key: str = field(default_factory=lambda: os.getenv("BOCHA_API_KEY", ""))
    bocha_url: str = field(
        default_factory=lambda: os.getenv(
            "BOCHA_SEARCH_URL", "https://api.bochaai.com/v1/web-search"
        )
    )
    external_domains: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            host.strip().lower()
            for host in os.getenv(
                "EXTERNAL_DOMAINS",
                ",".join(OFFICIAL_SITES),
            ).split(",")
            if host.strip()
        )
    )

    def __post_init__(self):
        if not 1 <= self.public_rag_lexical_weight <= 8:
            raise ValueError('PUBLIC_RAG_LEXICAL_WEIGHT must be between 1 and 8')
        if not (1 <= self.hospital_search_max_rounds <= 3 and 1 <= self.hospital_page_limit <= 12 and
                1 <= self.hospital_service_page_limit <= 24 and
                0 <= self.hospital_browser_limit <= 3 and 1 <= self.hospital_lookup_timeout <= 120):
            raise ValueError('Hospital lookup budgets exceed supported bounds')
        if self.recognition_provider not in {'disabled','tencent'}:
            raise ValueError('RECOGNITION_PROVIDER must be disabled or tencent')
        if self.mode not in {"demo", "live"}:
            raise ValueError("PROVIDER_MODE must be demo or live")

        if self.history_max_turns < 1 or self.history_max_chars < 1:
            raise ValueError("History limits must be positive")
        if not 0 <= self.retrieval_min_score <= 1:
            raise ValueError("RETRIEVAL_MIN_SCORE must be between 0 and 1")
        if self.rag_backend not in {"json", "postgres"}:
            raise ValueError("RAG_BACKEND must be json or postgres")
