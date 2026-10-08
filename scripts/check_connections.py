"""Connectivity check for EIA and IBKR data sources."""
import sys
sys.path.append("src")

from dotenv import load_dotenv
load_dotenv()

from storage_stress.data.connectivity import (
    eia_storage, eia_henryhub_futures, IBKRConfig, ibkr_smoke_test
)

try:
    df = eia_storage("salt_south_central")
    print(f"[OK] EIA storage   {len(df)} rows | last={df.index[-1].date()} | level={df.iloc[-1,0]:.1f} Bcf | net_flow={df.iloc[-1,1]:+.1f} Bcf")
except Exception as e:
    print(f"[FAIL] EIA storage   {e}")

try:
    c1 = eia_henryhub_futures(1)
    
    c2 = eia_henryhub_futures(2)
    spread = c1.iloc[-1, 0] - c2.iloc[-1, 0]
    print(f"[OK] EIA futures   C1={c1.iloc[-1,0]:.3f} | C2={c2.iloc[-1,0]:.3f} | spread={spread:+.3f} $/MMBtu")
except Exception as e:
    print(f"[FAIL] EIA futures   {e}")

try:
    r = ibkr_smoke_test(IBKRConfig(port=4002))
    front  = r["front"]
    second = r["second"]
    nbars  = len(r["bars"])
    print(f"[OK] IBKR          front={front} | second={second} | {nbars} daily bars")
except Exception as e:
    print(f"[FAIL] IBKR          {e}")
