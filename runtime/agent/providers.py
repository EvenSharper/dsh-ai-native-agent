"""Model boundaries: an explicit offline fixture and an HTTP JSON provider.

Neither provider executes commands. Verification plans are advisory; the local
runner alone owns the configured verification commands and file operations.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import resources
from http.client import HTTPException
import ipaddress
import json
import math
import os
import ssl
from urllib import error, parse, request

from .interfaces import ModelProvider
from .schema import (
    GuardianResult, KnowledgeClaim, Proposal, Review, Verdict,
    VerificationResult, parse_claim, parse_guardian, parse_proposal,
    parse_review, serializable,
)


class ProviderError(RuntimeError):
    """A sanitized provider failure: never includes response bodies or keys."""


def _json_object(text: str) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON number")

    value = json.loads(text, object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _verified_evidence_ids(verification: VerificationResult) -> list[str]:
    """Reject inconsistent aggregate success as well as missing evidence."""
    if verification.ok is not True or not verification.results:
        return []
    evidence = []
    for result in verification.results:
        if (result.returncode != 0 or result.timed_out or
                not isinstance(result.evidence_id, str) or
                not result.evidence_id.strip()):
            return []
        evidence.append(result.evidence_id)
    if len(evidence) != len(set(evidence)):
        return []
    return evidence


def _claims_supported(claims: list[KnowledgeClaim], verification: VerificationResult) -> bool:
    evidence = set(_verified_evidence_ids(verification))
    return bool(claims and evidence) and all(
        claim.evidence_ids and set(claim.evidence_ids) <= evidence for claim in claims
    )


def _parse_claims(value: dict, verification: VerificationResult) -> list[KnowledgeClaim]:
    if set(value) != {"claims"} or not isinstance(value["claims"], list):
        raise ValueError("Expected a claims array")
    if len(value["claims"]) > 50:
        raise ValueError("Too many claims")
    claims = [parse_claim(claim) for claim in value["claims"]]
    if claims and not _claims_supported(claims, verification):
        raise ValueError("Claims must cite successful verification evidence")
    return claims


def _unsupported_knowledge() -> Review:
    return Review(Verdict.BLOCK, [
        "Knowledge requires nonempty claims citing actual successful verification evidence."
    ])


class ScriptedProvider(ModelProvider):
    """Deterministic fixture, not an intelligent model or independent reviewer.

    ``proposals`` is a required nonempty list. ``reviews`` and ``guardians``
    default to fixture ACCEPT/GREEN decisions. Each sequence repeats its last
    item after exhaustion, letting the runner enforce its own round limit.
    ``claims`` defaults to empty; ``knowledge_review`` defaults to ACCEPT.
    In claims, evidence_ids entries equal to ``$verification`` expand to the
    current successful command evidence IDs. A source equal to that placeholder
    becomes an explicit verification-evidence reference.
    """

    is_fixture = True
    name = "scripted-fixture (no model intelligence)"

    def __init__(self, scenario: dict):
        allowed = {"proposals", "reviews", "guardians", "claims", "knowledge_review"}
        if not isinstance(scenario, dict) or set(scenario) - allowed:
            raise ProviderError("Invalid fixture scenario fields")
        self._scenario = deepcopy(scenario)
        self._scenario.setdefault("reviews", [{
            "verdict": "ACCEPT", "reasons": ["Scripted fixture acceptance; no model review."]
        }])
        self._scenario.setdefault("guardians", [{
            "risk": "GREEN", "reasons": ["Scripted fixture decision; local policy still applies."],
            "human_review_required": False,
        }])
        self._scenario.setdefault("claims", [])
        self._scenario.setdefault("knowledge_review", {
            "verdict": "ACCEPT", "reasons": ["Scripted fixture decision; evidence gate still applies."]
        })
        self._positions = {key: 0 for key in ("proposals", "reviews", "guardians")}
        try:
            for key, parser in (("proposals", parse_proposal),
                                ("reviews", parse_review), ("guardians", parse_guardian)):
                values = self._scenario[key]
                if not isinstance(values, list) or not values:
                    raise ValueError("Expected nonempty fixture sequence")
                for value in values:
                    parser(value)
            claims = self._scenario["claims"]
            if not isinstance(claims, list) or len(claims) > 50:
                raise ValueError("Invalid fixture claims")
            for claim in claims:
                parse_claim(claim)
            parse_review(self._scenario["knowledge_review"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Invalid scripted fixture response") from None

    def _next(self, key: str):
        values = self._scenario[key]
        position = self._positions[key]
        self._positions[key] += 1
        return deepcopy(values[min(position, len(values) - 1)])

    def constructor_proposal(self, goal: str, system_record: dict, context: str,
                             feedback: dict | None = None) -> Proposal:
        return parse_proposal(self._next("proposals"))

    def critic_review(self, goal: str, system_record: dict, proposal: Proposal,
                      diff: str, verification: VerificationResult) -> Review:
        return parse_review(self._next("reviews"))

    def guardian_review(self, proposal: Proposal, diff: str, phase: str) -> GuardianResult:
        return parse_guardian(self._next("guardians"))

    def learning_proposal(self, goal: str, proposal: Proposal, diff: str,
                          verification: VerificationResult, review: Review) -> list[KnowledgeClaim]:
        evidence = _verified_evidence_ids(verification)
        if not evidence or review.verdict != Verdict.ACCEPT:
            return []
        claims = deepcopy(self._scenario["claims"])
        for claim in claims:
            claim["evidence_ids"] = [
                expanded for item in claim["evidence_ids"]
                for expanded in (evidence if item == "$verification" else [item])
            ]
            if claim["source"] == "$verification":
                claim["source"] = "Verification evidence: " + ", ".join(evidence)
        try:
            return _parse_claims({"claims": claims}, verification)
        except (TypeError, ValueError):
            raise ProviderError("Fixture claims failed evidence validation") from None

    def knowledge_review(self, claims: list[KnowledgeClaim], system_record: dict,
                         verification: VerificationResult) -> Review:
        if not _claims_supported(claims, verification):
            return _unsupported_knowledge()
        return parse_review(deepcopy(self._scenario["knowledge_review"]))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to another endpoint, even after a 307/308.
        raise ProviderError("Provider HTTP redirects are disabled")


def role_messages(role: str, human_goal: str | None, untrusted_inputs: dict) -> tuple[str, str]:
    prompt = resources.files("agent").joinpath("prompts", role + ".md").read_text(encoding="utf-8")
    payload = json.dumps({
        "human_goal": human_goal,
        "untrusted_inputs": serializable(untrusted_inputs),
    }, ensure_ascii=False, allow_nan=False)
    return prompt, payload


class JsonRoleProvider(ModelProvider):
    """Shared role payloads and schema/evidence gates, independent of transport."""

    def _call(self, role, human_goal, untrusted_inputs, parser):
        raise NotImplementedError

    def constructor_proposal(self, goal: str, system_record: dict, context: str,
                             feedback: dict | None = None) -> Proposal:
        return self._call("constructor", goal, {
            "system_record": system_record, "workspace_context": context,
            "previous_round_feedback": feedback,
        }, parse_proposal)

    def critic_review(self, goal: str, system_record: dict, proposal: Proposal,
                      diff: str, verification: VerificationResult) -> Review:
        return self._call("critic", goal, {
            "system_record": system_record, "proposal": proposal,
            "diff": diff, "verification": verification,
        }, parse_review)

    def guardian_review(self, proposal: Proposal, diff: str, phase: str) -> GuardianResult:
        return self._call("guardian", None, {
            "proposal": proposal, "diff": diff, "phase": phase,
        }, parse_guardian)

    def learning_proposal(self, goal: str, proposal: Proposal, diff: str,
                          verification: VerificationResult, review: Review) -> list[KnowledgeClaim]:
        if not _verified_evidence_ids(verification) or review.verdict != Verdict.ACCEPT:
            return []
        return self._call("learning", goal, {
            "proposal": proposal, "diff": diff, "verification": verification,
            "critic_review": review,
        }, lambda value: _parse_claims(value, verification))

    def knowledge_review(self, claims: list[KnowledgeClaim], system_record: dict,
                         verification: VerificationResult) -> Review:
        if not _claims_supported(claims, verification):
            return _unsupported_knowledge()
        return self._call("knowledge_review", None, {
            "claims": claims, "system_record": system_record,
            "verification": verification,
        }, parse_review)


class OpenAICompatibleProvider(JsonRoleProvider):
    """Explicitly configured Chat Completions JSON-mode provider, stdlib only.

    JSON mode is not schema enforcement: every response is validated locally.
    HTTP is allowed only for a loopback development server. API credentials are
    read from the named environment variable at request time and never logged.
    There are no remote tools, tool execution, or automatic endpoint fallbacks.
    """

    is_fixture = False
    name = "openai-compatible"
    MAX_RESPONSE_BYTES = 2_000_000

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1",
                 api_key_env: str = "OPENAI_API_KEY", timeout_seconds: float = 60):
        if not isinstance(model, str) or not model.strip():
            raise ProviderError("An explicit model name is required")
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise ProviderError("An API-key environment-variable name is required")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ProviderError("Provider timeout must be a positive finite number")
        if not isinstance(base_url, str) or any(c.isspace() for c in base_url):
            raise ProviderError("Invalid provider base URL")
        try:
            url = parse.urlsplit(base_url)
            host = url.hostname
            _ = url.port
            loopback = bool(host and host.lower() == "localhost")
            if host and not loopback:
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
            if (not host or url.username is not None or url.password is not None or
                    url.query or url.fragment or "\\" in base_url or
                    url.scheme not in {"http", "https"} or
                    (url.scheme == "http" and not loopback)):
                raise ValueError("Unsafe endpoint")
        except ValueError:
            raise ProviderError("Use an HTTPS base URL, or HTTP on a loopback host") from None
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        try:
            # Preserve certificate/hostname verification without honoring
            # SSLKEYLOGFILE, which would write TLS session secrets to disk.
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.verify_flags |= ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN
            tls.load_default_certs()
            tls.set_alpn_protocols(["http/1.1"])
            self._opener = request.build_opener(_NoRedirect(), request.HTTPSHandler(context=tls))
        except (OSError, ValueError):
            raise ProviderError("Provider TLS initialization failed") from None

    def _call(self, role: str, human_goal: str | None, untrusted_inputs: dict, parser):
        key = os.environ.get(self.api_key_env, "")
        if not key.strip() or "\r" in key or "\n" in key:
            raise ProviderError("The configured API-key environment variable is missing or invalid")
        prompt, user_message = role_messages(role, human_goal, untrusted_inputs)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_message},
            ],
            "response_format": {"type": "json_object"},
            "n": 1,
            "stream": False,
        }
        req = request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise ProviderError("Provider returned an unsuccessful HTTP status")
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise ProviderError("Provider response exceeded the size limit")
        except ProviderError:
            raise
        except error.HTTPError as exc:
            exc.close()
            raise ProviderError(f"Provider request failed with HTTP {exc.code}; check endpoint, model, credentials and rate limits") from None
        except (error.URLError, OSError, ValueError, HTTPException):
            raise ProviderError("Provider request failed; check endpoint, credentials, and timeout") from None
        try:
            envelope = _json_object(body.decode("utf-8"))
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected exactly one choice")
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                raise ValueError("Refused, truncated, or non-text completion")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("Expected assistant message")
            if (message.get("refusal") not in (None, "") or
                    message.get("tool_calls") is not None or
                    message.get("function_call") is not None):
                raise ValueError("Refusal or tool call")
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("Expected JSON content")
            return parser(_json_object(content))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ProviderError("Provider response failed local JSON/schema validation") from None
