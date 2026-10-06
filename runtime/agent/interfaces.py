from abc import ABC, abstractmethod
from .schema import Proposal, Review, GuardianResult, VerificationResult, KnowledgeClaim


class ModelProvider(ABC):
    @abstractmethod
    def constructor_proposal(self, goal: str, system_record: dict, context: str,
                             feedback: dict | None = None) -> Proposal: ...

    @abstractmethod
    def critic_review(self, goal: str, system_record: dict, proposal: Proposal,
                      diff: str, verification: VerificationResult) -> Review: ...

    @abstractmethod
    def guardian_review(self, proposal: Proposal, diff: str, phase: str) -> GuardianResult: ...

    @abstractmethod
    def learning_proposal(self, goal: str, proposal: Proposal, diff: str,
                          verification: VerificationResult, review: Review) -> list[KnowledgeClaim]: ...

    @abstractmethod
    def knowledge_review(self, claims: list[KnowledgeClaim], system_record: dict,
                         verification: VerificationResult) -> Review: ...


class Workspace(ABC):
    @abstractmethod
    def prepare(self) -> None: ...

    @abstractmethod
    def context(self) -> str: ...

    @abstractmethod
    def stage(self, proposal: Proposal) -> None: ...

    @abstractmethod
    def diff(self) -> str: ...

    @abstractmethod
    def verify(self) -> VerificationResult: ...

    @abstractmethod
    def check_integrity(self) -> None: ...

    @abstractmethod
    def export_patch(self, path) -> None: ...
