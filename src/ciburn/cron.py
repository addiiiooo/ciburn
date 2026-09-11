"""Minimal 5-field cron parser used to estimate how often a schedule fires."""

from __future__ import annotations

import re
from dataclasses import dataclass

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
MONTH_NAMES = {
    n: i + 1
    for i, n in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
DAY_NAMES = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    def matches(self, minute: int, hour: int, day: int, month: int, weekday: int) -> bool:
        if minute not in self.minutes or hour not in self.hours or month not in self.months:
            return False
        dom = day in self.days
        dow = weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom or dow  # POSIX cron semantics
        return dom and dow


def parse_cron(expr: str) -> CronSpec | None:
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    sets: list[frozenset[int]] = []
    for i, (part, (lo, hi)) in enumerate(zip(parts, FIELD_RANGES, strict=True)):
        names = MONTH_NAMES if i == 3 else DAY_NAMES if i == 4 else {}
        s = _parse_field(part, lo, hi, names)
        if s is None:
            return None
        if i == 4:
            s = frozenset(0 if v == 7 else v for v in s)
        sets.append(s)
    return CronSpec(
        minutes=sets[0],
        hours=sets[1],
        days=sets[2],
        months=sets[3],
        weekdays=sets[4],
        day_restricted=parts[2] != "*",
        weekday_restricted=parts[4] != "*",
    )


def _parse_field(field: str, lo: int, hi: int, names: dict[str, int]) -> frozenset[int] | None:
    out: set[int] = set()
    for piece in field.lower().split(","):
        step = 1
        if "/" in piece:
            piece, step_s = piece.split("/", 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                return None
            step = int(step_s)
        if piece == "*":
            rng = range(lo, hi + 1, step)
        elif "-" in piece:
            a, b = piece.split("-", 1)
            ai, bi = _val(a, names), _val(b, names)
            if ai is None or bi is None or ai > bi:
                return None
            rng = range(ai, bi + 1, step)
        else:
            v = _val(piece, names)
            if v is None:
                return None
            rng = range(v, hi + 1, step) if step > 1 else range(v, v + 1)
        for v in rng:
            if lo <= v <= (hi if hi != 6 else 7):
                out.add(v)
    return frozenset(out) if out else None


def _val(s: str, names: dict[str, int]) -> int | None:
    if s in names:
        return names[s]
    if re.fullmatch(r"\d+", s):
        return int(s)
    return None


def runs_per_day(expr: str) -> float | None:
    """Average firings per day over a representative 4-week window (Feb 2026)."""
    spec = parse_cron(expr)
    if spec is None:
        return None
    # 2026-02-01 is a Sunday; 28 days covers every weekday x every day-of-month 1..28
    total = 0
    for d in range(28):
        day = d + 1
        weekday = (0 + d) % 7  # Sunday=0
        for hour in spec.hours:
            for minute in spec.minutes:
                if spec.matches(minute, hour, day, 2, weekday):
                    total += 1
    return total / 28.0
