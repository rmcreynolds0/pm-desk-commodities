"""Smoke + unit tests. Run with `pytest` from the project root."""
import numpy as np
import pandas as pd

from storage_stress.data import connectivity as C
from storage_stress.signal import build_dsi, convex_stress, zscore
from storage_stress.agents import agent_zero, agent_dsi, simulate, bootstrap_sharpe_ci


def test_synthetic_storage_shape():
    df = C.eia_storage("salt_south_central", synthetic=True)
    assert {"level_bcf", "net_flow_bcf"}.issubset(df.columns)
    assert len(df) > 100
    assert df["level_bcf"].min() > 0


def test_convex_transform_is_convex_and_signed():
    assert convex_stress(pd.Series([0.3])).iloc[0] < convex_stress(pd.Series([0.9])).iloc[0]
    assert convex_stress(pd.Series([-0.9])).iloc[0] < 0


def test_dsi_pipeline_runs():
    salt = C.eia_storage("salt_south_central", synthetic=True)
    waha = C.regional_basis("waha").iloc[:, 0]
    dom = C.regional_basis("domsouth").iloc[:, 0]
    dsi = build_dsi(salt, 90.0, waha, dom, k=4.0)
    assert {"dsi_raw", "dsi_clean"}.issubset(dsi.columns)
    assert len(dsi) > 50


def test_zscore_centers():
    s = pd.Series(np.random.default_rng(0).normal(0, 1, 300))
    z = zscore(s, window=156)
    assert abs(z.dropna().mean()) < 0.5


def test_agents_share_execution_and_backtest_runs():
    salt = C.eia_storage("salt_south_central", synthetic=True)
    c1 = C.eia_henryhub_futures(1, synthetic=True)
    c2 = C.eia_henryhub_futures(2, synthetic=True)
    waha = C.regional_basis("waha").iloc[:, 0]
    dom = C.regional_basis("domsouth").iloc[:, 0]
    spread = (c1.iloc[:, 0] - c2.iloc[:, 0]).resample("W-FRI").last()
    spread = spread.reindex(salt.index).interpolate().dropna()
    spread_ret = spread.diff()
    spread_vol = spread_ret.rolling(30).std()

    dsi = build_dsi(salt, 90.0, waha, dom, k=4.0)
    idx = dsi.index
    s0 = agent_zero(idx, seed=1, trade_every=2)
    sd = agent_dsi(dsi["dsi_clean"], spread_ret, entry_z=1.5)

    for sig in (s0, sd):
        res = simulate(spread, sig, spread_vol.reindex(idx))
        assert "equity" in res.columns
        ret = res["equity"].pct_change().dropna()
        _, (lo, hi) = bootstrap_sharpe_ci(ret, n_boot=200)
        assert lo <= hi
