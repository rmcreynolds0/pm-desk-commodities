"""One-off: correct agent_zero's entry_px to the front-deferred convention.

The first live entry was recorded with IBKR's BAG combo price (+1.425), whose
sign convention is opposite to the mark job's (front_close - deferred_close).
The true entry spread from the leg fills is 2.887 - 4.312 = -1.425. This patches
the single existing position/fill row so P&L is consistent going forward. The
engine bug itself is already fixed; this only repairs the one pre-fix row.
Safe to delete after running.
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")
from storage_stress.execution import books  # noqa: E402

con = books.connect(Path("data/live/books.db"))
con.execute("UPDATE positions SET entry_px = -1.425 WHERE agent = 'agent_zero'")
con.execute(
    "UPDATE fills SET price = -1.425, "
    "note = 'ibkr_combo_fill=1.425 (sign-corrected to front-deferred)' "
    "WHERE agent = 'agent_zero'")
con.commit()
print("patched agent_zero entry_px ->", books.get_position(con, "agent_zero")["entry_px"])
con.close()
