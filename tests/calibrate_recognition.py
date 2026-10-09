"""Run with: .venv/bin/python tests/calibrate_recognition.py. Uses only temporary generated audio."""

import json
from pathlib import Path
import tempfile

from recognition_cases import measure_cases


def accepted(row: dict, votes: int, support: float, gap: float) -> str | None:
    candidates = row["candidates"]
    if not candidates:
        return None
    best = candidates[0]
    margin = best.support_ratio - (candidates[1].support_ratio if len(candidates) > 1 else 0)
    if best.aligned_votes >= votes and best.support_ratio >= support and margin >= gap:
        return best.track_id
    return None


def select_thresholds(rows: list[dict]) -> tuple[int, float, float] | None:
    """Maximize correct positives without false matches; never select an all-reject rule."""
    usable = [row for row in rows if not row.get("error") and row["candidates"]]
    votes = {1} | {row["candidates"][0].aligned_votes for row in usable}
    supports = {0.01} | {row["candidates"][0].support_ratio for row in usable}
    best = None
    for vote in votes:
        for support in supports:
            eligible = [row for row in usable if row["candidates"][0].aligned_votes >= vote
                        and row["candidates"][0].support_ratio >= support]
            margins = [(row, row["candidates"][0].support_ratio -
                        (row["candidates"][1].support_ratio if len(row["candidates"]) > 1 else 0))
                       for row in eligible]
            unsafe_gap = max([0.0] + [margin for row, margin in margins
                                      if row["candidates"][0].track_id != row["expected"]])
            safe = [margin for row, margin in margins if margin > unsafe_gap
                    and row["candidates"][0].track_id == row["expected"]]
            if safe:
                choice = (len(safe), support, vote, min(safe))
                if best is None or choice > best:
                    best = choice
    return (best[2], best[1], best[3]) if best is not None else None


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="magikazaam-calibration-") as temporary:
        root = Path(temporary)
        calibration = root / "calibration"
        held_out = root / "held-out"
        calibration.mkdir()
        held_out.mkdir()
        rows = measure_cases(calibration, 1200)
        thresholds = select_thresholds(rows)
        assert thresholds is not None, "Fixture scores did not separate; do not enable match results."
        vote, support, gap = thresholds
        validation = measure_cases(held_out, 2200)
        for label, cases in (("calibration", rows), ("held_out", validation)):
            outcomes = [accepted(row, vote, support, gap) for row in cases]
            print(json.dumps({"set": label, "votes": vote, "support": support, "gap": gap,
                              "correct_positives": sum(row["expected"] is not None and outcome == row["expected"] for row, outcome in zip(cases, outcomes)),
                              "positives": sum(row["expected"] is not None for row in cases),
                              "false_matches": sum(outcome is not None and outcome != row["expected"] for row, outcome in zip(cases, outcomes)),
                              "negatives": sum(row["expected"] is None for row in cases),
                              "missed": [row["case"] for row, outcome in zip(cases, outcomes) if row["expected"] is not None and outcome != row["expected"]]}), flush=True)
        assert all(accepted(row, vote, support, gap) in (None, row["expected"]) for row in validation)
        assert all(accepted(row, vote, support, gap) == row["expected"] for row in validation if row["case"].endswith("/clean"))


if __name__ == "__main__":
    main()
