"""Redaction must never change a verdict.

A seeded generator builds runs, answers, claims and specs that mention emails, card numbers and
credentials in varied case and format. Each is judged offline twice, raw and redacted, and the
outcome, every claim status and every expectation result must be identical.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import pytest

from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.predicates import PageTextPredicate, RequestPredicate, Scope
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.redact import Redactor
from tests.unit.conftest import CONFIRMATION_TEXT, final, honest_run, net, shop_spec, snap

SEEDS = range(40)
_CARDS = ("4111111111111111", "5555555555554444", "4242424242424242", "378282246310005")
_DOMAINS = ("example.com", "Mail.Example.org", "shop.test")


@dataclass(frozen=True, slots=True)
class _Case:
    run: Run
    spec: TaskSpec
    claims: ClaimSet | None


def _vary_case(rng: random.Random, value: str) -> str:
    style = rng.choice(("lower", "upper", "title", "random"))
    if style == "lower":
        return value.lower()
    if style == "upper":
        return value.upper()
    if style == "title":
        return value.title()
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in value)


def _format_card(rng: random.Random, digits: str) -> str:
    if len(digits) == 15:
        groups = [digits[:4], digits[4:10], digits[10:]]
    else:
        groups = [digits[i : i + 4] for i in range(0, len(digits), 4)]
    return rng.choice((" ", "-")).join(groups)


def _token(rng: random.Random) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "sk-proj-" + "".join(rng.choice(alphabet) for _ in range(32))


def _case(seed: int) -> _Case:
    rng = random.Random(seed)
    user = rng.choice(("jane.doe", "Bob.Smith", "ops+alerts", "x_y"))
    email = f"{user}@{rng.choice(_DOMAINS)}"
    absent_email = f"nobody{seed}@{rng.choice(_DOMAINS)}"
    card = rng.choice(_CARDS)
    card_text = _format_card(rng, card) if rng.random() < 0.5 else card
    api_key = _token(rng)
    key_method = rng.choice(("POST", "GET"))
    order_ref = rng.choice(("A1", "6011000990139424", "4111111111111111"))
    charge = rng.choice((f"paid with card {card_text}", f"was charged {card}"))
    base = honest_run()
    order = net(
        6,
        "POST",
        "/api/orders",
        201,
        body={
            "items": [{"sku": "K-1", "price": 24.0}],
            "email": _vary_case(rng, email),
            "card_number": card_text,
            "api_key": api_key,
        },
        response={"order_id": order_ref, "contact": _vary_case(rng, email)},
    )
    keep_order = rng.random() < 0.8
    network = [
        order if n.id == "net#6" else n for n in base.network if keep_order or n.id != "net#6"
    ]
    page = (
        f"{CONFIRMATION_TEXT}. Ref {order_ref}. Receipt sent to {_vary_case(rng, email)}. "
        f"Card {card_text}."
    )
    run = base.model_copy(
        update={
            "network": network,
            "snapshots": [*base.snapshots, snap(9, "/order/A1", page)],
            "final_state": final("/order/A1", page, "shot#7"),
            "agent_answer": (
                f"I placed the order, ref {order_ref}. The receipt went to "
                f"{_vary_case(rng, email)} and I {charge} using key {api_key}."
            ),
        }
    )
    spec = shop_spec().model_copy(
        update={
            "expect": [
                *shop_spec().expect,
                Expectation(
                    id="receipt",
                    any=[PageTextPredicate(contains=f"sent to {_vary_case(rng, email)}")],
                ),
                Expectation(
                    id="profile_sent",
                    any=[RequestPredicate(method="POST", body_contains=[_vary_case(rng, email)])],
                ),
            ]
        }
    )
    if rng.random() < 0.25:
        return _Case(run, spec, None)
    claims = [
        Claim(
            id="c1",
            text="placed the order",
            quote="I placed the order",
            kind=ClaimKind.OUTCOME,
            key=True,
            predicates=[RequestPredicate(method="POST", url_contains=["/api/orders"])],
        ),
        Claim(
            id="c2",
            text=f"receipt went to {_vary_case(rng, email)}",
            quote=f"receipt went to {_vary_case(rng, email)}",
            kind=ClaimKind.STATE,
            key=rng.random() < 0.5,
            predicates=[PageTextPredicate(contains=_vary_case(rng, email))],
        ),
        Claim(
            id="c3",
            text=charge,
            quote=f"I {charge}",
            kind=ClaimKind.ACTION,
            predicates=[RequestPredicate(method="POST", body_contains=[card_text])],
        ),
        Claim(
            id="c4",
            text="used the api key",
            quote=f"using key {api_key}",
            kind=ClaimKind.ACTION,
            predicates=[RequestPredicate(method=key_method, body_contains=[api_key])],
        ),
        Claim(
            id="c5",
            text=f"cc to {absent_email}",
            quote=f"cc to {_vary_case(rng, absent_email)}",
            kind=ClaimKind.STATE,
            key=rng.random() < 0.5,
            predicates=[PageTextPredicate(contains=_vary_case(rng, absent_email), scope=Scope.ANY)],
        ),
    ]
    if rng.random() < 0.6:
        claims.pop()
    claims.append(
        Claim(
            id="c6",
            text=f"order ref {order_ref}",
            quote=f"ref {order_ref}",
            kind=ClaimKind.VALUE,
            predicates=[PageTextPredicate(contains=f"Ref {order_ref}")],
        )
    )
    rng.shuffle(claims)
    return _Case(run, spec, ClaimSet(claims=claims))


def _judge(case: _Case) -> RunVerdict:
    settings = Settings.model_validate({"anthropic_api_key": None, "cache_dir": None})
    options = JudgeOptions(offline=True, spec=case.spec, claims=case.claims)
    return asyncio.run(Judge(settings).judge(case.run, options))


def _redacted(case: _Case, seed: int) -> _Case:
    redactor = Redactor(f"invariance-{seed}".encode())
    run, report = redactor.redact_run(case.run)
    assert report.total > 0
    spec, _ = redactor.redact_model(case.spec)
    claims = redactor.redact_model(case.claims)[0] if case.claims is not None else None
    return _Case(run, spec, claims)


def _shape(v: RunVerdict) -> tuple[object, ...]:
    return (
        v.outcome,
        sorted((c.claim_id, c.status) for c in v.claims),
        sorted((e.expectation_id, e.met) for e in v.expectations),
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_redaction_does_not_change_the_verdict(seed: int) -> None:
    case = _case(seed)
    assert _shape(_judge(_redacted(case, seed))) == _shape(_judge(case))


def test_generator_covers_every_outcome_and_status() -> None:
    verdicts = [_judge(_case(seed)) for seed in SEEDS]
    assert {v.outcome for v in verdicts} == set(Outcome)
    assert {c.status for v in verdicts for c in v.claims} == set(Support)
