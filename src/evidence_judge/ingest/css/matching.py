"""Whether a selector matches an element: NO, MAYBE or YES."""

from __future__ import annotations

from evidence_judge.ingest.css.model import MAYBE, NO, YES, Compound, Element, Pseudo, Selector


def matches(selector: Selector, element: Element) -> int:
    found = _match(selector, len(selector.parts) - 1, element)
    return min(found, MAYBE) if selector.maybe else found


def _match(selector: Selector, index: int, element: Element) -> int:
    found = _compound_match(selector.parts[index], element)
    if found == NO or index == 0:
        return found
    combinator = selector.combinators[index - 1]
    if combinator == ">":
        parent = element.parent
        return NO if parent is None else min(found, _match(selector, index - 1, parent))
    if combinator == "+":
        previous = element.previous
        return NO if previous is None else min(found, _match(selector, index - 1, previous))
    best = NO
    other = element.parent if combinator == " " else element.previous
    while other is not None:
        matched = _match(selector, index - 1, other)
        if matched == YES:
            return found
        best = max(best, matched)
        other = other.parent if combinator == " " else other.previous
    return min(found, best)


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
