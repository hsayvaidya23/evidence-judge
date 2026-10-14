"""Forgery lab: rewrite what an agent says about a run, never what it did."""

from evidence_judge.forgery.base import (
    KIND_DESCRIPTIONS,
    MANIPULATION_KINDS,
    Forger,
    ForgeryKind,
    with_story,
)
from evidence_judge.forgery.facts import TaskFacts, derive_facts
from evidence_judge.forgery.injection import (
    INJECTED_PREDICATE_SETS,
    injected_claims,
    injected_proposals,
)
from evidence_judge.forgery.llm_forger import ForgeryDraft, LLMForger, build_forgery_prompt
from evidence_judge.forgery.templates import TemplateForger, forge

__all__ = [
    "INJECTED_PREDICATE_SETS",
    "KIND_DESCRIPTIONS",
    "MANIPULATION_KINDS",
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
    "with_story",
]
