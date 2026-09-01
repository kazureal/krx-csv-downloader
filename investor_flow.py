"""KRX investor-flow acquisition and research-input helpers.

This module is intentionally separate from the frozen HRF Living Map core.
It collects investor-level trading quantity/value data and exposes a clearly
labelled positive-net-addition price proxy.  It does not alter HRF structures,
S1/NEXT selection, or promotion rules.
"""

from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, Mapping, MutableMapping, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd


FLOW_ENGINE_VERSION = "INVESTOR_FLOW_INPUT_v0.1.0_20260901"
FLOW_SOURCE = "KRX_VIA_PYKRX_INVESTOR_BY_DATE"

TICKER_ALIASES = {
    "펩트론": ("087010", "펩트론"),
    "삼성전자": ("005930", "삼성전자"),
    "디앤디파마텍": ("347850", "디앤디파마텍"),
    "코오롱티슈진": ("950160", "코오롱티슈진"),
    "SK하이닉스": ("000660", "SK하이닉스"),
    "에스케이하이닉스": ("000660", "SK하이닉스"),
    "하이닉스": ("000660", "SK하이닉스"),
}

INVESTOR_ALIASES = {
    "institution": ("기관합계", "기관", "institution", "institution_total"),
    "foreign": ("외국인합계", "외국인", "foreign", "foreign_total"),
    "individual": ("개인", "individual", "retail"),
    "other_corporation": ("기타법인", "other_corporation", "corporation"),
}

SIDES = {"buy": "매수", "sell": "매도", "net": None}
METRICS = ("qty", "value")
CORE_INVESTORS = ("institution", "foreign")
ALL_INVESTORS = ("institution", "foreign", "individual", "other_corporation")


def normalize_ticker_input(query: str) -> Tuple[str, str]:
    """Resolve a six-digit ticker or one of the app's frozen common aliases."""
    raw = (query or "").strip()
    if re.fullmatch(r"\d{6}", raw):
        return raw, raw

    key = re.sub(r"\s+", "", raw).upper()
    for alias, (ticker, name) in TICKER_ALIASES.items():
        if re.sub(r"\s+", "", alias).upper() == key:
            return ticker, name
    raise ValueError("종목명 별칭을 찾지 못했습니다. 6자리 종목코드를 입력해 주세요.")


def _chunk_ranges(start: date, end: date, max_days: int = 365) -> Iterable[Tuple[date, date]]:
    if start > end:
        raise ValueError("시작일이 종료일보다 늦습니다.")
    if max_days < 1:
        raise ValueError("max_days는 1 이상이어야 합니다.")
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _find_investor_column(columns: Iterable[object], investor: str) -> Optional[object]:
    normalized = {str(c).strip().lower(): c for c in columns}
    for alias in INVESTOR_ALIASES[investor]:
        hit = normalized.get(alias.lower())
        if hit is not None:
            return hit
    return None


def normalize_investor_frame(
    frame: pd.DataFrame,
    metric: str,
    side: str,
) -> pd.DataFrame:
    """Normalize one pykrx investor frame into date + canonical columns."""
    if metric not in METRICS:
        raise ValueError(f"지원하지 않는 metric: {metric}")
    if side not in SIDES:
        raise ValueError(f"지원하지 않는 side: {side}")
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date"])

    work = frame.reset_index().copy()
    date_col = next(
        (c for c in work.columns if str(c).strip().lower() in {"날짜", "date"}),
        work.columns[0],
    )
    out = pd.DataFrame({"date": pd.to_datetime(work[date_col], errors="coerce")})

    for investor in ALL_INVESTORS:
        source_col = _find_investor_column(work.columns, investor)
        if source_col is None:
            continue
        target = f"{investor}_{side}_{metric}"
        out[target] = pd.to_numeric(work[source_col], errors="coerce")

    return out.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last")


