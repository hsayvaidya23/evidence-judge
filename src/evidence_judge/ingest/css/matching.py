"""Whether a selector matches an element: NO, MAYBE or YES."""

from __future__ import annotations

from typing import Final

from evidence_judge.ingest.css.model import MAYBE, NO, YES, Compound, Element, Pseudo, Selector

MAX_MATCH_STEPS: Final = 50_000
"""How many (compound, element) tests one selector match may make before the reader gives up
and answers MAYBE."""


class _BudgetSpentError(Exception):
    pass


def matches(selector: Selector, element: Element) -> int:
    if not selector.ancestors <= element.ancestry:
        return NO
    last = len(selector.parts) - 1
    found = _compound_match(selector.parts[last], element)
    if found == NO:
        return NO
    if last > 0:
        try:
            found = _Matcher(selector).top(last, element, found)
        except _BudgetSpentError:
            return MAYBE
    return min(found, MAYBE) if selector.maybe else found


class _Matcher:
    """One selector matched against one element. Each (compound, element) answer and each
    "an ancestor or earlier sibling matches" answer is kept on the element (`Element.matched`)
    for every later match of the same selector, so a selector with many descendant combinators
    costs (parts x depth) tests over a whole tree, not (depth ^ parts) for each element."""

    __slots__ = ("_id", "_selector", "_steps")

    def __init__(self, selector: Selector) -> None:
        self._selector = selector
        self._id = id(selector)
        self._steps = 0

    def top(self, index: int, element: Element, found: int) -> int:
        """The match of the last compound, which only a fresh `matches` asks about: its own
        answer `found` combined with the rest, not kept."""
        self._steps += 1
        return min(found, self._left(index, element))

    def match(self, index: int, element: Element) -> int:
        key = (self._id, index)
        known = _known(element, key)
        if known is not None and known[0] is self._selector:
            return known[1]
        self._steps += 1
        if self._steps > MAX_MATCH_STEPS:
            raise _BudgetSpentError
        found = _compound_match(self._selector.parts[index], element)
        if found != NO and index > 0:
            found = min(found, self._left(index, element))
        _keep(element, key, (self._selector, found))
        return found

    def _left(self, index: int, element: Element) -> int:
        """How the selector up to the compound before `index` matches what the combinator
        before `index` points to from `element`."""
        combinator = self._selector.combinators[index - 1]
        if combinator == ">":
            parent = element.parent
            return NO if parent is None else self.match(index - 1, parent)
        if combinator == "+":
            previous = element.previous
            return NO if previous is None else self.match(index - 1, previous)
        up = combinator == " "
        key = (self._id, -index)
        walked: list[tuple[Element, int]] = []
        tail = NO
        other = element.parent if up else element.previous
        while other is not None:
            known = _known(other, key)
            if known is not None and known[0] is self._selector:
                tail = known[1]
                break
            found = self.match(index - 1, other)
            walked.append((other, found))
            if found == YES:
                break
            other = other.parent if up else other.previous
        for item, found in reversed(walked):
            tail = max(tail, found)
            _keep(item, key, (self._selector, tail))
        return tail


def _known(element: Element, key: tuple[int, int]) -> tuple[Selector, int] | None:
    return None if element.matched is None else element.matched.get(key)


def _keep(element: Element, key: tuple[int, int], answer: tuple[Selector, int]) -> None:
    if element.matched is None:
        element.matched = {}
    element.matched[key] = answer


def _compound_match(part: Compound, element: Element) -> int:
    if element.host is not None:
        if not part.host:
            return NO
        if part.host_maybe:
            return MAYBE
        return min((_compound_match(arg, element.host) for arg in part.host_args), default=YES)
    if part.host:
        return NO
    if part.tag is not None and part.tag != element.tag:
        return NO
    if any(i != element.attrs.get("id") for i in part.ids):
        return NO
    if any(c not in element.classes for c in part.classes):
        return NO
    if not all(_attr_matches(a, element.attrs) for a in part.attrs):
        return NO
    found = YES
    for pseudo in part.pseudos:
        found = min(found, _pseudo_match(pseudo, element))
        if found == NO:
            return NO
    return found


def _pseudo_match(pseudo: Pseudo, element: Element) -> int:
    match pseudo.name:
        case "root":
            return YES if element.tag == "HTML" and element.parent is None else NO
        case "not" | "is" | "where":
            found = max((matches(s, element) for s in pseudo.args), default=NO)
            if pseudo.maybe:
                found = max(found, MAYBE)
            if pseudo.name == "not":
                return {NO: YES, MAYBE: MAYBE, YES: NO}[found]
            return found
        case "defined":
            return MAYBE if "-" in element.tag else YES
        case "any-link":
            return YES if element.tag in ("A", "AREA") and "href" in element.attrs else NO
        case "maybe":
            return MAYBE
        case _:
            return _structural(pseudo, element)


def _structural(pseudo: Pseudo, element: Element) -> int:
    siblings = element.siblings or [element]
    index = element.index if element.siblings is not None else 0
    typed = pseudo.name.endswith("of-type")
    pool = [s for s in siblings if s.tag == element.tag] if typed else siblings
    position = pool.index(element) if typed else index
    count = len(pool)
    name = pseudo.name.removesuffix("-of-type").removesuffix("-child")
    if name == "first":
        return YES if position == 0 else NO
    if name == "last":
        return YES if position == count - 1 else NO
    if name == "only":
        return YES if count == 1 else NO
    step, offset = pseudo.nth or (0, 1)
    place = count - position if "last" in name else position + 1
    if step == 0:
        return YES if place == offset else NO
    steps, rest = divmod(place - offset, step)
    return YES if rest == 0 and steps >= 0 else NO


def _attr_matches(test: tuple[str, str | None, str, bool], attrs: dict[str, str]) -> bool:
    name, op, wanted, folded = test
    actual = next((v for k, v in attrs.items() if k.lower() == name), None)
    if actual is None:
        return False
    if op is None:
        return True
    if folded:
        actual, wanted = actual.lower(), wanted.lower()
    match op:
        case "=":
            return actual == wanted
        case "~=":
            return wanted in actual.split()
        case "|=":
            return actual == wanted or actual.startswith(f"{wanted}-")
        case "^=":
            return bool(wanted) and actual.startswith(wanted)
        case "$=":
            return bool(wanted) and actual.endswith(wanted)
        case _:
            return bool(wanted) and wanted in actual
