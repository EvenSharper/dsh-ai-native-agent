"""Small, validated contracts shared by the three roles and local adapters."""
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    BLOCK = "BLOCK"


class Risk(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class FileChange:
    path: str
    content: str | None


@dataclass
class Proposal:
    intent: str
    observed_facts: list[str] = field(default_factory=list)
    proposed_change: list[str] = field(default_factory=list)
    semantic_delta: list[str] = field(default_factory=list)
    new_concepts: list[str] = field(default_factory=list)
    risks_unknowns: list[str] = field(default_factory=list)
    verification_plan: list[str] = field(default_factory=list)
    reversibility_notes: list[str] = field(default_factory=list)
    changes: list[FileChange] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)


@dataclass
class Review:
    verdict: Verdict
    reasons: list[str]


@dataclass
class GuardianResult:
    risk: Risk
    reasons: list[str]
    human_review_required: bool = False


@dataclass
class VerificationCommand:
    name: str
    argv: list[str]
    timeout_seconds: float = 60


@dataclass
class CommandResult:
    name: str
    argv: list[str]
    returncode: int | None
    output: str
    timed_out: bool = False
    evidence_id: str = ""


@dataclass
class VerificationResult:
    ok: bool
    commands: list[str]
    output: str
    results: list[CommandResult] = field(default_factory=list)


@dataclass
class KnowledgeClaim:
    statement: str
    source: str
    confidence: str
    scope: str
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    kind: str = "fact"
    validity: str = ""
    supersedes: str | None = None


def serializable(value: Any) -> Any:
    if is_dataclass(value):
        return serializable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    return value


def _object(value: Any, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    if set(value) - allowed or required - set(value):
        raise ValueError(f"Invalid fields: extra={set(value)-allowed}, missing={required-set(value)}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{name} must be a list of nonempty strings")
    return value


def parse_proposal(value: Any) -> Proposal:
    value = _object(value, set(Proposal.__dataclass_fields__), {"intent", "changes", "side_effects"})
    args = {"intent": _text(value["intent"], "intent")}
    for name in set(Proposal.__dataclass_fields__) - {"intent", "changes"}:
        args[name] = _strings(value.get(name, []), name)
    changes = value["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        raise ValueError("Proposal must contain 1 to 50 file changes")
    parsed = []
    for change in changes:
        _object(change, {"path", "content"}, {"path", "content"})
        path = _text(change["path"], "path")
        content = change["content"]
        if content is not None and (not isinstance(content, str) or len(content.encode("utf-8")) > 500_000):
            raise ValueError("File content must be UTF-8 text <= 500KB, or null to delete")
        parsed.append(FileChange(path, content))
    args["changes"] = parsed
    if not args["side_effects"]:
        raise ValueError("Explicit side_effects are required, use ['local_files'] for local edits")
    return Proposal(**args)


def parse_review(value: Any) -> Review:
    value = _object(value, {"verdict", "reasons"}, {"verdict", "reasons"})
    reasons = _strings(value["reasons"], "reasons")
    if not reasons:
        raise ValueError("Review must include evidence-based reasons")
    return Review(Verdict(value["verdict"]), reasons)


def parse_guardian(value: Any) -> GuardianResult:
    value = _object(value, {"risk", "reasons", "human_review_required"}, {"risk", "reasons", "human_review_required"})
    if type(value["human_review_required"]) is not bool:
        raise ValueError("human_review_required must be boolean")
    reasons = _strings(value["reasons"], "reasons")
    if not reasons:
        raise ValueError("Guardian must give reversibility evidence")
    return GuardianResult(Risk(value["risk"]), reasons, value["human_review_required"])


def parse_claim(value: Any) -> KnowledgeClaim:
    required = {"statement", "source", "confidence", "scope", "evidence_ids", "kind", "validity"}
    value = _object(value, set(KnowledgeClaim.__dataclass_fields__), required)
    for name in {"statement", "source", "scope", "validity"}:
        _text(value[name], name)
    if value["confidence"] not in {"low", "medium", "high", "very_high"}:
        raise ValueError("Invalid confidence")
    if value["kind"] not in {"fact", "inference", "unknown"}:
        raise ValueError("Invalid knowledge kind")
    if value.get("status", "active") != "active":
        raise ValueError("New claims must be active")
    if not _strings(value["evidence_ids"], "evidence_ids"):
        raise ValueError("Knowledge must cite verification evidence")
    if not isinstance(value.get("metadata", {}), dict):
        raise ValueError("metadata must be an object")
    if value.get("supersedes") is not None:
        _text(value["supersedes"], "supersedes")
    return KnowledgeClaim(**value)
