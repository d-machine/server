# MCX Bhavcopy — Mobile Downloader App

## Problem
GCP datacenter IPs are blocked by Akamai WAF on mcxindia.com.
A mobile app on a residential/cellular IP bypasses the block.

## Approach
1. Mobile app fetches MCX bhavcopy from the phone (unblocked IP)
2. Uploads the CSV to a new server endpoint → lands in `/inbox`
3. Server's existing `sync-inbox` flow registers and uploads to GCS

---

## Server Changes

### New endpoint: `POST /admin/bhavcopy/upload`
- Accepts `multipart/form-data` with fields:
  - `file`: the CSV file bytes
  - `date`: trade date `YYYY-MM-DD` (used to name the blob)
- Saves file to `INBOX_DIR` as `BhavCopy_MCX_0_0_0_{YYYYMMDD}_F_0000.csv`
- Auto-calls `sync-inbox` logic for that file
- Returns `{ status, file_name, rows_synced }`
- Protected by `X-API-Key` header (simple shared secret via env var `UPLOAD_API_KEY`)

**File:** `app/routers/bhavcopy.py` — add new route + update `_INBOX_SOURCES` to include MCX pattern

---

## Mobile App (React Native / Expo)

### Stack
- Expo (managed workflow) — easiest cross-platform setup
- Single screen, no navigation needed

### Screen: MCX Downloader
```
┌─────────────────────────────────┐
│  MCX Bhavcopy Uploader          │
│                                 │
│  Server URL  [_______________]  │
│  API Key     [_______________]  │
│  Date        [ 2025-01-01   ]   │  ← date picker
│                                 │
│       [ Fetch & Upload ]        │
│                                 │
│  Status: ─────────────────────  │
│  • Fetching MCX page...         │
│  • Got CSV (1423 rows)          │
│  • Uploaded ✓                   │
└─────────────────────────────────┘
```

### App Logic (`App.tsx`)
1. GET `https://www.mcxindia.com/market-data/bhavcopy` → extract `__VIEWSTATE`
2. POST form with `__EVENTTARGET = ctl00$cph_InnerContainerRight$C001$lnkExpToCSV`
   and `txtDate_hid_val = YYYYMMDD` → receive CSV bytes
3. POST CSV to `{serverUrl}/admin/bhavcopy/upload` with `X-API-Key` header
4. Show status at each step

### Settings persistence
- `AsyncStorage` for server URL and API key (filled once, remembered)

---

## Implementation Order

| # | Task | Where |
|---|------|-------|
| 1 | Add `UPLOAD_API_KEY` env var + simple key-check middleware | server |
| 2 | Add MCX pattern to `_INBOX_SOURCES` | `app/routers/bhavcopy.py` |
| 3 | Add `POST /admin/bhavcopy/upload` endpoint | `app/routers/bhavcopy.py` |
| 4 | Scaffold Expo app (`npx create-expo-app mcx-uploader`) | mobile |
| 5 | Build `App.tsx` — settings form + MCX fetch + upload logic | mobile |
| 6 | Test on Android (MCX site works on mobile data) | — |

---

## Notes
- iOS may block HTTP (non-HTTPS) server URLs — use HTTPS or add `NSAllowsArbitraryLoads` in `Info.plist`
- MCX form POST requires the full `__VIEWSTATE` from the GET response; must be done in the same session
- The date picker should default to yesterday and skip weekends
- For batch backfill: add a date range loop in the app (fetch one day at a time with 2s delay)
