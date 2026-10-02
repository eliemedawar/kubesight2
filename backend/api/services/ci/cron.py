"""Five-field cron, evaluated in a named timezone.

The one thing a scheduled build has to get right is WHEN, and the two ways a
home-grown cron goes wrong are both about local time: a nightly job that runs at
the wrong hour for half the year because it was computed in UTC, and a job that
runs twice — or not at all — on the night the clocks change. So every schedule
carries an IANA zone and this module does its arithmetic on that zone's wall
clock, converting to an instant only at the end.

Syntax is the standard one, so an expression copied out of a crontab or a
Jenkinsfile means the same thing here::

    ┌ minute        0-59
    │ ┌ hour        0-23
    │ │ ┌ day       1-31
    │ │ │ ┌ month   1-12 or JAN-DEC
    │ │ │ │ ┌ dow   0-7 or SUN-SAT (0 and 7 are both Sunday)
    * * * * *

Each field takes ``*``, a value, a range ``a-b``, a step ``*/n`` / ``a-b/n`` /
``a/n`` (``a`` to the end of the field), and comma lists of any of those.

Day-of-month and day-of-week follow Vixie cron, the rule every crontab in
existence was written against: when both are written without a leading ``*`` a
day matches if EITHER does (``0 9 1 * MON`` is "the 1st, and every Monday");
when either starts with ``*`` — ``*/2`` included — a day must match BOTH.

Aliases: ``@hourly @daily @midnight @weekly @monthly @yearly @annually`` mean
what they mean everywhere. ``@nightly`` is KubeSight's own and means 02:00 every
day: "nightly" in a build system is the 2 a.m. run, clear of midnight batch
jobs and of the evening, and an alias that read "nightly" but fired at 00:00
would be a surprise to exactly the person typing it. ``@reboot`` has no meaning
for a server-side scheduler and is refused.

Daylight saving, also Vixie's rule, because it is the one that surprises nobody:

* A run at a FIXED hour (``30 2 * * *``) whose wall-clock time does not exist
  that night — the clocks jump 02:00 → 03:00 — moves forward to the moment the
  clocks jump to, so the nightly still runs, once, at 03:00. On the night a
  time happens twice it runs once, at the first occurrence.
* A job that runs EVERY hour (hour field ``*``) is an interval, not an
  appointment: times that do not exist are simply not there (the next interval
  comes along), and the repeated hour is run through again, so "every 15
  minutes" keeps meaning every 15 real minutes.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import FrozenSet, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_EXPRESSION_CHARS = 120

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
    # KubeSight's own — see the module docstring for why 02:00.
    "@nightly": "0 2 * * *",
}

_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_DOW_NAMES = {
    name: index
    for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}

# (label, low, high, names) — dow's high is 7 because 7 is accepted as Sunday.
_FIELDS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, _MONTH_NAMES),
    ("day-of-week", 0, 7, _DOW_NAMES),
)

# How far either side of "now" a wall-clock time can sit from the instant it
# maps to across a transition. The largest DST shift in the tz database is two
# hours; three leaves room without making the search noticeably longer.
_FOLD_WINDOW = timedelta(hours=3)
# How many years ahead a valid expression is searched: the 28-year cycle after
# which dates fall on the same weekdays again, plus one — so "Feb 29, when it
# is a Monday" is found too. Only matching months are walked, so a horizon
# this long costs nothing for an ordinary expression.
_SEARCH_YEARS = 29

_ITEM_RE = re.compile(r"^(?P<range>\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/(?P<step>\d+))?$")


class CronError(ValueError):
    """An expression or timezone that cannot be used. Message is user-facing."""


@dataclass(frozen=True)
class CronExpression:
    """A parsed expression. Weekdays are 0-6 with Sunday = 0."""

    source: str
    minutes: FrozenSet[int]
    hours: FrozenSet[int]
    days: FrozenSet[int]
    months: FrozenSet[int]
    weekdays: FrozenSet[int]
    # Vixie's "starts with *" flags, which decide the day-of-month /
    # day-of-week OR rule.
    dom_star: bool
    dow_star: bool
    hour_star: bool
    alias: Optional[str] = None

    @property
    def fields(self) -> str:
        """The five-field form, aliases expanded."""
        return ALIASES.get(self.alias, self.source) if self.alias else self.source


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _value(token: str, label: str, low: int, high: int, names) -> int:
    upper = token.upper()
    if names and upper in names:
        return names[upper]
    if not token.isdigit():
        if names:
            known = ", ".join(sorted(names, key=names.get))
            raise CronError(f"The {label} field has '{token}', which is not a number or one of {known}.")
        raise CronError(f"The {label} field has '{token}', which is not a number.")
    number = int(token)
    if not low <= number <= high:
        raise CronError(f"The {label} field has {number}, outside {low}-{high}.")
    return number


def _parse_field(text: str, index: int) -> Tuple[FrozenSet[int], bool]:
    label, low, high, names = _FIELDS[index]
    values = set()
    for item in text.split(","):
        if not item:
            raise CronError(f"The {label} field has an empty item — check for a stray comma.")
        match = _ITEM_RE.match(item)
        if not match:
            raise CronError(f"The {label} field has '{item}', which cron cannot read.")
        part, step_text = match.group("range"), match.group("step")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"The {label} field steps by 0, which never moves.")

        if part == "*":
            start, end = low, high
        elif "-" in part:
            first, last = part.split("-", 1)
            start = _value(first, label, low, high, names)
            end = _value(last, label, low, high, names)
            if start > end:
                raise CronError(
                    f"The {label} field has the range {part}, which runs backwards. "
                    f"Write it as two ranges, e.g. {first}-{high},{low}-{last}."
                )
        else:
            start = _value(part, label, low, high, names)
            # "5/15" is "from 5 to the end, every 15" — a lone value with a
            # step only makes sense that way.
            end = high if step_text else start

        values.update(range(start, end + 1, step))

    if index == 4:
        # 7 is Sunday as well as 0; one name for one day from here on.
        values = {0 if value == 7 else value for value in values}
    return frozenset(values), text.startswith("*")


def parse(text: str) -> CronExpression:
    """Parse an expression or alias, or raise :class:`CronError` saying why not."""
    source = " ".join(str(text or "").split())
    if not source:
        raise CronError("Enter a cron expression, e.g. '0 2 * * *' for 02:00 every day.")
    if len(source) > MAX_EXPRESSION_CHARS:
        raise CronError(f"A cron expression is at most {MAX_EXPRESSION_CHARS} characters.")

    alias = None
    if source.startswith("@"):
        alias = source.lower()
        if alias == "@reboot":
            raise CronError("@reboot has no meaning for a scheduled build. Use a time, e.g. @nightly.")
        if alias not in ALIASES:
            raise CronError(
                f"Unknown shortcut '{source}'. Use one of: {', '.join(ALIASES)}."
            )
        fields_text = ALIASES[alias]
    else:
        fields_text = source

    fields = fields_text.split(" ")
    if len(fields) != 5:
        raise CronError(
            "A cron expression has five fields — minute, hour, day of month, month, "
            f"day of week — and this one has {len(fields)}."
        )

    minutes, _ = _parse_field(fields[0], 0)
    hours, hour_star = _parse_field(fields[1], 1)
    days, dom_star = _parse_field(fields[2], 2)
    months, _ = _parse_field(fields[3], 3)
    weekdays, dow_star = _parse_field(fields[4], 4)

    expression = CronExpression(
        source=alias or source,
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        dom_star=dom_star,
        dow_star=dow_star,
        # "Every hour" for the DST rule means the hour field is all 24 hours,
        # not merely that it was written with a star: */2 is an appointment
        # every other hour, and appointments shift instead of vanishing.
        hour_star=len(hours) == 24,
        alias=alias,
    )
    _check_reachable(expression)
    return expression


def _check_reachable(expression: CronExpression) -> None:
    """Refuse an expression that can never fire, such as ``0 0 30 2 *``.

    Only the day-of-month alone can do this: with day-of-week in play the OR
    rule always leaves some day. Saved silently it would be a schedule that
    looks armed and never runs, which is worse than an error now.
    """
    if expression.dom_star or not expression.dow_star:
        # A starred day-of-month always includes the 1st; an un-starred pair is
        # OR, and any matching weekday will do.
        return
    # Day-of-month restricted, ANDed with the weekdays: the date has to exist.
    # Whether it also lands on one of the weekdays is a question the 28-year
    # calendar cycle always answers yes to, within the search horizon.
    for month in expression.months:
        # 29 for February: a leap year will come.
        longest = 29 if month == 2 else calendar.monthrange(2001, month)[1]
        if any(day <= longest for day in expression.days):
            return
    days = ", ".join(str(day) for day in sorted(expression.days))
    raise CronError(
        f"Day {days} never falls in the selected month(s), so this schedule would never run."
    )


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------

def zone(name: Optional[str]) -> ZoneInfo:
    """The IANA zone ``name`` names, or :class:`CronError`. Empty is UTC."""
    text = str(name or "").strip() or "UTC"
    # ZoneInfo reads a file named after the key; refuse anything that is not
    # shaped like a zone name before it gets near the filesystem.
    if len(text) > 64 or not re.match(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$", text):
        raise CronError(f"'{text}' is not a timezone name. Use an IANA name such as Asia/Beirut.")
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"Unknown timezone '{text}'. Use an IANA name such as Asia/Beirut or UTC.")


def _as_zone(tz) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else zone(tz)


# ---------------------------------------------------------------------------
# When it runs
# ---------------------------------------------------------------------------

def _day_matches(expression: CronExpression, day: date) -> bool:
    if day.month not in expression.months:
        return False
    dom = day.day in expression.days
    dow = (day.weekday() + 1) % 7 in expression.weekdays
    # Vixie, exactly: either field written with a star makes it AND (a bare
    # star is the full set, so that reduces to the other field); both written
    # without one makes it OR.
    if expression.dom_star or expression.dow_star:
        return dom and dow
    return dom or dow


def _wall_times(expression: CronExpression, start: datetime) -> Iterator[datetime]:
    """Naive local times the expression names, ascending, from ``start`` on.

    Walks months, then days, then the hour and minute sets, so a yearly
    schedule is a dozen steps rather than half a million minutes.
    """
    hours = sorted(expression.hours)
    minutes = sorted(expression.minutes)
    for year in range(start.year, start.year + _SEARCH_YEARS):
        for month in sorted(expression.months):
            if (year, month) < (start.year, start.month):
                continue
            for day_number in range(1, calendar.monthrange(year, month)[1] + 1):
                day = date(year, month, day_number)
                if day < start.date() or not _day_matches(expression, day):
                    continue
                for hour in hours:
                    for minute in minutes:
                        wall = datetime(year, month, day_number, hour, minute)
                        if wall >= start:
                            yield wall


def _local(instant: datetime, tz: ZoneInfo) -> datetime:
    return instant.astimezone(tz).replace(tzinfo=None)


def _instants(wall: datetime, tz: ZoneInfo, interval: bool) -> List[datetime]:
    """The UTC instant(s) a wall-clock time stands for in ``tz``.

    Normally one. Two when the clocks go back and the time happens twice;
    none, or the end of the gap, when the clocks go forward over it — which
    of those depends on ``interval`` (see the module docstring).
    """
    first = wall.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    second = wall.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    if first == second:
        return [first]
    if _local(first, tz) == wall and _local(second, tz) == wall:
        # Ambiguous. fold=0 is the earlier of the two.
        return [first, second] if interval else [first]
    # A gap. Per PEP 495, fold=1 lands before the jump and fold=0 after it;
    # the jump itself is the first second whose local time is past ``wall``.
    if interval:
        return []
    low, high = min(first, second), max(first, second)
    while (high - low) > timedelta(seconds=1):
        middle = low + (high - low) / 2
        if _local(middle, tz) > wall:
            high = middle
        else:
            low = middle
    return [high.replace(microsecond=0)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # Naive values in this codebase are UTC (SQLite hands them back that way).
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def next_after(expression: CronExpression, after: datetime, tz) -> Optional[datetime]:
    """The first run strictly after ``after``, as an aware UTC datetime.

    ``None`` only if nothing matches within the search horizon, which
    :func:`parse` already makes impossible for a valid expression.
    """
    tz = _as_zone(tz)
    after_utc = _utc(after)
    local_now = _local(after_utc, tz)
    # Start a little in the past of the local clock: across a fall-back the
    # instant just after `after` can carry an EARLIER wall-clock time.
    start = (local_now - _FOLD_WINDOW).replace(second=0, microsecond=0)

    best: Optional[datetime] = None
    best_wall: Optional[datetime] = None
    for wall in _wall_times(expression, start):
        # Once well past the first hit, nothing later can map to an earlier
        # instant — the window is the most a transition can reorder.
        if best_wall is not None and wall > best_wall + _FOLD_WINDOW:
            break
        for instant in _instants(wall, tz, expression.hour_star):
            if instant > after_utc and (best is None or instant < best):
                best, best_wall = instant, best_wall or wall
    return best


def upcoming(expression: CronExpression, after: datetime, tz, count: int = 5) -> List[datetime]:
    """The next ``count`` runs after ``after``, ascending."""
    runs: List[datetime] = []
    cursor = after
    for _ in range(max(0, count)):
        nxt = next_after(expression, cursor, tz)
        if nxt is None:
            break
        runs.append(nxt)
        cursor = nxt
    return runs


def min_interval_minutes(expression: CronExpression) -> int:
    """The shortest gap, in minutes, between two runs on an ordinary day.

    Used to refuse a schedule that would queue a build every minute. Computed
    on the wall clock of a day with no transition, which is the day that
    matters: a DST night only ever lengthens or merges intervals.
    """
    marks = sorted(hour * 60 + minute for hour in expression.hours for minute in expression.minutes)
    if len(marks) == 1:
        return 24 * 60
    gaps = [b - a for a, b in zip(marks, marks[1:])]
    gaps.append(marks[0] + 24 * 60 - marks[-1])
    return min(gaps)


# ---------------------------------------------------------------------------
# In words
# ---------------------------------------------------------------------------

_DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_MONTH_FULL = (
    "", "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def _join(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _runs(values: List[int]) -> List[Tuple[int, int]]:
    """Consecutive stretches: [1,2,3,5] -> [(1,3),(5,5)]."""
    out: List[Tuple[int, int]] = []
    for value in values:
        if out and value == out[-1][1] + 1:
            out[-1] = (out[-1][0], value)
        else:
            out.append((value, value))
    return out


def _step_of(values: List[int], low: int, high: int) -> Optional[int]:
    """n when values is exactly low, low+n, ... up to high (a ``*/n``)."""
    if len(values) < 2 or values[0] != low:
        return None
    step = values[1] - values[0]
    return step if values == list(range(low, high + 1, step)) else None


def _clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _weekday_phrase(weekdays: List[int]) -> str:
    """'weekdays', 'weekends', 'Sundays', 'Monday to Thursday', ..."""
    days = set(weekdays)
    if days == {1, 2, 3, 4, 5}:
        return "weekdays"
    if days == {0, 6}:
        return "weekends"
    # Monday-first, which is how a week reads in a sentence.
    ordered = sorted(days, key=lambda day: (day + 6) % 7)
    stretches = _runs([(day + 6) % 7 for day in ordered])
    if len(stretches) == 1 and stretches[0][1] - stretches[0][0] >= 2:
        first, last = stretches[0]
        return f"{_DAY_NAMES[(first + 1) % 7]} to {_DAY_NAMES[(last + 1) % 7]}"
    return _join([f"{_DAY_NAMES[day]}s" for day in ordered])


def _days_phrase(days: List[int]) -> str:
    stretches = _runs(days)
    parts = [
        _ordinal(a) if a == b else f"{_ordinal(a)} to {_ordinal(b)}"
        for a, b in stretches
    ]
    return f"the {_join(parts)}"


def _months_phrase(months: List[int]) -> str:
    return _join([_MONTH_FULL[month] for month in months])


def _time_phrase(expression: CronExpression) -> Tuple[str, bool]:
    """How often within a day, and whether that reads as an appointment.

    An appointment ("at 02:00") takes the day in front of it — "Weekdays at
    08:30". An interval ("Every 15 minutes") takes it behind — "Every 15
    minutes on weekdays".
    """
    minutes = sorted(expression.minutes)
    hours = sorted(expression.hours)
    every_hour = len(hours) == 24
    minute_step = _step_of(minutes, 0, 59)

    if every_hour:
        if len(minutes) == 60:
            return "Every minute", False
        if minute_step:
            return f"Every {minute_step} minutes", False
        if minutes == [0]:
            return "Every hour, on the hour", False
        if len(minutes) == 1:
            return f"Every hour at {minutes[0]:02d} minutes past", False
        return f"Every hour at {_join([f':{m:02d}' for m in minutes])}", False

    hour_step = _step_of(hours, 0, 23)
    if hour_step and len(minutes) == 1:
        tail = "on the hour" if minutes[0] == 0 else f"at {minutes[0]:02d} minutes past"
        return f"Every {hour_step} hours, {tail}", False

    if len(minutes) == 1:
        stretches = _runs(hours)
        if len(hours) <= 6 or len(stretches) > 1:
            return f"at {_join([_clock(hour, minutes[0]) for hour in hours])}", True
        first, last = stretches[0]
        return (
            f"Every hour from {_clock(first, minutes[0])} to {_clock(last, minutes[0])}",
            False,
        )

    # Several minutes inside restricted hours: an interval inside a window.
    stretches = _runs(hours)
    if len(stretches) == 1:
        window = f"between {_clock(stretches[0][0], 0)} and {_clock(stretches[0][1], 59)}"
    else:
        window = f"during hours {_join([str(hour) for hour in hours])}"
    if len(minutes) == 60:
        return f"Every minute {window}", False
    if minute_step:
        return f"Every {minute_step} minutes {window}", False
    return f"At {_join([f':{m:02d}' for m in minutes])} past each hour {window}", False


def _day_phrase(expression: CronExpression) -> str:
    """Which days, or '' for every day."""
    months = sorted(expression.months)
    all_months = len(months) == 12
    in_months = "" if all_months else f" in {_months_phrase(months)}"

    days = sorted(expression.days)
    weekdays = sorted(expression.weekdays)
    every_date = len(days) == 31
    every_weekday = len(weekdays) == 7

    if every_date and every_weekday:
        return "" if all_months else f"every day{in_months}"

    weekday_part = f"on {_weekday_phrase(weekdays)}"
    if every_date:
        return f"{weekday_part}{in_months}"

    of = "of every month" if all_months else f"of {_months_phrase(months)}"
    step = _step_of(days, 1, 31)
    dates = f"every {_ordinal(step)} day" if step else _days_phrase(days)
    day_part = f"on {dates} {of}"
    if every_weekday:
        return day_part
    if expression.dom_star or expression.dow_star:
        # AND: a rare shape, said plainly rather than elegantly.
        return f"{day_part} ({_weekday_phrase(weekdays)} only)"
    return f"{day_part}, or {weekday_part}{in_months}"


def describe(expression: CronExpression) -> str:
    """Plain English: "Every day at 02:00", "Weekdays at 08:30", ...

    Times are wall-clock times in the schedule's own zone; the caller names
    the zone, because the same words mean a different instant in each.
    """
    time_text, appointment = _time_phrase(expression)
    day_text = _day_phrase(expression)

    if appointment:
        if not day_text:
            return f"Every day {time_text}"
        if day_text.startswith("on ") and len(expression.days) == 31:
            # Weekdays only: "Weekdays at 08:30", "Sundays at 03:00" — the
            # phrase people actually say, without the "On".
            rest = day_text[len("on "):]
            return f"{rest[0].upper()}{rest[1:]} {time_text}"
        # "…, or on Mondays, at 09:00" — without the comma the time reads as
        # belonging to the Mondays only.
        joiner = ", " if ", or " in day_text else " "
        return f"{day_text[0].upper()}{day_text[1:]}{joiner}{time_text}"

    if not day_text:
        return time_text
    return f"{time_text} {day_text}"
