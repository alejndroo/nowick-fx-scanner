"""Adaptive plan — bounded, explainable adjustments only, recomputed every
scan cycle (~60s).

Reads the trade journal's rolling stats and decides, right now:
  - what risk % to use for the next trade (drifts within config's approved
    RISK_PCT_MIN..RISK_PCT_MAX band based on the recent win/loss streak —
    NEVER outside that band, which is what the user explicitly approved)
  - which pairs (if any) to pause for the rest of today, if they have
    enough trades to trust the number and are meaningfully underperforming
  - whether today's realized loss has crossed the daily circuit breaker,
    which stops all new entries until the next UTC day

Recomputed continuously (not once a day) so it reacts WITHIN the same day —
this exists specifically because of an incident where a day's profit was
given back within a few hours; a once-a-day plan would have done nothing
about that.

Nothing here touches engine.py's signal-detection logic. This only decides
how much to risk and which pairs to skip — the definition of a "signal"
never changes.
"""
from datetime import datetime, timezone

import config
import journal as journal_mod

MIN_TRADES_FOR_PAIR_PAUSE = 4      # don't pause a pair off 1-2 unlucky trades — that's noise, not signal
PAIR_PAUSE_WIN_RATE_GAP = 0.25     # pause a pair if its win rate is this many points below the overall average
DAILY_LOSS_LIMIT_PCT = 0.30        # stop new entries for the day once realized loss crosses this fraction of the day's starting balance
STREAK_RISK_STEP = 0.01            # how much each net consecutive win/loss nudges risk toward the band's edge


def compute_plan(journal_data: dict, day_start_balance: float, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = journal_mod.rolling_stats(journal_data)
    today_pnl = journal_mod.today_realized_pnl(journal_data, now)
    reasons = []

    # ---- risk %: drift within the approved band based on recent streak ----
    mid = (config.RISK_PCT_MIN + config.RISK_PCT_MAX) / 2
    streak = stats["current_streak"]
    # Losing streak (streak < 0) drifts risk DOWN toward RISK_PCT_MIN;
    # winning streak (streak > 0) drifts risk UP toward RISK_PCT_MAX.
    risk_pct = mid + (streak * STREAK_RISK_STEP)
    risk_pct = max(config.RISK_PCT_MIN, min(config.RISK_PCT_MAX, risk_pct))
    if streak <= -2:
        reasons.append(f"{-streak} losses in a row -> risk drifted toward the low end ({risk_pct * 100:.1f}%)")
    elif streak >= 2:
        reasons.append(f"{streak} wins in a row -> risk drifted toward the high end ({risk_pct * 100:.1f}%)")
    else:
        reasons.append(f"no strong recent streak -> risk near the middle of the band ({risk_pct * 100:.1f}%)")

    # ---- pairs to pause: meaningfully worse than the overall average, with enough samples to trust it ----
    overall_wr = stats["overall_win_rate"]
    paused_pairs = []
    if overall_wr is not None:
        for pair, pstats in stats["by_pair"].items():
            if pstats["n"] < MIN_TRADES_FOR_PAIR_PAUSE or pstats["win_rate"] is None:
                continue
            if pstats["win_rate"] < overall_wr - PAIR_PAUSE_WIN_RATE_GAP:
                paused_pairs.append(pair)
                reasons.append(f"{pair} paused: {pstats['win_rate'] * 100:.0f}% win rate over {pstats['n']} trades vs {overall_wr * 100:.0f}% overall")

    # ---- daily circuit breaker ----
    daily_loss_limit_hit = False
    if day_start_balance > 0 and today_pnl < 0:
        loss_fraction = -today_pnl / day_start_balance
        if loss_fraction >= DAILY_LOSS_LIMIT_PCT:
            daily_loss_limit_hit = True
            reasons.append(
                f"daily loss limit hit: realized {today_pnl:.2f} ({loss_fraction * 100:.0f}% of day-start balance "
                f"{day_start_balance:.2f}) -> no new entries until the next UTC day"
            )

    return {
        "risk_pct": round(risk_pct, 4),
        "paused_pairs": paused_pairs,
        "daily_loss_limit_hit": daily_loss_limit_hit,
        "today_realized_pnl": today_pnl,
        "reasons": reasons,
        "stats_snapshot": {
            "n_closed_14d": stats["n_closed"],
            "overall_win_rate": stats["overall_win_rate"],
            "current_streak": streak,
            "total_pnl_14d": stats["total_pnl"],
        },
        "computed_at": now.isoformat(timespec="seconds"),
    }
