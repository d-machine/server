"""
SQLAlchemy Core Table definitions, matching app/db_init.py's raw DDL exactly.

These are plain Core Table objects (not ORM declarative models) — used to
build select()/insert()/update()/delete() statements executed via
arthdesk_db.Database. Kept separate from app/models/*.py's (currently
dead/unused, drifted) declarative classes.

Add tables here incrementally as routers are converted from raw text() SQL
to Core statements — this file is not meant to mirror 100% of db_init.py on
day one.
"""
from sqlalchemy import MetaData, Table, Column, Integer, Text

metadata = MetaData()

instrument_types = Table(
    "instrument_types", metadata,
    Column("instrument_type_id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("asset_class", Text, nullable=False),
    Column("tax_category", Text, nullable=False),
)
