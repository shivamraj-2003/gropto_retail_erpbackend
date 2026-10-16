"""The arithmetic behind a transfer that does not arrive exactly as it left.

Kept free of the database so every case can be tested on its own. A line has:
  dispatched  what the sender sent
  received    good units counted in at the destination (all deliveries added up)
  damaged     units that arrived but are not sellable (all deliveries added up)

`open` is what is still unaccounted for: positive = missing (sent 5, 4 arrived -> 1 open), negative = more
arrived than was sent. A line is settled by an action chosen by a person (see `plan_resolution`).
"""

from dataclasses import dataclass

EPS = 0.0005

#: Missing goods: they were lost, or they never left / came back to the sender.
SHORT_ACTIONS = {"write_off", "return_to_source"}
#: More arrived than the sender recorded: the sender's figure was wrong, or the extra goes back.
EXCESS_ACTIONS = {"reduce_sender", "send_back"}


@dataclass(frozen=True)
class LineQty:
    dispatched: float
    received: float = 0.0
    damaged: float = 0.0


def arrived(line: LineQty) -> float:
    return line.received + line.damaged


def open_qty(line: LineQty) -> float:
    return round(line.dispatched - arrived(line), 3)


def needs_attention(line: LineQty) -> bool:
    """Short, extra, or damaged: somebody must look at it before the transfer is closed."""
    return abs(open_qty(line)) > EPS or line.damaged > EPS


def nothing_missing(lines: list[LineQty]) -> bool:
    return all(open_qty(line) <= EPS for line in lines)


def any_arrived(lines: list[LineQty]) -> bool:
    return any(arrived(line) > EPS for line in lines)


def closed_status(lines: list[LineQty]) -> str:
    """Where a transfer ends up once nothing more is expected: exact = received, anything else = needs checking."""
    return "discrepancy" if any(needs_attention(line) for line in lines) else "received"


def status_after_delivery(lines: list[LineQty], *, close: bool) -> str:
    """Status after goods were counted in. `close` = the person says nothing more is coming."""
    if close or nothing_missing(lines):
        return closed_status(lines)
    return "partially_received" if any_arrived(lines) else "dispatched"


@dataclass(frozen=True)
class Step:
    """One stock effect of settling a line. place: 'source' or 'dest'; delta is the change to that place's stock."""

    item_id: str
    action: str
    qty: float
    place: str | None
    delta: float


def plan_resolution(lines: dict[str, LineQty], actions: dict[str, str]) -> tuple[list[Step], list[str]]:
    """What closing a transfer with a mismatch does to stock, and the problems with the chosen actions.

    Damaged units need no action (they never entered sellable stock; they are recorded as a loss).
    A missing or extra line must be given one of the actions above.
    """
    steps: list[Step] = []
    errors: list[str] = []
    for item_id, line in lines.items():
        gap = open_qty(line)
        if abs(gap) <= EPS:
            continue
        action = actions.get(item_id)
        if gap > 0:
            if action not in SHORT_ACTIONS:
                errors.append(item_id)
                continue
            if action == "return_to_source":
                steps.append(Step(item_id, action, gap, "source", gap))
            else:
                steps.append(Step(item_id, action, gap, None, 0.0))
        else:
            extra = -gap
            if action not in EXCESS_ACTIONS:
                errors.append(item_id)
                continue
            steps.append(Step(item_id, action, extra, "source" if action == "reduce_sender" else "dest", -extra))
    return steps, errors
