import json
from datetime import datetime, timezone
from pathlib import Path

from fxsignals.weekly import due, summary_message

SAT_0930 = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)   # Lisbon = UTC in November
SAT_1015 = datetime(2026, 11, 7, 10, 15, tzinfo=timezone.utc)
WED = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def test_due_only_saturday_from_ten_and_once_per_week(cfg):
    assert due(cfg, WED) is None
    assert due(cfg, SAT_0930) is None
    week = due(cfg, SAT_1015)
    assert week == "2026-W45"
    state = Path(cfg["paths"]["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "backtest_schedule.json").write_text(json.dumps({"week": week}))
    assert due(cfg, SAT_1015) is None  # already ran this week
    cfg["backtest_schedule"]["enabled"] = False
    (state / "backtest_schedule.json").unlink()
    assert due(cfg, SAT_1015) is None


def test_summary_names_better_rule_set_and_warns_on_small_samples():
    s = lambda n, e: {"trades": n, "win_rate_pct": 35.0, "expectancy_r": e, "total_r": n * e, "profit_factor": 1.2,
                      "max_drawdown_r": 6.0, "statistically_meaningful": n >= 100}
    msg = summary_message({"overall": {"strict": s(40, -0.05), "adaptive": s(140, 0.12)}})
    assert "adaptive (new)" in msg and "Fewer than 100" in msg and "Strict" in msg and "Adaptive" in msg
    msg = summary_message({"overall": {"strict": s(150, 0.2), "adaptive": s(160, 0.1)}})
    assert "strict (old)" in msg and "Fewer than 100" not in msg
