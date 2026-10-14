"""Output schema the LLM fills when extracting claims."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    ValidatorFunctionWrapHandler,
    model_validator,
)

from evidence_judge.core.claims import ClaimKind
from evidence_judge.core.predicates import Predicate, vacuity_reason

_PREDICATE: TypeAdapter[Predicate] = TypeAdapter(Predicate)


def _usable(raw: object) -> Predicate | None:
    try:
        predicate = _PREDICATE.validate_python(raw)
    except ValidationError:
        return None
    return predicate if vacuity_reason(predicate) is None else None


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, description="The claim restated in plain words")
    quote: str = Field(
        min_length=1, description="Exact, verbatim span copied from the agent output"
    )
    kind: ClaimKind
    origin: Literal["answer", "reasoning"] = Field(
        default="answer", description="Which part of the agent output the quote comes from"
    )
    key: bool = Field(default=False, description="True if the task's success depends on it")
    predicates: list[Predicate] = Field(
        default_factory=list[Predicate],
        description="1-3 evidence checks; any one satisfied proves the claim",
    )
    _dropped: int = PrivateAttr(default=0)

    @property
    def dropped_predicates(self) -> int:
        """Proposed predicates dropped as malformed or too weak to prove the claim."""
        return self._dropped

    @model_validator(mode="wrap")
    @classmethod
    def _drop_vacuous(cls, data: object, handler: ValidatorFunctionWrapHandler) -> ExtractedClaim:
        dropped = 0
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else None
        if fields is not None and isinstance(fields.get("predicates"), list):
            proposed = cast(list[object], fields["predicates"])
            kept = [p for p in map(_usable, proposed) if p is not None]
            dropped = len(proposed) - len(kept)
            data = {**fields, "predicates": kept}
        claim = cast(ExtractedClaim, handler(data))
        claim._dropped = dropped
        return claim


class ClaimExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claims: list[ExtractedClaim] = Field(default_factory=list[ExtractedClaim])
