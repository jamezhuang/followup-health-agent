from typing import Literal, Annotated
from pydantic import BaseModel, Field, ConfigDict, field_validator, AliasChoices

Intent = Literal[
    "symptom",
    "department",
    "knowledge",
    "update",
    "medication",
    "report",
    "other",
    "greeting",
    "thanks",
    "help",
    "clarify",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observation(StrictModel):
    name: Literal[
        "age",
        "symptom",
        "duration",
        "associated",
        "history",
    ]

    value: str = Field(min_length=1, max_length=400)
    evidence: str = Field(min_length=1, max_length=400)

    operation: Literal["set", "append"] = "set"


class RiskMention(StrictModel):
    symptom: str = Field(min_length=1, max_length=40)
    evidence: str = Field(min_length=1, max_length=300)
    status: Literal["present", "absent", "uncertain", "hypothetical", "historical"]


class Classification(StrictModel):
    intent: Intent
    secondary_intents: list[Intent] = Field(default_factory=list, max_length=3)
    observations: list[Observation] = Field(default_factory=list, max_length=8)
    risks: list[RiskMention] = Field(default_factory=list, max_length=8)
    supported: bool
    clarification: str = Field(default="", max_length=300)


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    evidence_id: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=600)


class Answer(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)

    answer_basis: (
        Literal[
            "general",
            "retrieved",
            "mixed",
            "insufficient",
        ]
        | None
    ) = None

    department: str = Field(default="", max_length=80)

    claims: list[Claim] = Field(
        default_factory=list,
        max_length=12,
    )

    follow_up_questions: list[Annotated[str, Field(min_length=1, max_length=200)]] = (
        Field(default_factory=list, max_length=2)
    )

    missing_information: list[Annotated[str, Field(min_length=1, max_length=80)]] = (
        Field(default_factory=list, max_length=2)
    )


class Verification(StrictModel):
    supported: bool
    reason: str = Field(max_length=400)


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=400)
    top_k: int = Field(default=3, ge=1, le=3)


class Source(StrictModel):
    id: str
    title: str
    institution: str
    url: str
    published_at: str | None
    collected_at: str
    text: str
    allowed_departments: list[str]
    kind: Literal['official_summary', 'external_excerpt', 'private_document']
    score: float = 0.0


class MessageRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def no_blank(cls, value):
        if not value.strip():
            raise ValueError("请输入内容")
        return value.strip()


class TurnResult(StrictModel):
    run_id: str
    provider_mode: Literal["demo", "live"]
    intent: Intent

    answer_basis: Literal[
        "general",
        "retrieved",
        "mixed",
        "insufficient",
    ] = "retrieved"

    summary: str
    department: str
    claims: list[Claim]
    missing_information: list[str]
    follow_up_questions: list[str]
    sources: list[Source]

    risk_notice: str | None = None
    risk_inputs: list[str] = Field(default_factory=list)

    limitation: str = (
        "健康信息辅助，不提供确诊或处方；" "回答依据以本轮展示的来源为准。"
    )

    retrieval_notice: str = ""


class Message(StrictModel):
    role: Literal["user", "assistant"]
    content: str


class SessionView(StrictModel):
    id: str
    messages: list[Message]
    facts: dict[str, str]
    last_result: TurnResult | None
    last_turn: TurnResult | None = None
    memory_context: dict[str, str] = Field(default_factory=dict)
    candidates: dict[str, str] = Field(default_factory=dict)


class AuthRequest(StrictModel):
    email: str = Field(min_length=6, max_length=20, validation_alias=AliasChoices("username", "email"))
    password: str = Field(min_length=6, max_length=20)

    @field_validator("email", "password")
    @classmethod
    def valid_credential(cls, value):
        import re

        if not re.fullmatch(r"[A-Za-z0-9]{6,20}", value):
            raise ValueError("账号和密码须为 6–20 位数字或字母，可单独使用或混合")
        return value

    @field_validator("email")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()


class LoginRequest(StrictModel):
    # Existing email accounts and password hashes remain usable after migration.
    email: str = Field(min_length=1, max_length=160, validation_alias=AliasChoices("username", "email"))
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_username(cls, value):
        import re
        value = value.strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9]{6,20}", value) and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("账号须为 6–20 位数字或字母")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value, info):
        import re
        if "@" not in info.data.get("email", "") and not re.fullmatch(r"[A-Za-z0-9]{6,20}", value):
            raise ValueError("密码须为 6–20 位数字或字母")
        return value


class ProfileRequest(StrictModel):
    age: str = Field(default="", max_length=40)
    history: str = Field(default="", max_length=400)
    allergies: str = Field(default="", max_length=400)


class MemoryRequest(StrictModel):
    field: Literal["age", "history"]
    value: str = Field(min_length=1, max_length=400)


class ReportInput(StrictModel):
    name: str = Field(default="粘贴的报告", min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=18000)


class ReportRow(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=80)
    unit: str = Field(default="", max_length=40)
    reference: str = Field(default="", max_length=80)
    location: str = Field(default="用户核对", max_length=80)


class ReportConfirm(StrictModel):
    revision: int = Field(ge=1)
    confirmed_text: str = Field(min_length=1, max_length=18000)
    rows: list[ReportRow] = Field(default_factory=list, max_length=80)


class ReportView(StrictModel):
    id: str
    session_id: str
    name: str
    original_text: str
    confirmed_text: str = ""
    rows: list[ReportRow]
    confirmed: bool = False
    revision: int = 1
    created_at: str
    notice: str


class ReportSummary(StrictModel):
    summary: str = Field(min_length=1, max_length=600)
