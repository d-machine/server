# AISPL Database Schema

This document describes `portfolio_server.db`'s schema as it actually is today.

**Equity, index, and derivatives identity/detail tables now come from the shared
[`arthdesk-instruments`](https://github.com/d-machine/arthdesk-instruments) package** (also used by
the desktop client), not from this repo's own DDL — so the server and client can't drift apart on
them the way `bse_symbol`/`MF`/`instrument_type` previously did. See
`app/tables.py` (imports) and `app/db_init.py` (`init_schema()` creates/seeds them).

**MF, MCX, fixed income, and all price tables (`latest_prices`, `equity_eod`, `fo_eod`, `mcx_eod`,
`mf_nav`) are also sourced from the same package now** (schema only — no shared DAO layer exists yet
for these, so the server's own query code for them is still being migrated; `app/routers/prices.py`
and the MF branch of `/instruments/search` are known-broken until that lands).

## Key differences from the old (pre-Phase-4) schema

- **Hub `instruments` table is minimal**: `id`, `instr_type_id`, `is_active` only — no `name`,
  `isin`, `created_at`/`updated_at`. Every descriptive field lives on the per-type detail table
  instead (`instrument_equity.nse_name`/`bse_name`, etc.).
- **All money is `REAL` rupees to 6dp, never `_paise` integers.** `instrument_equity.face_value`,
  `instrument_derivatives.strkp`, EOD price columns — all real-valued rupees.
- **Derivatives are exchange-scoped, not exchange-agnostic.** An NSE F&O contract and a BSE F&O
  contract are separate rows (`exchange` + `nse_bse_id`), even if they'd describe "the same"
  underlying/expiry/strike — they're genuinely different, independently-traded instruments. The old
  schema modeled one row with both `nse_fininstrmid`/`bse_fininstrmid` columns, which is why this
  doc used to show them that way.
- **No `exchanges`-table foreign keys from instrument tables.** `instrument_index` dropped its
  `exchange` column entirely (small, curated, hand-seeded table — symbols don't collide across
  exchanges); EOD tables use a plain `exchange` text column, not an FK (each row is already scoped
  to one exchange via its own primary key).

## Entity-Relationship Diagram

```mermaid
erDiagram
    %% ─── Reference tables (arthdesk_instruments) ───
    INSTRUMENT_TYPES ||--o{ INSTRUMENTS : "instr_type_id"
    TAX_CATEGORIES ||--o{ INSTRUMENT_TYPES : "tax_cat_id"

    %% ─── Instrument hub (arthdesk_instruments) ───
    INSTRUMENTS ||--o| INSTRUMENT_EQUITY : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_INDEX : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_MF : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_FIXED_INCOME : "has details"
    INSTRUMENTS ||--o{ INSTRUMENT_DERIVATIVES : "underlying (ul_instr_id)"

    %% ─── Prices (arthdesk_instruments) ───
    INSTRUMENTS ||--o{ EQUITY_EOD : "has EOD price"
    INSTRUMENTS ||--o{ FO_EOD : "has EOD price"
    INSTRUMENTS ||--o{ MCX_EOD : "has EOD price"
    INSTRUMENTS ||--o{ MF_NAV : "has NAV"
    INSTRUMENTS ||--o| LATEST_PRICES : "has current cache"

    %% ─── Table Definitions ───

    INSTRUMENT_TYPES {
        int id PK
        string name UK
        string asset_class
        string tax_cat
        int tax_cat_id FK
    }

    INSTRUMENTS {
        int id PK
        int instr_type_id FK
        int is_active
    }

    INSTRUMENT_EQUITY {
        int instr_id PK, FK
        string isin UK
        int nse_id UK
        string nse_name
        string nse_sym
        int bse_id UK
        string bse_name
        string bse_sym
        float face_value "REAL rupees"
        int is_active
        string created_at
        string updated_at
    }

    INSTRUMENT_MF {
        int instr_id PK, FK
        string isin UK
        string amfi_code UK
        string scheme_type
        string fund_house
        string plan
        string option
    }

    INSTRUMENT_DERIVATIVES {
        int instr_id PK, FK
        string exchange "NSE or BSE — part of the row's identity"
        int ul_instr_id FK "resolved underlying (equity OR index)"
        string nse_bse_name
        int nse_bse_id "unique per (exchange, nse_bse_id), not globally"
        string contract_type
        string expd
        float strkp "REAL rupees, 0 for futures"
        string opn_type "'-' for futures"
        int lot_size
    }

    LATEST_PRICES {
        int instr_id PK, FK
        string exchange
        string price_date
        float price "REAL rupees, single value — not OHLC"
        string last_synced_at
    }
```

## Still on the server's own (hand-written, pre-Phase-4) schema for operational data

Not part of `arthdesk-instruments` — unrelated to instrument identity, untouched:

- `exchanges` (code/name/country — reference data for sources other than the instrument tables now)
- `bhavcopy_files` (per-file sync tracking: status, rows_synced, error)
- `trading_calendar` (market holidays)
