"""Ken French Data Library loader: parsing the monthly block.

Offline — feeds a synthetic French CSV to the parser rather than hitting the
network, so the monthly/annual-block split is checked deterministically.
"""

import pandas as pd

from french_data import parse_monthly

SAMPLE_FRENCH_CSV = """This file was created using the 202606 CRSP database.
Some more prose about the T-bill rate.

,Mkt-RF,SMB,HML,RF
192607,   2.89,  -2.55,  -2.39,   0.22
192608,   2.64,  -1.14,   3.81,   0.25
202605,   1.10,   0.40,  -0.30,   0.35

  Annual Factors: January-December
1927,  29.47,  -2.44,  -4.34,   3.12
1928,  35.39,   4.36,  -6.29,   3.56

Copyright 2026 Kenneth R. French
"""


def test_parse_monthly_takes_only_the_monthly_block():
    df = parse_monthly(SAMPLE_FRENCH_CSV)
    assert list(df.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    # Six-digit YYYYMM rows only; the four-digit annual rows are excluded.
    assert len(df) == 3
    assert str(df.index[0]) == "1926-07"
    assert df.loc[pd.Period("2026-05", "M"), "Mkt-RF"] == 1.10
    # Sorted ascending regardless of file order.
    assert df.index.is_monotonic_increasing
