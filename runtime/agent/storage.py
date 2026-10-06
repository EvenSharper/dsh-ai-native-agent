"""Durable run evidence and shared, reviewed knowledge. No third-party services."""
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schema import serializable

SECTIONS = ("domain_concepts", "invariants", "truth_sources", "state_transitions",
            "external_contracts", "known_failure_modes", "forbidden_simplifications",
            "decisions", "unknowns", "knowledge")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_record() -> dict:
    return {"schema_version": 1, **{key: [] for key in SECTIONS}}


def read_record(path: Path) -> dict:
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(record, dict) or record.get("schema_version", 1) != 1:
        raise ValueError("Unsupported System Record schema")
    for section in SECTIONS:
        if not isinstance(record.get(section), list):
            raise ValueError(f"System Record {section} must be a list")
    record["schema_version"] = 1
    return record


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(serializable(value), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def record_lock(path: Path):
    """One run per shared record; a crashed-run lock is deliberately not auto-stolen."""
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError(f"System Record is locked by another run: {lock}") from exc
    try:
        with handle:
            handle.write(json.dumps({"pid": os.getpid(), "created_at": now()}))
        yield
    finally:
        lock.unlink(missing_ok=True)


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **payload) -> None:
        record = {"time": now(), "event": event, **serializable(payload)}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def save_claims(record: dict, claims, path: Path, *, run_id: str, patch: Path,
                base_commit: str) -> list[dict]:
    """Store candidate-scoped evidence; a verified patch is not yet source-repo truth."""
    patch_digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    existing = {entry.get("id"): entry for entry in record["knowledge"] if isinstance(entry, dict)}
    claimed_supersedes = set()
    for claim in claims:
        if claim.supersedes:
            if claim.supersedes not in existing or claim.supersedes in claimed_supersedes:
                raise ValueError("Superseded knowledge must name one existing unique entry")
            claimed_supersedes.add(claim.supersedes)
    saved = []
    for claim in claims:
        entry = serializable(claim)
        entry.update(id=uuid4().hex, last_verified=now(), superseded_by=None)
        entry["metadata"].update(run_id=run_id, base_commit=base_commit,
                                  patch_sha256=patch_digest,
                                  applicability="verified_candidate_only", applied_to_source=False)
        if claim.supersedes:
            existing[claim.supersedes]["status"] = "superseded"
            existing[claim.supersedes]["superseded_by"] = entry["id"]
        record["knowledge"].append(entry)
        saved.append(entry)
    write_json(path, record)
    return saved
