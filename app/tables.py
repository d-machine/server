"""
SQLAlchemy Core Table definitions used to build select()/insert()/update()/
delete() statements executed via arthdesk_db.Database. Kept separate from
app/models/*.py's (currently dead/unused, drifted) declarative classes.

asset_classes/tax_categories/instrument_types/instruments come from
arthdesk_instruments — the shared instrument-identity package — rather than
being defined here, so the server and arthdesk-py's client can never drift
apart on these again the way bse_symbol/MF/instrument_type did.

Add tables here incrementally as routers are converted from raw text() SQL
to Core statements — this file is not meant to mirror 100% of db_init.py on
day one.
"""
from arthdesk_instruments import asset_classes, tax_categories, instrument_types, make_instruments_table

instruments = make_instruments_table("instrument_id")

__all__ = ["asset_classes", "tax_categories", "instrument_types", "instruments"]
