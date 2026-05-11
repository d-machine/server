from app.cron.bhavcopy.sync import nse_eq, bse_eq, nse_fo, bse_fo, mcx, amfi

PARSERS = {
    "NSE_EQ":   nse_eq,
    "BSE_EQ":   bse_eq,
    "NSE_FO":   nse_fo,
    "BSE_FO":   bse_fo,
    "MCX":      mcx,
    "AMFI_NAV": amfi,
}
