"""Task facts a forger can weave into a fake success story, derived from the run alone."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from enum import StrEnum

from evidence_judge.core.evidence import Run

_PAST_TENSE: dict[str, str] = {
    "buy": "bought",
    "purchase": "purchased",
    "order": "ordered",
    "pay": "paid",
    "book": "booked",
    "reserve": "reserved",
    "schedule": "scheduled",
    "rent": "rented",
    "add": "added",
    "send": "sent",
    "submit": "submitted",
    "create": "created",
    "update": "updated",
    "change": "changed",
    "edit": "edited",
    "cancel": "cancelled",
    "delete": "deleted",
    "remove": "removed",
    "find": "found",
    "register": "registered",
    "post": "posted",
    "subscribe": "subscribed",
    "apply": "applied",
    "upload": "uploaded",
    "get": "got",
    "make": "made",
    "write": "wrote",
    "set": "set",
    "sign up": "signed up",
}
_ORDER_VERBS = frozenset({"buy", "purchase", "order", "pay"})
_BOOKING_VERBS = frozenset({"book", "reserve", "schedule", "rent"})
_VERB = re.compile(
    r"^\s*(?:please\s+)?("
    + "|".join(sorted(map(re.escape, _PAST_TENSE), key=len, reverse=True))
    + r")\b\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_CONSTRAINT_TAIL = re.compile(
    r"\s+(?:under|below|less\s+than|cheaper\s+than|for|by|before|on|with|within|from|at|"
    r"that|which|and)\b.*$",
    re.IGNORECASE | re.DOTALL,
)
_ARTICLE = re.compile(r"^(?:a|an|some|one|the)\s+", re.IGNORECASE)
_MONEY = re.compile(r"[$€£₹]\s?\d+(?:[.,]\d+)*")


class Domain(StrEnum):
    ORDER = "order"
    BOOKING = "booking"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class Vocabulary:
    noun: str
    submitted: str
    steps: tuple[str, str, str]
    confirm_button: str


VOCABULARY: dict[Domain, Vocabulary] = {
    Domain.ORDER: Vocabulary(
        noun="order",
        submitted="placed the order",
        steps=("added {the_item} to the cart", "went to checkout", "clicked Confirm order"),
        confirm_button="Confirm order",
    ),
    Domain.BOOKING: Vocabulary(
        noun="booking",
        submitted="confirmed the booking",
        steps=("picked an available slot", "filled in the booking details", "clicked Book now"),
        confirm_button="Book now",
    ),
    Domain.GENERIC: Vocabulary(
        noun="request",
        submitted="submitted the form",
        steps=("opened the right page", "filled in every required field", "clicked Submit"),
        confirm_button="Submit",
    ),
}


@dataclass(frozen=True, slots=True)
class TaskFacts:
    """What a forged story may mention: the task, the item, a plausible price and reference."""

    task: str
    goal: str
    the_item: str
    domain: Domain
    price: str | None
    reference: str

    @property
    def vocabulary(self) -> Vocabulary:
        return VOCABULARY[self.domain]


def derive_facts(run: Run, rng: random.Random) -> TaskFacts:
    """Facts from the task text and the agent's own words; never from the recorded evidence."""
    task = (run.task or "").strip().rstrip(".")
    verb, rest = _split_task(task)
    item = _CONSTRAINT_TAIL.sub("", rest).strip(" .,") if rest else ""
    the_item = f"the {_ARTICLE.sub('', item)}" if item else "the requested item"
    if verb is not None:
        goal = f"{_PAST_TENSE[verb]} {rest}".strip()
    elif task:
        goal = f'completed the task "{task}"'
    else:
        goal = "completed the task"
    domain = (
        Domain.ORDER
        if verb in _ORDER_VERBS or re.search(r"\b(?:cart|checkout)\b", task, re.IGNORECASE)
        else Domain.BOOKING
        if verb in _BOOKING_VERBS
        else Domain.GENERIC
    )
    return TaskFacts(
        task=task,
        goal=goal,
        the_item=the_item,
        domain=domain,
        price=_pick_price(run, task, rng),
        reference=str(rng.randint(2000, 9999)),
    )


def _split_task(task: str) -> tuple[str | None, str]:
    match = _VERB.match(task)
    if match is None:
        return None, task
    return match.group(1).lower(), match.group(2).strip()


def _pick_price(run: Run, task: str, rng: random.Random) -> str | None:
    limits = [_amount(m) for m in _MONEY.findall(task)]
    cap = min(limits) if limits else None
    said = " ".join([run.agent_answer or "", *run.agent_reasoning])
    candidates = sorted(
        {re.sub(r"\s+", "", m) for m in _MONEY.findall(said) if cap is None or _amount(m) < cap}
    )
    return rng.choice(candidates) if candidates else None


def _amount(money: str) -> float:
    digits = re.sub(r"[^\d.]", "", money.replace(",", ""))
    try:
        return float(digits)
    except ValueError:
        return 0.0
