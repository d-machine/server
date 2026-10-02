"""
SQLAlchemy Core Table definitions used to build select()/insert()/update()/
delete() statements executed via arthdesk_db.Database. Kept separate from
app/models/*.py's (currently dead/unused, drifted) declarative classes.

Every instrument-related table (identity, detail, price) comes from
arthdesk_instruments — the shared instrument-identity package — rather than
being defined here, so the server and arthdesk-py's client can never drift
apart on these again the way bse_symbol/MF/instrument_type did.

The hub table's primary key is `id` here (not `instrument_id`) — this is a
direct consequence of importing the package's Table object as-is, not a
separate renaming decision. See arthdesk_instruments.instruments' docstring.
"""
from arthdesk_instruments import (
    asset_classes,
    tax_categories,
    instrument_types,
    instruments,
    instrument_equity,
    instrument_index,
    instrument_derivatives,
    instrument_mf,
    instrument_mcx,
    instrument_fixed_income,
    instrument_pending,
    latest_prices,
    equity_eod,
    fo_eod,
    mcx_eod,
    mf_nav,
)

__all__ = [
    "asset_classes",
    "tax_categories",
    "instrument_types",
    "instruments",
    "instrument_equity",
    "instrument_index",
    "instrument_derivatives",
    "instrument_mf",
    "instrument_mcx",
    "instrument_fixed_income",
    "instrument_pending",
    "latest_prices",
    "equity_eod",
    "fo_eod",
    "mcx_eod",
    "mf_nav",
]
