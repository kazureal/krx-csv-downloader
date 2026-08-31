from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import math
import numpy as np
import pandas as pd

HRF_ENGINE_VERSION = "HRF Living Map v1.0 / CP71 implementation build 0.1"
B2_PROXY_LOW = 0.65
B2_PROXY_HIGH = 1.35
EPISODE_TOUCH_GAP = 5  # valid-session index gap <= 5 stays in the same encounter episode

COHERENT_ROLE_PAIRS = {
    ("B>A", "A>A"): "ROLE_BREAKOUT_SUPPORT",
    ("A>A", "A>B"): "ROLE_SUPPORT_BREAKDOWN",
    ("A>B", "B>B"): "ROLE_BREAKDOWN_RESISTANCE",
    ("B>B", "B>A"): "ROLE_RESIST_BREAKOUT",
}


@dataclass(frozen=True)
class Structure:
    lo: float
    hi: float
    structure_type: str
    proof: str
    created_date: Optional[str] = None
    members: int = 1
    source_key: str = ""

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _norm_col(c: str) -> str:
    s = str(c).strip().lower().replace(" ", "").replace("_", "")
    aliases = {
        "일자": "date", "날짜": "date", "date": "date", "trddd": "date",
        "시가": "open", "open": "open", "tddopnprc": "open",
        "고가": "high", "high": "high", "tddhgprc": "high",
        "저가": "low", "low": "low", "tddlwprc": "low",
        "종가": "close", "close": "close", "tddclsprc": "close",
        "거래량": "volume", "volume": "volume", "acctr dvol": "volume", "acctrdvol": "volume",
    }
    return aliases.get(s, str(c).strip().lower())


