"""
Scoring model.

Two tiers, matching the challenge spec:

  - HARD FAILURE : a correctness violation (double-booking, oversize aircraft,
    missing required jetbridge, international flight at a domestic gate, invalid
    output, ...). Raised as ``HardFailure`` and terminates the run; the final
    score is reported as FAILED.

  - SOFT PENALTY : a quality cost (reassignments, premium-gate misuse, walking
    distance, unassigned flights). Accumulated in a ``ScoreCard`` and summed
    into a single numeric score where LOWER IS BETTER, so solutions can be
    ranked.
"""

from .config import WEIGHTS


class HardFailure(Exception):
    """A game-ending rule violation."""

    def __init__(self, reason: str, time=None):
        self.reason = reason
        self.time = time
        super().__init__(reason)


class ScoreCard:
    def __init__(self, weights: dict = None):
        self.weights = dict(weights if weights is not None else WEIGHTS)
        self.items = []  # (category, points, detail, time)

    def add(self, category: str, detail: str = None, time=None, amount: float = 1):
        """Add a soft penalty. ``amount`` is multiplied by the category weight
        (count for discrete events, metres for walking distance, etc.)."""
        points = self.weights.get(category, 0) * amount
        self.items.append((category, points, detail, time))

    @property
    def total(self) -> float:
        return sum(points for _, points, _, _ in self.items)

    def breakdown(self) -> dict:
        """{category: {'count': n, 'points': p}} aggregated over all items."""
        agg = {}
        for category, points, _, _ in self.items:
            entry = agg.setdefault(category, {"count": 0, "points": 0.0})
            entry["count"] += 1
            entry["points"] += points
        return agg
