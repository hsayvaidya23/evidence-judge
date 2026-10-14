"""Forgery lab: rewrite what an agent says about a run; `plant_evidence` adds what a gaming
agent would also do."""

from evidence_judge.forgery.base import (
    KIND_DESCRIPTIONS,
    MANIPULATION_KINDS,
    Forger,
    ForgeryKind,
    with_story,
)
from evidence_judge.forgery.evidence import EVIDENCE_KINDS, plant_evidence
from evidence_judge.forgery.facts import TaskFacts, derive_facts
from evidence_judge.forgery.injection import (
    INJECTED_PREDICATE_SETS,
    TAUTOLOGICAL_PREDICATE_SETS,
    injected_claims,
    injected_proposals,
)
from evidence_judge.forgery.llm_forger import ForgeryDraft, LLMForger, build_forgery_prompt
from evidence_judge.forgery.templates import TemplateForger, forge

__all__ = [
    "EVIDENCE_KINDS",
    "INJECTED_PREDICATE_SETS",
    "KIND_DESCRIPTIONS",
    "MANIPULATION_KINDS",
    "TAUTOLOGICAL_PREDICATE_SETS",
    "Forger",
    "ForgeryDraft",
    "ForgeryKind",
    "LLMForger",
    "TaskFacts",
    "TemplateForger",
    "build_forgery_prompt",
    "derive_facts",
    "forge",
    "injected_claims",
    "injected_proposals",
    "plant_evidence",
    "with_story",
]