def assemble_flow_frames(frames: Mapping[Tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    """Merge the six buy/sell/net × qty/value frames and add audits."""
    merged: Optional[pd.DataFrame] = None
    for metric in METRICS:
        for side in SIDES:
            normalized = normalize_investor_frame(frames.get((metric, side)), metric, side)
            merged = normalized if merged is None else merged.merge(normalized, on="date", how="outer")

    if merged is None or merged.empty:
        return pd.DataFrame()

    expected = [
        f"{investor}_{side}_{metric}"
        for investor in ALL_INVESTORS
        for metric in METRICS
        for side in SIDES
    ]
    for column in expected:
        if column not in merged.columns:
            merged[column] = pd.NA
        merged[column] = pd.to_numeric(merged[column], errors="coerce").round().astype("Int64")

    identity_columns = []
    for investor in ALL_INVESTORS:
        for metric in METRICS:
            buy = f"{investor}_buy_{metric}"
            sell = f"{investor}_sell_{metric}"
            net = f"{investor}_net_{metric}"
            ok_col = f"{investor}_{metric}_identity_ok"
            complete = merged[[buy, sell, net]].notna().all(axis=1)
            merged[ok_col] = complete & ((merged[buy] - merged[sell]) == merged[net])
            identity_columns.append(ok_col)

    core_columns = [
        f"{investor}_{side}_{metric}"
        for investor in CORE_INVESTORS
        for metric in METRICS
        for side in SIDES
    ]
    merged["flow_input_complete"] = merged[core_columns].notna().all(axis=1)
    merged["flow_identity_ok"] = merged[identity_columns].all(axis=1)

    for investor in CORE_INVESTORS:
        qty = pd.to_numeric(merged[f"{investor}_buy_qty"], errors="coerce")
        value = pd.to_numeric(merged[f"{investor}_buy_value"], errors="coerce")
        price = value / qty.where(qty > 0)
        merged[f"{investor}_gross_buy_avg_price"] = price.astype("Float64")

    merged["source"] = FLOW_SOURCE
    merged["flow_engine_version"] = FLOW_ENGINE_VERSION
    merged = merged.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return merged


def add_finalization_flag(
    flow: pd.DataFrame,
    now_kst: Optional[datetime] = None,
) -> pd.DataFrame:
    """Mark today's KRX investor data provisional until 18:00 Asia/Seoul."""
    out = flow.copy()
    now = now_kst or datetime.now(ZoneInfo("Asia/Seoul"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Seoul"))
    dates = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["flow_finalized"] = (dates < now.date()) | ((dates == now.date()) & (now.hour >= 18))
    return out


def fetch_investor_flow_by_date(
    ticker: str,
    start: date,
    end: date,
    max_days: int = 365,
    sleep_seconds: float = 0.10,
) -> Tuple[pd.DataFrame, MutableMapping[str, object]]:
    """Fetch daily buy/sell/net quantity and value by investor group.

    pykrx is used as the adapter to KRX's investor-by-date dataset.  Empty or
    schema-broken calls are recorded; they are never silently treated as zero.
    """
    if not re.fullmatch(r"\d{6}", str(ticker)):
        raise ValueError("ticker는 6자리 종목코드여야 합니다.")
    try:
        import pykrx
        from pykrx import stock
    except Exception as ex:  # pragma: no cover - depends on deployment
        raise RuntimeError("pykrx를 불러오지 못했습니다. requirements.txt를 확인하세요.") from ex

    diagnostics: MutableMapping[str, object] = {
        "flow_engine_version": FLOW_ENGINE_VERSION,
        "source": FLOW_SOURCE,
        "pykrx_version": getattr(pykrx, "__version__", "unknown"),
        "ticker": ticker,
        "requested_start": str(start),
        "requested_end": str(end),
        "calls": [],
        "status": "",
    }

    chunk_frames = []
    for chunk_start, chunk_end in _chunk_ranges(start, end, max_days=max_days):
        raw_frames: Dict[Tuple[str, str], pd.DataFrame] = {}
        for metric in METRICS:
            fn = (
                stock.get_market_trading_volume_by_date
                if metric == "qty"
                else stock.get_market_trading_value_by_date
            )
            for side, krx_side in SIDES.items():
                rec = {
                    "start": str(chunk_start),
                    "end": str(chunk_end),
                    "metric": metric,
                    "side": side,
                    "rows": 0,
                    "status": "",
                    "error": "",
                }
                try:
                    args = (chunk_start.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d"), ticker)
                    frame = fn(*args) if krx_side is None else fn(*args, on=krx_side)
                    if frame is None or frame.empty:
                        rec["status"] = "EMPTY"
                        raw_frames[(metric, side)] = pd.DataFrame()
                    else:
                        rec["rows"] = int(len(frame))
                        normalized_probe = normalize_investor_frame(frame, metric, side)
                        required_probe = {
                            f"institution_{side}_{metric}",
                            f"foreign_{side}_{metric}",
                        }
                        if not required_probe.issubset(normalized_probe.columns):
                            rec["status"] = "SCHEMA_ERROR"
                            rec["error"] = f"columns={list(map(str, frame.columns))}"
                        else:
                            rec["status"] = "OK"
                        raw_frames[(metric, side)] = frame
                except Exception as ex:  # pragma: no cover - live adapter path
                    rec["status"] = "ERROR"
                    rec["error"] = f"{type(ex).__name__}: {ex}"
                    raw_frames[(metric, side)] = pd.DataFrame()
                diagnostics["calls"].append(rec)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        chunk = assemble_flow_frames(raw_frames)
        if not chunk.empty:
            chunk_frames.append(chunk)

    if not chunk_frames:
        diagnostics["status"] = "FLOW_EMPTY"
        return pd.DataFrame(), diagnostics

    out = pd.concat(chunk_frames, ignore_index=True)
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    out = add_finalization_flag(out)

    call_statuses = [str(rec["status"]) for rec in diagnostics["calls"]]
    all_calls_ok = bool(call_statuses) and all(status == "OK" for status in call_statuses)
    all_rows_complete = bool(out["flow_input_complete"].all())
    all_identities_ok = bool(out["flow_identity_ok"].all())
    diagnostics["status"] = (
        "FLOW_OK" if all_calls_ok and all_rows_complete and all_identities_ok else "FLOW_PARTIAL_OR_INVALID"
    )
    diagnostics["rows"] = int(len(out))
    diagnostics["actual_start"] = str(out["date"].min().date())
    diagnostics["actual_end"] = str(out["date"].max().date())
    diagnostics["incomplete_rows"] = int((~out["flow_input_complete"]).sum())
    diagnostics["identity_failure_rows"] = int((~out["flow_identity_ok"]).sum())
    diagnostics["provisional_rows"] = int((~out["flow_finalized"]).sum())
    return out, diagnostics


def _weighted_std(values: pd.Series, weights: pd.Series, mean: float) -> float:
    denominator = float(weights.sum())
    if denominator <= 0:
        return math.nan
    variance = float((weights * (values - mean) ** 2).sum()) / denominator
    return math.sqrt(max(variance, 0.0))


def positive_net_cost_proxy_summary(flow: pd.DataFrame) -> pd.DataFrame:
    """Summarize a deliberately limited positive-net-addition price proxy.

    Daily gross buy average price = buy value / buy quantity.
    Episode proxy = that daily price weighted only by positive net quantity.
    Negative-net days are not converted into a fictional inventory ledger.
    """
    rows = []
    for investor, label in (("institution", "기관합계"), ("foreign", "외국인합계")):
        required = [
            f"{investor}_buy_qty",
            f"{investor}_buy_value",
            f"{investor}_net_qty",
        ]
        if not all(column in flow.columns for column in required):
            continue

        buy_qty = pd.to_numeric(flow[required[0]], errors="coerce")
        buy_value = pd.to_numeric(flow[required[1]], errors="coerce")
        net_qty = pd.to_numeric(flow[required[2]], errors="coerce")
        daily_price = buy_value / buy_qty.where(buy_qty > 0)
        eligible = (
            flow.get("flow_input_complete", True)
            & flow.get("flow_identity_ok", True)
            & (net_qty > 0)
            & daily_price.notna()
        )
        prices = daily_price[eligible].astype(float)
        weights = net_qty[eligible].astype(float)
        total_weight = float(weights.sum())
        proxy = float((prices * weights).sum() / total_weight) if total_weight > 0 else math.nan
        dispersion = _weighted_std(prices, weights, proxy) if total_weight > 0 else math.nan

        rows.append(
            {
                "investor": label,
                "positive_net_days": int(eligible.sum()),
                "positive_net_qty": int(total_weight) if total_weight > 0 else 0,
                "selected_range_net_qty": int(net_qty.fillna(0).sum()),
                "positive_net_addition_price_proxy": proxy,
                "weighted_daily_price_dispersion": dispersion,
            }
        )
    return pd.DataFrame(rows)