def standardize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError("OHLCV 데이터가 비어 있습니다.")
    x = df.copy()
    x = x.rename(columns={c: _norm_col(c) for c in x.columns})
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in x.columns]
    if missing:
        raise ValueError(f"필수 컬럼이 없습니다: {missing}. 필요한 컬럼: date/open/high/low/close/volume")

    # Date parser: accept YYYYMMDD, YYYY-MM-DD, pandas timestamps.
    if pd.api.types.is_numeric_dtype(x["date"]):
        x["date"] = pd.to_datetime(x["date"].astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    else:
        raw = x["date"].astype(str).str.strip()
        d1 = pd.to_datetime(raw, errors="coerce")
        need = d1.isna() & raw.str.fullmatch(r"\d{8}")
        if need.any():
            d1.loc[need] = pd.to_datetime(raw.loc[need], format="%Y%m%d", errors="coerce")
        x["date"] = d1

    for c in ["open", "high", "low", "close", "volume"]:
        if x[c].dtype == object:
            x[c] = x[c].astype(str).str.replace(",", "", regex=False).str.replace(" ", "", regex=False)
        x[c] = pd.to_numeric(x[c], errors="coerce")

    x = x.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    finite = np.isfinite(x[["open", "high", "low", "close"]].to_numpy(dtype=float)).all(axis=1)
    x["valid_session"] = finite & x["volume"].notna() & (x["volume"] != 0)
    return x


def add_b2_proxy_segments(valid: pd.DataFrame) -> pd.DataFrame:
    """
    CP66 operational B2-style proxy for OHLCV-only use:
    restart when raw close ratio crosses outside [0.65, 1.35].
    This is NOT a claim that the app has independently identified the corporate action.
    """
    x = valid.copy().reset_index(drop=True)
    ratio = x["close"] / x["close"].shift(1)
    boundary = (ratio < B2_PROXY_LOW) | (ratio > B2_PROXY_HIGH)
    boundary.iloc[0] = False
    x["b2_proxy_boundary"] = boundary.fillna(False)
    x["b2_proxy_close_ratio"] = ratio
    x["price_coordinate_segment"] = x["b2_proxy_boundary"].cumsum().astype(int)
    return x


def prepare_valid(df: pd.DataFrame) -> pd.DataFrame:
    x = standardize_ohlcv(df)
    v = x.loc[x["valid_session"]].copy().reset_index(drop=True)
    if len(v) < 2:
        raise ValueError("유효 일봉이 2개 미만이라 Daily Gate를 계산할 수 없습니다.")
    return add_b2_proxy_segments(v)


def _daily_gate_one(cur: pd.Series, prev: pd.Series) -> Dict[str, Any]:
    up_pen = bool(cur.high > prev.high)
    down_pen = bool(cur.low < prev.low)
    up_hold = bool(up_pen and cur.close > prev.high)
    down_hold = bool(down_pen and cur.close < prev.low)
    up_reject = bool(up_pen and not up_hold)
    down_reject = bool(down_pen and not down_hold)

    if up_pen and down_pen:
        geometry = "OUTSIDE"
        if up_hold:
            state = "OUTSIDE_UP_HOLD"
            accepted_dir = "UP"
            resolution = "UP_BREAK_HOLD"
        elif down_hold:
            state = "OUTSIDE_DOWN_HOLD"
            accepted_dir = "DOWN"
            resolution = "DOWN_BREAK_HOLD"
        else:
            state = "OUTSIDE_NEUTRAL"
            accepted_dir = None
            resolution = "OUTSIDE_AMBIGUOUS"
    elif up_pen:
        geometry = "UP"
        if up_hold:
            state = "UP_HOLD"
            accepted_dir = "UP"
            resolution = "UP_BREAK_HOLD"
        else:
            state = "UP_REJECT"
            accepted_dir = None
            resolution = "UP_BREAK_REJECT"
    elif down_pen:
        geometry = "DOWN"
        if down_hold:
            state = "DOWN_HOLD"
            accepted_dir = "DOWN"
            resolution = "DOWN_BREAK_HOLD"
        else:
            state = "DOWN_REJECT"
            accepted_dir = None
            resolution = "DOWN_BREAK_REJECT"
    else:
        geometry = "INSIDE"
        state = "INSIDE"
        accepted_dir = None
        resolution = "INSIDE"

    return {
        "geometry": geometry,
        "state": state,
        "resolution": resolution,
        "accepted_dir": accepted_dir,
        "up_penetrate": up_pen,
        "down_penetrate": down_pen,
        "up_hold": up_hold,
        "down_hold": down_hold,
        "up_reject": up_reject,
        "down_reject": down_reject,
        "prev_high": float(prev.high),
        "prev_low": float(prev.low),
    }


def build_gate_series(valid: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    last_accepted_dir: Optional[str] = None
    last_accepted_idx: Optional[int] = None
    for i, cur in valid.iterrows():
        rec: Dict[str, Any] = {
            "date": cur.date,
            "segment": int(cur.price_coordinate_segment),
            "gate_state": None,
            "gate_geometry": None,
            "gate_resolution": None,
            "accepted_dir": None,
            "controller_state": "NEUTRAL",
            "authority": "NEUTRAL",
            "context_dir": last_accepted_dir,
            "freshness_age": None,
        }
        if i == 0 or valid.iloc[i - 1].price_coordinate_segment != cur.price_coordinate_segment:
            last_accepted_dir = None
            last_accepted_idx = None
            rec.update({"gate_state": "SEGMENT_START", "gate_geometry": "SEGMENT_START", "gate_resolution": "SEGMENT_START"})
            rows.append(rec)
            continue

        prev = valid.iloc[i - 1]
        g = _daily_gate_one(cur, prev)
        rec.update({
            "gate_state": g["state"], "gate_geometry": g["geometry"], "gate_resolution": g["resolution"],
            "accepted_dir": g["accepted_dir"], "prev_high": g["prev_high"], "prev_low": g["prev_low"],
            "up_penetrate": g["up_penetrate"], "down_penetrate": g["down_penetrate"],
        })

        if g["accepted_dir"] is not None:
            is_reset = last_accepted_dir is not None and g["accepted_dir"] != last_accepted_dir
            rec["controller_state"] = "RESET" if is_reset else "FRESH"
            rec["authority"] = "FOCUS_ACTIVE"
            rec["context_dir"] = g["accepted_dir"]
            rec["freshness_age"] = 0
            last_accepted_dir = g["accepted_dir"]
            last_accepted_idx = i
        elif g["state"] in {"UP_REJECT", "DOWN_REJECT", "OUTSIDE_NEUTRAL"} and (g["up_penetrate"] or g["down_penetrate"]):
            rec["controller_state"] = "REJECT" if g["state"] != "OUTSIDE_NEUTRAL" else "OUTSIDE_AMBIGUOUS"
            rec["authority"] = "FAILED_OPEN" if g["state"] != "OUTSIDE_NEUTRAL" else "CONTEXT_ONLY"
            rec["context_dir"] = last_accepted_dir
            rec["freshness_age"] = None if last_accepted_idx is None else i - last_accepted_idx
        else:
            if last_accepted_dir is not None:
                rec["controller_state"] = "CARRY"
                rec["authority"] = "CONTEXT_ONLY"
                rec["context_dir"] = last_accepted_dir
                rec["freshness_age"] = i - last_accepted_idx if last_accepted_idx is not None else None
            else:
                rec["controller_state"] = "NEUTRAL"
                rec["authority"] = "NEUTRAL"
        rows.append(rec)
    return pd.DataFrame(rows)


def _side_for_outside(row: pd.Series, p: float) -> Optional[str]:
    if float(row.low) > p:
        return "A"  # Above
    if float(row.high) < p:
        return "B"  # Below
    return None  # touches the price


def _encounter_episodes_for_price(seg: pd.DataFrame, p: float) -> List[Dict[str, Any]]:
    low = seg["low"].to_numpy(float)
    high = seg["high"].to_numpy(float)
    touch_idx = np.flatnonzero((low <= p) & (high >= p))
    if touch_idx.size == 0:
        return []

    groups: List[np.ndarray] = []
    cut = np.flatnonzero(np.diff(touch_idx) > EPISODE_TOUCH_GAP) + 1
    groups = np.split(touch_idx, cut)
    out: List[Dict[str, Any]] = []
    n = len(seg)
    for g in groups:
        if g.size == 0:
            continue
        s = int(g[0]); e = int(g[-1])
        entry = None
        j = s - 1
        while j >= 0 and entry is None:
            entry = _side_for_outside(seg.iloc[j], p)
            j -= 1
        exit_side = None
        j = e + 1
        while j < n and exit_side is None:
            exit_side = _side_for_outside(seg.iloc[j], p)
            j += 1
        role = f"{entry}>{exit_side}" if entry and exit_side else (f"{entry}>NA" if entry else "NA")
        out.append({
            "start_idx": s,
            "end_idx": e,
            "start_date": seg.iloc[s].date,
            "end_date": seg.iloc[e].date,
            "entry_side": entry,
            "exit_side": exit_side,
            "role": role,
            "resolved": bool(entry and exit_side),
            "touch_count": int(g.size),
        })
    return out


def _exact_price_evidence(seg: pd.DataFrame) -> pd.DataFrame:
    """Build exact settlement-price episode evidence from unique closes."""
    closes = seg["close"].to_numpy(float)
    prices = np.unique(closes[np.isfinite(closes)])
    records: List[Dict[str, Any]] = []
    close_series = seg["close"].to_numpy(float)

    for p in prices:
        eps = _encounter_episodes_for_price(seg, float(p))
        if not eps:
            continue
        resolved = [ep for ep in eps if ep["resolved"]]
        rec: Dict[str, Any] = {
            "price": float(p),
            "settlement_count": int(np.count_nonzero(close_series == p)),
            "episode_count": len(eps),
            "resolved_count": len(resolved),
            "role_sequence": ";".join(ep["role"] for ep in eps),
            "episodes": eps,
        }
        if len(resolved) >= 2:
            a, b = resolved[-2], resolved[-1]
            rec.update({
                "pair_role1": a["role"], "pair_role2": b["role"],
                "pair_key": f"{a['start_date'].date()}~{a['end_date'].date()}~{a['role']}|{b['start_date'].date()}~{b['end_date'].date()}~{b['role']}",
                "pair_a": a, "pair_b": b,
            })
        else:
            rec.update({"pair_role1": None, "pair_role2": None, "pair_key": None, "pair_a": None, "pair_b": None})
        if len(resolved) == 1:
            rec["single_key"] = f"{resolved[0]['start_date'].date()}~{resolved[0]['end_date'].date()}~{resolved[0]['role']}"
            rec["single_ep"] = resolved[0]
        else:
            rec["single_key"] = None; rec["single_ep"] = None
        records.append(rec)
    return pd.DataFrame(records)


def _episode_hull(seg: pd.DataFrame, ep: Dict[str, Any], lo: float, hi: float) -> Optional[Tuple[float, float]]:
    sub = seg.iloc[int(ep["start_idx"]): int(ep["end_idx"]) + 1]
    c = sub.loc[(sub["close"] >= lo) & (sub["close"] <= hi), "close"].astype(float)
    if c.empty:
        return None
    return float(c.min()), float(c.max())


def _build_role_and_recurrent(exact: pd.DataFrame, seg: pd.DataFrame) -> Tuple[List[Structure], List[Structure]]:
    role_structures: List[Structure] = []
    recurrent: List[Structure] = []
    if exact.empty:
        return role_structures, recurrent

    pair = exact.loc[exact["pair_key"].notna()].copy()
    for key, g in pair.groupby("pair_key", sort=False):
        lo, hi = float(g.price.min()), float(g.price.max())
        role1 = str(g.iloc[0].pair_role1); role2 = str(g.iloc[0].pair_role2)
        semantic = COHERENT_ROLE_PAIRS.get((role1, role2))
        a = g.iloc[0].pair_a; b = g.iloc[0].pair_b
        created = str(pd.Timestamp(b["end_date"]).date()) if b else None
        if semantic:
            role_structures.append(Structure(
                lo=lo, hi=hi, structure_type=semantic,
                proof=f"latest resolved roles {role1}→{role2}; exact episode-token pair",
                created_date=created, members=int(g.price.nunique()), source_key=str(key),
            ))

        # Q1 recurrent settlement core can arise even if the role pair itself is cross-like.
        h1 = _episode_hull(seg, a, lo, hi) if a else None
        h2 = _episode_hull(seg, b, lo, hi) if b else None
        if h1 and h2:
            core_lo = max(h1[0], h2[0]); core_hi = min(h1[1], h2[1])
            if core_lo <= core_hi:
                recurrent.append(Structure(
                    lo=float(core_lo), hi=float(core_hi), structure_type="RECURRENT_SETTLEMENT_CORE",
                    proof=f"episode settlement hull overlap {h1[0]:g}–{h1[1]:g} ∩ {h2[0]:g}–{h2[1]:g}",
                    created_date=created, members=int(g.price.nunique()), source_key=str(key),
                ))
    return role_structures, recurrent


def _build_unretested_bases(exact: pd.DataFrame, seg: pd.DataFrame) -> List[Structure]:
    bases: List[Structure] = []
    if exact.empty:
        return bases
    single = exact.loc[exact["single_key"].notna()].copy()
    for key, g in single.groupby("single_key", sort=False):
        if g.price.nunique() < 3:
            continue
        ep = g.iloc[0].single_ep
        role = ep["role"] if ep else ""
        if role not in {"B>A", "A>B"}:
            continue
        lo, hi = float(g.price.min()), float(g.price.max())
        sub = seg.iloc[int(ep["start_idx"]): int(ep["end_idx"]) + 1]
        c = sub.loc[(sub.close >= lo) & (sub.close <= hi), "close"].astype(float).to_numpy()
        if len(np.unique(c)) < 3 or len(c) < 3:
            continue
        d = np.diff(c)
        if not (np.any(d > 0) and np.any(d < 0)):
            continue
        # Common traded intersection across the settlement sessions, intersected with close hull.
        used = sub.loc[(sub.close >= lo) & (sub.close <= hi)]
        traded_lo = float(used.low.max())
        traded_hi = float(used.high.min())
        common_lo = max(traded_lo, float(np.min(c)))
        common_hi = min(traded_hi, float(np.max(c)))
        if common_lo > common_hi:
            continue
        bases.append(Structure(
            lo=lo, hi=hi, structure_type="UNRETESTED_SETTLEMENT_BASE",
            proof=f"single resolved {role}; ≥3 settlement prices; bidirectional close path; common traded intersection {common_lo:g}–{common_hi:g}",
            created_date=str(pd.Timestamp(ep["end_date"]).date()), members=int(g.price.nunique()), source_key=str(key),
        ))
    return bases


def _build_rooted_discontinuities(seg: pd.DataFrame, roots: Sequence[Structure]) -> List[Structure]:
    """Promote exact gap edges only when a qualified root already exists before the gap."""
    edges: List[Structure] = []
    if not roots:
        return edges
    date_to_idx = {pd.Timestamp(d).date(): i for i, d in enumerate(seg.date)}
    seen = set()
    for root in roots:
        if not root.created_date:
            continue
        # CP57: the *directional crossing component* must resolve at origin.
        # A structural root alone is insufficient. The terminal resolved role tells
        # us the departure direction: B>A = upward crossing, A>B = downward crossing.
        proof = root.proof or ""
        if "→B>A" in proof or "single resolved B>A" in proof:
            required_dir = "UP"
        elif "→A>B" in proof or "single resolved A>B" in proof:
            required_dir = "DOWN"
        else:
            continue
        rd = pd.Timestamp(root.created_date).date()
        i = date_to_idx.get(rd)
        if i is None or i + 1 >= len(seg):
            continue
        a = seg.iloc[i]; b = seg.iloc[i + 1]
        if required_dir == "UP" and float(b.low) > float(a.high):
            vals = [(float(a.high), "ROOTED_DISCONTINUITY_PRE_EDGE"), (float(b.low), "ROOTED_DISCONTINUITY_POST_EDGE")]
            direction = "UP_GAP"
        elif required_dir == "DOWN" and float(b.high) < float(a.low):
            vals = [(float(a.low), "ROOTED_DISCONTINUITY_PRE_EDGE"), (float(b.high), "ROOTED_DISCONTINUITY_POST_EDGE")]
            direction = "DOWN_GAP"
        else:
            continue
        for p, typ in vals:
            k = (round(p, 10), typ, str(b.date.date()))
            if k in seen:
                continue
            seen.add(k)
            edges.append(Structure(
                lo=p, hi=p, structure_type=typ,
                proof=f"qualified root {root.structure_type}; immediate next-valid-session non-overlap {direction}; gap interior excluded",
                created_date=str(b.date.date()), members=1, source_key=f"root:{root.source_key}",
            ))
    return edges


def _dedupe_structures(structures: Sequence[Structure]) -> List[Structure]:
    # Keep typed identities; collapse exact duplicates of same type/geometry.
    out: Dict[Tuple[float, float, str], Structure] = {}
    for s in structures:
        key = (round(float(s.lo), 8), round(float(s.hi), 8), s.structure_type)
        old = out.get(key)
        if old is None or s.members > old.members:
            out[key] = s
    return sorted(out.values(), key=lambda z: (z.lo, z.hi, z.structure_type))


def build_structural_registry(valid: pd.DataFrame, include_rooted_edges: bool = False) -> Tuple[List[Structure], Dict[str, Any]]:
    latest_seg_id = int(valid.iloc[-1].price_coordinate_segment)
    seg = valid.loc[valid.price_coordinate_segment == latest_seg_id].copy().reset_index(drop=True)
    exact = _exact_price_evidence(seg)
    role, recurrent = _build_role_and_recurrent(exact, seg)
    bases = _build_unretested_bases(exact, seg)
    roots = _dedupe_structures(role + recurrent + bases)
    edge_candidates = _build_rooted_discontinuities(seg, roots)
    edges = edge_candidates if include_rooted_edges else []
    registry = _dedupe_structures(roots + edges)
    meta = {
        "latest_segment_id": latest_seg_id,
        "segment_start": str(seg.iloc[0].date.date()),
        "segment_end": str(seg.iloc[-1].date.date()),
        "segment_rows": int(len(seg)),
        "exact_settlement_prices": int(len(exact)),
        "role_structures": int(len(role)),
        "recurrent_cores": int(len(recurrent)),
        "unretested_bases": int(len(bases)),
        "rooted_edge_candidates": int(len(edge_candidates)),
        "rooted_edges_in_map": int(len(edges)),
        "registry_count": int(len(registry)),
    }
    return registry, meta


def _distance_to_structure(current: float, s: Structure) -> float:
    if current < s.lo:
        return s.lo - current
    if current > s.hi:
        return current - s.hi
    return 0.0


def select_ladder(registry: Sequence[Structure], current: float) -> Dict[str, Any]:
    upper = [s for s in registry if s.lo > current]
    lower = [s for s in registry if s.hi < current]
    occupied = [s for s in registry if s.lo <= current <= s.hi]
    upper.sort(key=lambda s: (s.lo - current, s.hi - s.lo, s.lo))
    lower.sort(key=lambda s: (current - s.hi, s.hi - s.lo, -s.hi))
    return {
        "upper_s1": upper[0] if len(upper) >= 1 else None,
        "upper_next": upper[1] if len(upper) >= 2 else None,
        "lower_s1": lower[0] if len(lower) >= 1 else None,
        "lower_next": lower[1] if len(lower) >= 2 else None,
        "upper_latent": upper[2:],
        "lower_latent": lower[2:],
        "current_engagement": occupied,
    }


def classify_interaction(bar: pd.Series, structure: Optional[Structure], side: str) -> str:
    if structure is None:
        return "NO_STRUCTURE"
    L, U = float(structure.lo), float(structure.hi)
    high, low, close = float(bar.high), float(bar.low), float(bar.close)
    if side == "UP":
        if high < L:
            return "NOT_REACHED"
        if high <= U:
            return "TOUCH"
        return "BREAK_HOLD" if close > U else "BREAK_REJECT"
    if side == "DOWN":
        if low > U:
            return "NOT_REACHED"
        if low >= L:
            return "TOUCH"
        return "BREAK_HOLD" if close < L else "BREAK_REJECT"
    raise ValueError("side must be UP or DOWN")


def _s_to_dict(s: Optional[Structure]) -> Optional[Dict[str, Any]]:
    return None if s is None else s.as_dict()


def _fmt_band(s: Optional[Structure]) -> str:
    if s is None:
        return "없음"
    if math.isclose(s.lo, s.hi):
        return f"{s.lo:,.0f}"
    return f"{s.lo:,.0f}~{s.hi:,.0f}"


def calculate_living_map(df: pd.DataFrame, compute_previous_interaction: bool = True, include_rooted_edges: bool = False) -> Dict[str, Any]:
    valid = prepare_valid(df)
    gates = build_gate_series(valid)
    cur = valid.iloc[-1]
    gate = gates.iloc[-1].to_dict()

    registry, reg_meta = build_structural_registry(valid, include_rooted_edges=include_rooted_edges)
    ladder = select_ladder(registry, float(cur.close))

    prior_interaction: Dict[str, Any] = {"available": False}
    if compute_previous_interaction and len(valid) >= 3:
        prev_valid = valid.iloc[:-1].copy().reset_index(drop=True)
        # Only meaningful if current bar remains in same coordinate segment as prior bar.
        if int(valid.iloc[-2].price_coordinate_segment) == int(valid.iloc[-1].price_coordinate_segment):
            prev_registry, _ = build_structural_registry(prev_valid, include_rooted_edges=include_rooted_edges)
            prev_ladder = select_ladder(prev_registry, float(valid.iloc[-2].close))
            up_res = classify_interaction(cur, prev_ladder["upper_s1"], "UP")
            down_res = classify_interaction(cur, prev_ladder["lower_s1"], "DOWN")
            prior_interaction = {
                "available": True,
                "as_of": str(valid.iloc[-2].date.date()),
                "bar_date": str(cur.date.date()),
                "upper_s1": _s_to_dict(prev_ladder["upper_s1"]),
                "upper_result": up_res,
                "lower_s1": _s_to_dict(prev_ladder["lower_s1"]),
                "lower_result": down_res,
                "upper_next_activation": "NEXT_ACTIVE_FROM_NEXT_VALID_SESSION" if up_res == "BREAK_HOLD" else "NO_PROMOTION",
                "lower_next_activation": "NEXT_ACTIVE_FROM_NEXT_VALID_SESSION" if down_res == "BREAK_HOLD" else "NO_PROMOTION",
            }

    b2_boundaries = valid.loc[valid.b2_proxy_boundary, ["date", "close", "b2_proxy_close_ratio"]]
    warnings: List[str] = []
    if gate.get("gate_geometry") == "OUTSIDE" or gate.get("gate_state", "").startswith("OUTSIDE"):
        warnings.append("OUTSIDE: 양쪽 전일 경계를 모두 침범해 일봉만으로 선후 순서를 알 수 없습니다.")
    if not b2_boundaries.empty:
        warnings.append("B2는 OHLCV-only 운영용 CP66 close-ratio proxy를 사용했습니다. 기업행위 정식 감사자료가 있으면 그 경계를 우선해야 합니다.")
    if reg_meta["registry_count"] == 0:
        warnings.append("현재 B2-consistent segment에서 자격을 충족한 구조가 없어 Gate-only 상태입니다.")
    if reg_meta["registry_count"] >= 100:
        warnings.append("구조 registry가 조밀합니다. CP71에 문서화된 hierarchy/chatter 한계에 해당할 수 있습니다.")
    if not include_rooted_edges and reg_meta.get("rooted_edge_candidates", 0):
        warnings.append("통합앱 build 0.1은 ROOTED_DISCONTINUITY edge를 감사 후보로만 보존하고 S1/NEXT에는 자동 승격하지 않습니다. CP57 crossing-component parity를 더 검증하기 전 과잉 edge 생성을 막기 위한 구현 안전장치입니다.")

    # Human-readable current summary, deliberately descriptive rather than BUY/SELL advice.
    focus_dir = gate.get("accepted_dir") if gate.get("authority") == "FOCUS_ACTIVE" else None
    if focus_dir == "UP":
        focus_text = f"상방 FOCUS_ACTIVE. 위 S1 {_fmt_band(ladder['upper_s1'])}가 현재 첫 구조입니다."
    elif focus_dir == "DOWN":
        focus_text = f"하방 FOCUS_ACTIVE. 아래 S1 {_fmt_band(ladder['lower_s1'])}가 현재 첫 구조입니다."
    elif gate.get("authority") == "FAILED_OPEN":
        focus_text = "오늘 개방 시도는 REJECT/FAILED_OPEN. 반대편 구조는 보이지만 자동 활성화하지 않습니다."
    elif gate.get("authority") == "CONTEXT_ONLY":
        focus_text = "새 HOLD가 없어 이전 방향은 CONTEXT_ONLY입니다. 양쪽 S1을 함께 봅니다."
    else:
        focus_text = "현재 fresh directional authority가 없습니다. 양쪽 구조를 중립적으로 봅니다."

    result = {
        "engine_version": HRF_ENGINE_VERSION,
        "verified_valid_session": str(cur.date.date()),
        "current_close": float(cur.close),
        "current_ohlcv": {k: float(cur[k]) for k in ["open", "high", "low", "close", "volume"]},
        "daily_gate": {
            "state": gate.get("gate_state"),
            "geometry": gate.get("gate_geometry"),
            "resolution": gate.get("gate_resolution"),
            "controller_state": gate.get("controller_state"),
            "authority": gate.get("authority"),
            "accepted_dir": gate.get("accepted_dir"),
            "context_dir": gate.get("context_dir"),
            "freshness_age": gate.get("freshness_age"),
            "prev_high": gate.get("prev_high"),
            "prev_low": gate.get("prev_low"),
        },
        "map": {
            "upper_s1": _s_to_dict(ladder["upper_s1"]),
            "upper_next": _s_to_dict(ladder["upper_next"]),
            "lower_s1": _s_to_dict(ladder["lower_s1"]),
            "lower_next": _s_to_dict(ladder["lower_next"]),
            "current_engagement": [s.as_dict() for s in ladder["current_engagement"]],
            "upper_latent_count": len(ladder["upper_latent"]),
            "lower_latent_count": len(ladder["lower_latent"]),
        },
        "last_bar_structural_interaction": prior_interaction,
        "registry_meta": reg_meta,
        "b2_proxy": {
            "rule": f"restart when close ratio outside [{B2_PROXY_LOW}, {B2_PROXY_HIGH}]",
            "boundary_count": int(len(b2_boundaries)),
            "latest_segment_start": reg_meta["segment_start"],
            "boundaries": [
                {"date": str(r.date.date()), "close": float(r.close), "ratio": float(r.b2_proxy_close_ratio)}
                for _, r in b2_boundaries.tail(20).iterrows()
            ],
        },
        "summary": focus_text,
        "warnings": warnings,
        "registry": [s.as_dict() for s in registry],
    }
    return result


def registry_frame(result: Dict[str, Any]) -> pd.DataFrame:
    rows = result.get("registry", [])
    if not rows:
        return pd.DataFrame(columns=["lo", "hi", "structure_type", "proof", "created_date", "members"])
    return pd.DataFrame(rows)


def compact_map_frame(result: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for label, key in [("UP S1", "upper_s1"), ("UP NEXT", "upper_next"), ("DOWN S1", "lower_s1"), ("DOWN NEXT", "lower_next")]:
        s = result["map"].get(key)
        rows.append({
            "role": label,
            "lower": None if s is None else s["lo"],
            "upper": None if s is None else s["hi"],
            "type": None if s is None else s["structure_type"],
            "created_date": None if s is None else s.get("created_date"),
        })
    return pd.DataFrame(rows)
