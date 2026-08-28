# v1.0.14A — TRACK02 DATA ADDENDUM

Purpose: close HRF Track02 pre-unblind data blockers without changing zone, matching, response, or inference rules.

Changes only in Development Batch collection:
- Preserve official KRX `ACC_TRDVAL` as `trading_value`.
- Preserve `MKTCAP` as `market_cap`.
- Preserve `LIST_SHRS` as `listed_shares`.
- Preserve `FLUC_RT`, `CMPPREVDD_PRC`, `FLUC_TP_CD` for corporate-action/reference-price audit.
- Mark valid_session using v1.1 domain rule: Volume>0, finite OHLC, OHLC>0.
- Keep same KRX MDCSTAT01701 endpoint, `adjStkPrc=1`, login form, per-stock isolated sessions/retries, max batch size 20.
- No response outcome, H15 response, MFE, MAE, Hotelling statistic, or OOS result is calculated.

This build is a data-collection addendum. It does not change research estimands or inference.
