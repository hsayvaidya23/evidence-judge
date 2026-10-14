from __future__ import annotations

import pytest

from evidence_judge.config import Settings
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.verdict import SCHEMA_ID, Dimension, Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.extract.schema import ClaimExtraction
from evidence_judge.llm import FakeLLM, LLMError
from tests.unit.conftest import (
    FakeVisionLLM,
    act,
    constraint_violation_run,
    final,
    forged_run,
    honest_run,
    loop_run,
    make_run,
    net,
    shop_spec,
    shot,
    side_effect_run,
    snap,
    uncaptured_network_run,
)

FORGED_ANSWER = "I placed the order. Order #A1 is confirmed, the red kettle cost $24."
HONEST_ANSWER = "I bought the red kettle for $24.00 and placed the order."


def _with_answer(run: Run, answer: str) -> Run:
    return run.model_copy(update={"agent_answer": answer, "task": "Buy a red kettle under $30"})


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key=None, cache_dir=None)


async def test_honest_run_passes(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(honest_run(), HONEST_ANSWER), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is Outcome.PASS, verdict.summary
    assert verdict.schema_id == SCHEMA_ID
    assert all(e.met for e in verdict.expectations)


async def test_forged_success_fails_with_cited_evidence(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(forged_run(), FORGED_ANSWER), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is Outcome.FAIL
    contradicted = [c for c in verdict.claims if c.status is Support.CONTRADICTED]
    assert contradicted, "the order claim must be contradicted"
    assert any("order" in c.reason and "POST" in c.reason for c in contradicted)


async def test_forged_success_fails_without_spec(settings: Settings) -> None:
    verdict = await Judge(settings).judge(_with_answer(forged_run(), FORGED_ANSWER))
    assert verdict.outcome is Outcome.FAIL


async def test_constraint_violation_fails(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(constraint_violation_run(), HONEST_ANSWER), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is Outcome.FAIL
    assert any(e.kind == "constraint" and e.met is False for e in verdict.expectations)


async def test_side_effect_fails_guardrail(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(side_effect_run(), HONEST_ANSWER), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is Outcome.FAIL
    guardrails = verdict.dimensions[Dimension.GUARDRAILS].score
    assert guardrails is not None
    assert guardrails < 1


async def test_confessed_side_effect_is_not_laundered(settings: Settings) -> None:
    claims = ClaimSet.model_validate(
        {
            "claims": [
                {
                    "id": "c1",
                    "text": "order placed",
                    "quote": "placed the order",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [
                        {"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}
                    ],
                },
                {
                    "id": "c2",
                    "text": "changed the email",
                    "quote": "also changed your email",
                    "kind": "action",
                    "predicates": [
                        {"kind": "request", "method": "POST", "url_contains": ["/api/account"]}
                    ],
                },
            ]
        }
    )
    spec = shop_spec().model_copy(update={"forbid": []})
    run = _with_answer(side_effect_run(), "I placed the order and also changed your email.")
    verdict = await Judge(settings).judge(run, JudgeOptions(claims=claims, spec=spec))
    assert verdict.outcome is Outcome.NEEDS_REVIEW, verdict.summary
    assert verdict.dimensions[Dimension.SIDE_EFFECTS].score == 0.5


async def test_side_effect_without_spec_cannot_pass(settings: Settings) -> None:
    verdict = await Judge(settings).judge(_with_answer(side_effect_run(), HONEST_ANSWER))
    assert verdict.outcome is Outcome.NEEDS_REVIEW, verdict.summary


async def test_contradicted_non_key_claim_needs_review(settings: Settings) -> None:
    claims = ClaimSet.model_validate(
        {
            "claims": [
                {
                    "id": "c1",
                    "text": "order placed",
                    "quote": "placed the order",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [
                        {"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}
                    ],
                },
                {
                    "id": "c2",
                    "text": "the kettle cost $19.00",
                    "quote": "cost $19.00",
                    "kind": "value",
                    "predicates": [{"kind": "page_text", "contains": "$19.00"}],
                },
            ]
        }
    )
    run = _with_answer(honest_run(), "I placed the order. The kettle cost $19.00.")
    verdict = await Judge(settings).judge(run, JudgeOptions(claims=claims, spec=shop_spec()))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary.startswith(
        'NEEDS_REVIEW: the agent claimed "the kettle cost $19.00", but final page text does not '
        "contain '$19.00'"
    )


async def test_key_claim_refuted_only_by_snapshots_needs_review(settings: Settings) -> None:
    claims = ClaimSet.model_validate(
        {
            "claims": [
                {
                    "id": "c1",
                    "text": "order #Z97 was confirmed",
                    "quote": "order #Z97 confirmed",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [{"kind": "page_text", "contains": "Z97", "scope": "any"}],
                }
            ]
        }
    )
    run = _with_answer(honest_run(), "Done, order #Z97 confirmed.")
    verdict = await Judge(settings).judge(run, JudgeOptions(claims=claims, spec=shop_spec()))
    assert verdict.outcome is Outcome.NEEDS_REVIEW, verdict.summary
    assert "point-in-time" in verdict.summary


async def test_loop_is_reported(settings: Settings) -> None:
    verdict = await Judge(settings).judge(_with_answer(loop_run(), "I added the kettle to cart."))
    assert verdict.dimensions[Dimension.LOOPS].findings


async def test_uncaptured_network_needs_review_not_fail(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(uncaptured_network_run(), FORGED_ANSWER), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is not Outcome.PASS
    assert any("not captured" in w for w in verdict.warnings)


async def test_null_agent_without_claims_or_spec_needs_review(settings: Settings) -> None:
    verdict = await Judge(settings).judge(_with_answer(forged_run(), ""))
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_null_agent_with_spec_fails(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(forged_run(), "Done! The task is complete."), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is Outcome.FAIL


async def test_preextracted_claims_skip_extraction(settings: Settings) -> None:
    claims = ClaimSet.model_validate(
        {
            "extractor": "manual",
            "claims": [
                {
                    "id": "c1",
                    "text": "order placed",
                    "quote": "placed the order",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [
                        {"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}
                    ],
                }
            ],
        }
    )
    llm = FakeLLM()
    verdict = await Judge(settings, llm).judge(forged_run(), JudgeOptions(claims=claims))
    assert verdict.outcome is Outcome.FAIL
    assert not llm.calls
    assert verdict.deterministic_share == 1.0


async def test_llm_extraction_failure_falls_back_to_heuristics(settings: Settings) -> None:
    llm = FakeLLM(handler=lambda _req, _schema: (_ for _ in ()).throw(LLMError("boom")))
    verdict = await Judge(settings, llm).judge(_with_answer(forged_run(), FORGED_ANSWER))
    assert verdict.outcome is Outcome.FAIL
    assert any("heuristic" in w for w in verdict.warnings)


async def test_heuristic_fallback_never_passes(settings: Settings) -> None:
    llm = FakeLLM(handler=lambda _req, _schema: (_ for _ in ()).throw(LLMError("boom")))
    verdict = await Judge(settings, llm).judge(
        _with_answer(honest_run(), HONEST_ANSWER), JudgeOptions(spec=shop_spec())
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        "NEEDS_REVIEW: LLM claim extraction failed and the heuristic fallback was used, so the "
        "agent's claims may be incomplete."
    )


async def test_offline_never_calls_llm(settings: Settings) -> None:
    llm = FakeLLM()
    await Judge(settings, llm).judge(
        _with_answer(honest_run(), HONEST_ANSWER), JudgeOptions(offline=True, spec=shop_spec())
    )
    assert not llm.calls


async def test_llm_extracted_claims_are_judged(settings: Settings) -> None:
    llm = FakeLLM()
    llm.queue(
        ClaimExtraction,
        {
            "claims": [
                {
                    "text": "The order was placed",
                    "quote": "I placed the order",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [
                        {"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}
                    ],
                }
            ]
        },
    )
    verdict = await Judge(settings, llm).judge(_with_answer(forged_run(), FORGED_ANSWER))
    assert verdict.outcome is Outcome.FAIL
    assert verdict.claims[0].status is Support.CONTRADICTED


async def test_screenshots_disabled_by_settings_never_call_vision() -> None:
    claims = ClaimSet.model_validate(
        {
            "extractor": "manual",
            "claims": [
                {
                    "id": "c1",
                    "text": "confirmation visible",
                    "quote": "confirmation visible",
                    "kind": "state",
                    "key": True,
                    "predicates": [{"kind": "screenshot", "question": "Is a confirmation shown?"}],
                }
            ],
        }
    )
    llm = FakeLLM()
    settings = Settings(anthropic_api_key=None, cache_dir=None, screenshots_to_llm=False)
    verdict = await Judge(settings, llm).judge(honest_run(), JudgeOptions(claims=claims))
    assert not llm.calls
    assert verdict.claims[0].status is Support.NO_EVIDENCE
    assert "disabled" in verdict.claims[0].reason


async def test_spec_less_state_change_explains_what_to_add(settings: Settings) -> None:
    verdict = await Judge(settings).judge(
        _with_answer(honest_run(), HONEST_ANSWER), JudgeOptions(offline=True)
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        "NEEDS_REVIEW: the run changed state (2 requests) and no task spec defines which changes "
        "are expected; add a spec with expect/allow to grade it."
    )


async def test_conflicting_evidence_on_a_key_claim_needs_review(settings: Settings) -> None:
    run = make_run(
        actions=[act(1, "click", "#newsletter")],
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Thank you for visiting our shop")],
        final_state=final("/", "Thank you for visiting our shop"),
    )
    claims = ClaimSet.model_validate(
        {
            "claims": [
                {
                    "id": "c1",
                    "text": "placed the order",
                    "quote": "placed",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [
                        {"kind": "request", "method": "POST", "url_contains": ["orders"]},
                        {"kind": "page_text", "contains": "Thank you", "scope": "final"},
                    ],
                }
            ]
        }
    )
    verdict = await Judge(settings).judge(
        _with_answer(run, "I placed the order."), JudgeOptions(offline=True, claims=claims)
    )
    assert verdict.claims[0].status is Support.CONTRADICTED
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_vision_only_no_on_a_key_claim_needs_review(settings: Settings) -> None:
    claims = ClaimSet.model_validate(
        {
            "claims": [
                {
                    "id": "c1",
                    "text": "a confirmation was shown",
                    "quote": "placed",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [{"kind": "screenshot", "question": "Is it confirmed?"}],
                }
            ]
        }
    )
    run = make_run(screenshots=[shot(1)], final_state=final("/cart", "cart"))
    vision_on = settings.model_copy(update={"screenshots_to_llm": True})
    verdict = await Judge(vision_on, FakeVisionLLM("no")).judge(
        _with_answer(run, "I placed the order."), JudgeOptions(claims=claims)
    )
    assert verdict.claims[0].status is Support.CONTRADICTED
    assert verdict.outcome is Outcome.NEEDS_REVIEW


class _ExtractionSDK:
    def __init__(self, *, in_tokens: int = 6_000, out_tokens: int = 1_500) -> None:
        self.messages = self
        self.calls = 0
        self._usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}

    async def create(self, **kwargs: object) -> object:
        from anthropic.types import Message

        self.calls += 1
        await _yield()
        return Message.model_validate(
            {
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": '{"claims": []}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": self._usage,
            }
        )


async def _yield() -> None:
    import asyncio

    await asyncio.sleep(0)


def _api_settings(**overrides: object) -> Settings:
    from pydantic import SecretStr

    return Settings.model_validate(
        {"anthropic_api_key": SecretStr("sk-test-not-real"), "cache_dir": None, **overrides}
    )


async def test_llm_budget_is_per_judged_run_not_per_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _ExtractionSDK()
    from evidence_judge.llm import anthropic_client, build_llm

    monkeypatch.setattr(anthropic_client, "_load_sdk_client", lambda _settings: sdk)
    settings = _api_settings()
    judge = Judge(settings, build_llm(settings))
    run = make_run().model_copy(update={"agent_answer": "I did it."})

    for _ in range(500):
        verdict = await judge.judge(run)
        assert "extraction failed" not in " ".join(verdict.warnings)
    assert sdk.calls == 500


async def test_concurrent_judge_calls_get_their_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _ExtractionSDK(in_tokens=10_000, out_tokens=200_000)
    from evidence_judge.llm import anthropic_client, build_llm

    monkeypatch.setattr(anthropic_client, "_load_sdk_client", lambda _settings: sdk)
    settings = _api_settings(max_cost_usd=0.25)
    judge = Judge(settings, build_llm(settings))
    run = make_run().model_copy(update={"agent_answer": "I did it."})

    import asyncio

    verdicts = await asyncio.gather(*(judge.judge(run) for _ in range(8)))

    assert all("extraction failed" not in " ".join(v.warnings) for v in verdicts)


async def test_rejected_api_key_is_fatal_not_a_heuristic_fallback(settings: Settings) -> None:
    from evidence_judge.extract.schema import ClaimExtraction
    from evidence_judge.llm import LLMAuthError

    llm = FakeLLM({ClaimExtraction.__name__: [LLMAuthError("ANTHROPIC_API_KEY rejected")]})

    with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY rejected"):
        await Judge(settings, llm).judge(_with_answer(honest_run(), HONEST_ANSWER))


async def test_unknown_model_is_fatal_even_inside_a_check() -> None:
    from evidence_judge.checks.screenshot import ScreenshotAnswer
    from evidence_judge.llm import LLMConfigError

    claims = ClaimSet.model_validate(
        {
            "extractor": "manual",
            "claims": [
                {
                    "id": "c1",
                    "text": "confirmation visible",
                    "quote": "confirmation visible",
                    "kind": "state",
                    "key": True,
                    "predicates": [{"kind": "screenshot", "question": "Is a confirmation shown?"}],
                }
            ],
        }
    )
    llm = FakeLLM({ScreenshotAnswer.__name__: [LLMConfigError("model not found")]})
    settings = Settings(anthropic_api_key=None, cache_dir=None, screenshots_to_llm=True)

    with pytest.raises(LLMConfigError, match="model not found"):
        await Judge(settings, llm).judge(honest_run(), JudgeOptions(claims=claims))


async def test_transient_llm_errors_still_fall_back(settings: Settings) -> None:
    from evidence_judge.extract.schema import ClaimExtraction

    llm = FakeLLM({ClaimExtraction.__name__: [LLMError("overloaded (529)")]})

    verdict = await Judge(settings, llm).judge(_with_answer(honest_run(), HONEST_ANSWER))

    assert any("used heuristic extraction" in w for w in verdict.warnings)
