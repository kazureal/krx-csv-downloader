HRF TRACK 02 — OOS-A BATCH INPUT
Date: 2026-08-28

PURPOSE
- Existing stable Batch app can collect the frozen 87 OOS-A stocks without another app update.
- Upload this ZIP in Development Batch OHLCV mode exactly as if it were a Universe ZIP.

IMPORTANT
- The column `development_selection_order` in this compatibility file means OOS-A collection order 1..87.
- The original universe global order is preserved as `source_development_selection_order`.
- OOS-A selection itself is already frozen.
- Future response/MFE/MAE outcomes remain sealed.

RECOMMENDED COLLECTION WITH CURRENT APP
- Batch size 20, Batch 1 -> OOS order 1-20
- Batch size 20, Batch 2 -> OOS order 21-40
- Batch size 20, Batch 3 -> OOS order 41-60
- Batch size 20, Batch 4 -> OOS order 61-80
- Batch size 20, Batch 5 -> OOS order 81-87
- Same OHLCV period: 2010-01-04 through 2026-08-27
- KRX login form required.
