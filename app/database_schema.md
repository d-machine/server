# AISPL Database Schema (Normalized Design)

This document outlines the unified, normalized database design adopted for both the backend server and desktop application. 

It relies on an integer-based `instrument_id` as the primary universal identifier, fully normalizes lookup tables (`exchanges`, `instrument_types`), and standardizes financial fields (e.g., `strike_price_paise`).

## Entity-Relationship Diagram

```mermaid
erDiagram
    %% ─── Master & Lookup Tables ───
    EXCHANGES ||--o{ INSTRUMENTS : "primary_exchange_id"
    INSTRUMENT_TYPES ||--o{ INSTRUMENTS : "instrument_type_id"

    %% ─── Instrument Hub ───
    INSTRUMENTS ||--o| INSTRUMENT_EQUITY : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_INDEX : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_MF : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_FIXED_INCOME : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_DERIVATIVES : "has details"
    INSTRUMENTS ||--o| INSTRUMENT_MCX : "has details"

    %% ─── Prices & Data ───
    INSTRUMENTS ||--o{ EQUITY_EOD : "has EOD price"
    INSTRUMENTS ||--o{ FO_EOD : "has EOD price"
    INSTRUMENTS ||--o{ MCX_EOD : "has EOD price"
    INSTRUMENTS ||--o{ MF_NAV : "has NAV"
    INSTRUMENTS ||--o| LATEST_PRICES : "has current cache"

    %% ─── Table Definitions ───

    EXCHANGES {
        int exchange_id PK
        string code UK
        string name
        string country
    }

    INSTRUMENT_TYPES {
        int instrument_type_id PK
        string name UK
        string asset_class
        string tax_category
    }

    INSTRUMENTS {
        int instrument_id PK
        string name
        int instrument_type_id FK
        int primary_exchange_id FK
        int is_active
        string source
        datetime created_at
        datetime updated_at
    }

    INSTRUMENT_EQUITY {
        int instrument_id PK, FK
        string isin UK
        string nse_symbol
        int nse_fininstrmid
        string bse_code
        int face_value_paise
        string sector
        string industry
    }

    INSTRUMENT_MF {
        int instrument_id PK, FK
        string isin UK
        string amfi_code UK
        string scheme_type
        string fund_house
        string plan
        string option
    }

    INSTRUMENT_DERIVATIVES {
        int instrument_id PK, FK
        int underlying_instrument_id FK
        string underlying_symbol
        string expiry_date
        int lot_size
        int strike_price_paise "Nullable for Futures"
        string contract_type "e.g., FUTURES, CE, PE"
        int nse_fininstrmid UK
        int bse_fininstrmid UK
    }

    INSTRUMENT_MCX {
        int instrument_id PK, FK
        string mcx_symbol
        string instrument_type
        string expiry_date
        float lot_size
        string unit
        int strike_price_paise
        string option_type
    }

    LATEST_PRICES {
        int instrument_id PK, FK
        string price_date
        int close_price_paise
        datetime last_synced_at
    }
```
