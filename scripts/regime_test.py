#!/usr/bin/env python3
"""
regime_test.py — THE WEEK-1 GATE that was never run.
====================================================

docs/strategy.md, Key assumption 1:

    "Post-2016 LNG export growth tightened the deliverability system; calendar
     spreads have NOT fully repriced this regime. (Week-1 statistical test; if
     it fails, the project pauses for rescoping.)"

SPRINT_PLANNER week 1: "do post-2016 NG spreads behave differently from
pre-2016? Test run + written verdict. If it fails, pause and rescope."

This is the project's own pause-gate, and every later stage is conditional on
it. It was skipped. It is run here.

WHY IT IS ONLY POSSIBLE NOW
---------------------------
The test needs a long spread history straddling 2016. EIA's futures feed gave
only contracts 1-4 and died in 2024; it could not build the seasonal spread at
all. The Datastream contract data (tr_ds_fut, 577 NG contracts) reaches back to
1990, so the pre/post comparison is finally constructible.

WHAT IS TESTED
--------------
The claim has two parts, and they are tested separately because they can fail
independently:

  (A) REGIME CHANGE: do post-2016 spreads behave differently at all?
      -> level, volatility, seasonal amplitude, mean-reversion speed.
      If nothing changed, the premise that LNG tightened the system is not
      visible in prices, and the strategy has no reason to exist.

  (B) DIRECTION: is the change consistent with a TIGHTER system?
      A tighter deliverability system should show LARGER seasonal swings and
      HIGHER spread volatility -- scarcity shows up as violent seasonality.

Split date: 2016-02-24, the first Sabine Pass LNG export cargo.

Usage:
    python scripts/regime_test.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.data import connectivity as C  # noqa: E402
from storage_stress.data import futures as F  # noqa: E402

LNG_BREAK = pd.Timestamp("2016-02-24")
HISTORY_START = "1997-01-01"


def describe(s: pd.Series, label: str) -> dict:
    return {"period": label, "n": len(s), "mean": s.mean(), "sd": s.std(),
            "min": s.min(), "max": s.max()}


def seasonal_amplitude(s: pd.Series) -> float:
    """Spread of the month-of-year means — how strong the seasonal pattern is.

    A tighter deliverability system should price winter vs shoulder months
    more aggressively, i.e. a LARGER amplitude.
    """
    return float(s.groupby(s.index.month).mean().std())


def ar1(s: pd.Series) -> float:
    """Lag-1 autocorrelation of weekly changes — mean-reversion speed."""
    d = s.diff().dropna()
    return float(d.autocorr(lag=1))


def main() -> None:
    with open(ROOT / "config" / "settings.yaml") as f:
        settings = yaml.safe_load(f)

    print("=" * 76)
    print("WEEK-1 REGIME TEST — did post-2016 NG spreads change behaviour?")
    print("=" * 76)
    print(f"split at {LNG_BREAK.date()} (first Sabine Pass LNG export cargo)\n")

    con = C._r2_duckdb()
    try:
        daily = F.ng_spread(con, "seasonal", start=HISTORY_START)
    finally:
        con.close()

    spread = daily["spread"].resample("W-FRI").last().dropna()
    roll_w = (daily["roll"].resample("W-FRI").max()
              .reindex(spread.index).fillna(False).astype(bool))
    chg = spread.diff().mask(roll_w, np.nan).dropna()

    pre, post = spread[spread.index < LNG_BREAK], spread[spread.index >= LNG_BREAK]
    pre_c, post_c = chg[chg.index < LNG_BREAK], chg[chg.index >= LNG_BREAK]

    print(f"data: {spread.index[0].date()} -> {spread.index[-1].date()}  "
          f"({len(spread)} weekly obs)")
    print(f"  pre-2016 : {len(pre)} weeks   post-2016: {len(post)} weeks\n")

    print(pd.DataFrame([describe(pre, "pre-2016"),
                        describe(post, "post-2016")]).to_string(index=False))

    results = []

    t, p = stats.ttest_ind(pre, post, equal_var=False)
    print(f"\n[1] LEVEL  (Welch t-test on spread level)")
    print(f"    pre mean {pre.mean():+.4f}   post mean {post.mean():+.4f}   "
          f"diff {post.mean()-pre.mean():+.4f}")
    print(f"    t={t:+.3f}  p={p:.4g}  -> {'DIFFERENT' if p < 0.05 else 'no difference'}")
    results.append(("level", p < 0.05, p))

    lev, p_lev = stats.levene(pre_c, post_c)
    print(f"\n[2] VOLATILITY  (Levene test on weekly changes)")
    print(f"    pre sd {pre_c.std():.4f}   post sd {post_c.std():.4f}   "
          f"ratio {post_c.std()/pre_c.std():.2f}x")
    print(f"    W={lev:.3f}  p={p_lev:.4g}  -> "
          f"{'DIFFERENT' if p_lev < 0.05 else 'no difference'}")
    results.append(("volatility", p_lev < 0.05, p_lev))

    amp_pre, amp_post = seasonal_amplitude(pre), seasonal_amplitude(post)
    print(f"\n[3] SEASONAL AMPLITUDE  (sd of month-of-year means)")
    print(f"    pre {amp_pre:.4f}   post {amp_post:.4f}   "
          f"ratio {amp_post/amp_pre:.2f}x")
    print(f"    -> {'LARGER post-2016 (consistent with tighter system)' if amp_post > amp_pre else 'NOT larger post-2016'}")
    results.append(("seasonal_amplitude", amp_post > amp_pre * 1.2, None))

    print(f"\n[4] MEAN REVERSION  (lag-1 autocorr of weekly changes)")
    print(f"    pre {ar1(pre):+.4f}   post {ar1(post):+.4f}")

    print("\n" + "=" * 76)
    print("VERDICT")
    print("=" * 76)
    level_diff, vol_diff = results[0][1], results[1][1]
    amp_bigger = results[2][1]

    regime_changed = level_diff or vol_diff
    tighter = amp_bigger or (post_c.std() > pre_c.std() * 1.2)

    print(f"  (A) regime CHANGED at all?      {'YES' if regime_changed else 'NO'}")
    print(f"  (B) change implies TIGHTER sys? {'YES' if tighter else 'NO'}")

    if regime_changed and tighter:
        print("\n  GATE PASSES — assumption 1 is supported. Post-2016 spreads")
        print("  behave differently AND in the direction a tighter deliverability")
        print("  system predicts. The strategy's premise survives.")
    elif regime_changed and not tighter:
        print("\n  GATE AMBIGUOUS — spreads did change, but NOT in the direction")
        print("  a tighter system predicts. The premise as written is not")
        print("  supported; a rescope is warranted.")
    else:
        print("\n  GATE FAILS — post-2016 spreads are not statistically")
        print("  distinguishable from pre-2016. The premise that LNG growth")
        print("  tightened the system in a way prices reflect is NOT supported.")
        print("  Per docs/strategy.md, the project PAUSES for rescoping.")


if __name__ == "__main__":
    main()
