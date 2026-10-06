"""Bounded Constructor -> Verification -> Critic loop with two Guardian gates."""
from pathlib import Path

from .cancellation import RunCancelled

from .schema import (Risk, Verdict, parse_claim, parse_guardian, parse_proposal,
                     parse_review, serializable)
from .storage import AuditLog, read_record, record_lock, save_claims, write_json


class HumanReviewRequired(RuntimeError):
    """Compatibility exception type; run() returns HUMAN_REQUIRED with reasons."""


class AgentOrchestrator:
    def __init__(self, provider, workspace, record_path, run_dir, max_rounds=3):
        if type(max_rounds) is not int or not 1 <= max_rounds <= 10:
            raise ValueError("max_rounds must be between 1 and 10")
        self.provider = provider
        self.workspace = workspace
        self.record_path = Path(record_path)
        self.run_dir = Path(run_dir)
        self.max_rounds = max_rounds
        self.audit = AuditLog(self.run_dir / "audit.jsonl")
        self.state = "PLAN"
        self.round = 0

    def _event(self, state, **data):
        self.state = state
        self.audit.append(state, round=self.round, **data)

    def _finish(self, status, **data):
        result = {"status": status, "rounds": self.round, "run_dir": str(self.run_dir.resolve()),
                  "audit_log": str((self.run_dir / "audit.jsonl").resolve()),
                  "knowledge_status": "none", **serializable(data)}
        self._event(status, result=result)
        write_json(self.run_dir / "result.json", result)
        return result

    def _gate(self, proposal, diff, phase):
        result = parse_guardian(serializable(self.provider.guardian_review(proposal, diff, phase)))
        self.audit.append("guardian", round=self.round, phase=phase, result=result)
        if result.risk != Risk.GREEN or result.human_review_required:
            raise HumanReviewRequired(f"Guardian {phase}: {result.risk.value}: {'; '.join(result.reasons)}")
        return result

    def run(self, goal: str) -> dict:
        try:
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("A nonempty human goal is required")
            with record_lock(self.record_path):
                return self._run(goal)
        except RunCancelled:
            return self._finish("CANCELLED", reasons=["Harness invocation cancelled"], stopped_at=self.state)
        except HumanReviewRequired as exc:
            return self._finish("HUMAN_REQUIRED", reasons=[str(exc)], stopped_at=self.state)
        except Exception as exc:
            return self._finish("ERROR", reasons=[str(exc)], error_type=type(exc).__name__, stopped_at=self.state)

    def _run(self, goal):
        self._event("PLAN", goal=goal, max_rounds=self.max_rounds,
                    provider=type(self.provider).__name__)
        record = read_record(self.record_path)
        if not getattr(self.workspace, "allow_execution", False):
            raise HumanReviewRequired("Verification executes candidate code on this host. Review configured commands and run with --allow-execution in a trusted disposable environment.")
        if not getattr(self.workspace, "commands", []):
            raise HumanReviewRequired("At least one user-configured verification command is required")
        self.workspace.prepare()
        initial_context = self.workspace.context()
        self.audit.append("workspace", base_commit=getattr(self.workspace, "base_commit", ""),
                          commands=self.workspace.commands)
        feedback = None
        for self.round in range(1, self.max_rounds + 1):
            self._event("CONSTRUCTOR_PROPOSAL")
            proposal = parse_proposal(serializable(self.provider.constructor_proposal(
                goal, record, initial_context, feedback)))
            self.audit.append("proposal", round=self.round, proposal=proposal)
            self._event("GUARDIAN_PRECHECK")
            if set(proposal.side_effects) != {"local_files"}:
                raise HumanReviewRequired("Only reversible local file candidates are supported; declared effects: " + ", ".join(proposal.side_effects))
            self._gate(proposal, "", "precheck")
            self._event("IMPLEMENT")
            self.workspace.stage(proposal)
            diff = self.workspace.diff()
            if not diff.strip():
                feedback = {"reason": "No change was produced", "previous_proposal": serializable(proposal)}
                self._event("CONSTRUCTOR_REVISE", feedback=feedback)
                continue
            write_json(self.run_dir / f"proposal-{self.round}.json", proposal)
            (self.run_dir / f"candidate-{self.round}.patch").write_text(diff, encoding="utf-8", newline="\n")
            self._event("VERIFY")
            verification = self.workspace.verify()
            self.audit.append("verification", round=self.round, verification=verification)
            self.workspace.check_integrity()
            if self.workspace.diff() != diff:
                raise RuntimeError("Verification changed the candidate; evidence no longer matches the diff")
            expected = [command.name for command in self.workspace.commands]
            actual = [result.name for result in verification.results]
            if expected != actual or not verification.results:
                raise RuntimeError("Verification evidence does not cover the configured command list")
            passed = verification.ok and all(result.returncode == 0 and not result.timed_out
                                             and result.evidence_id for result in verification.results)
            if not passed:
                feedback = {"reason": "Verification failed", "verification": serializable(verification),
                            "previous_proposal": serializable(proposal)}
                self._event("CONSTRUCTOR_REVISE", feedback=feedback)
                continue
            self._event("CRITIC_REVIEW")
            review = parse_review(serializable(self.provider.critic_review(goal, record, proposal, diff, verification)))
            self.audit.append("review", round=self.round, review=review)
            if review.verdict == Verdict.BLOCK:
                return self._finish("BLOCKED", review=review, verification=verification)
            if review.verdict == Verdict.REVISE:
                feedback = {"reason": "Critic requests revision", "review": serializable(review),
                            "verification": serializable(verification), "previous_proposal": serializable(proposal)}
                self._event("CONSTRUCTOR_REVISE", feedback=feedback)
                continue
            self._event("GUARDIAN_FINAL_CHECK")
            guardian = self._gate(proposal, diff, "final")
            self.workspace.check_integrity()
            if self.workspace.diff() != diff:
                raise RuntimeError("Candidate changed after review")
            patch = self.run_dir / "accepted.patch"
            self.workspace.export_patch(patch)
            self._event("ACCEPT_CHANGE", patch=str(patch), source_applied=False)
            knowledge_status, saved, knowledge_error = self._learn(goal, record, proposal, diff, verification, review, patch)
            return self._finish("ACCEPTED", proposal=proposal, review=review, guardian=guardian,
                                verification=verification, patch=str(patch.resolve()), source_applied=False,
                                knowledge_status=knowledge_status, knowledge_claims=saved,
                                knowledge_error=knowledge_error)
        return self._finish("EXHAUSTED", feedback=feedback,
                            reasons=["Revision budget exhausted; no accepted patch or knowledge update"])

    def _learn(self, goal, record, proposal, diff, verification, review, patch):
        # Code acceptance and knowledge acceptance are distinct outcomes.
        try:
            self._event("LEARNING_PROPOSAL")
            raw_claims = self.provider.learning_proposal(goal, proposal, diff, verification, review)
            if not isinstance(raw_claims, list) or len(raw_claims) > 50:
                raise ValueError("Learning proposal must be a list with at most 50 claims")
            claims = [parse_claim(serializable(claim)) for claim in raw_claims]
            self.audit.append("knowledge_proposal", claims=claims)
            if not claims:
                return "none", [], None
            evidence = {result.evidence_id for result in verification.results
                        if result.returncode == 0 and not result.timed_out}
            for claim in claims:
                if not set(claim.evidence_ids).issubset(evidence):
                    raise ValueError("Claim cites nonexistent or failing verification evidence")
            self._event("CRITIC_KNOWLEDGE_REVIEW")
            knowledge_review = parse_review(serializable(self.provider.knowledge_review(claims, record, verification)))
            self.audit.append("knowledge_review", review=knowledge_review)
            if knowledge_review.verdict != Verdict.ACCEPT:
                return "rejected", [], "; ".join(knowledge_review.reasons)
            self._event("UPDATE_SYSTEM_RECORD")
            saved = save_claims(record, claims, self.record_path, run_id=self.run_dir.name,
                                patch=patch, base_commit=getattr(self.workspace, "base_commit", ""))
            self.audit.append("knowledge_saved", claims=saved)
            return "saved", saved, None
        except Exception as exc:
            self.audit.append("knowledge_error", error_type=type(exc).__name__, reason=str(exc))
            return "failed", [], str(exc)
