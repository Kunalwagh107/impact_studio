"""FastAPI application: the interactive impact-study service."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analysis as A
from . import category_mapping as CM
from . import export_excel, export_pptx
from . import ingest, mapping as M, profiling as P, qc as QC
from . import market_mapping as MK

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FRONTEND = os.path.join(ROOT, "frontend")
UPLOADS = os.path.join(ROOT, "uploads")
OUTPUTS = os.path.join(ROOT, "outputs")
for d in (UPLOADS, OUTPUTS):
    os.makedirs(d, exist_ok=True)

app = FastAPI(title="Impact Studio", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class Source:
    id: str
    filename: str
    path: str
    sheets: list[str] = field(default_factory=list)
    size: int = 0
    cache: dict = field(default_factory=dict)   # (sheet, header_row) -> DataFrame
    last_used: float = 0.0

    def touch(self) -> None:
        self.last_used = time.time()

    def cached_frames_bytes(self) -> int:
        """Rough resident size of the frames this source is holding onto."""
        total = 0
        for df in self.cache.values():
            try:
                total += int(df.memory_usage(deep=True).sum())
            except Exception:
                pass
        return total

    def drop_caches(self) -> None:
        self.cache.clear()


SOURCES: dict[str, Source] = {}
# Reserved for caching a prepared frame set between steps. Deliberately NOT
# populated: a Prepared holds full canonicalised frames for both sides, and
# retaining it per request is how the process previously grew without bound.
PREPARED: dict[str, A.Prepared] = {}

# --- registry limits --------------------------------------------------------
# Every registered source can hold a full copy of a large sheet in ``cache``.
# Left unbounded, repeatedly opening the same workbook in one browser session
# grows the process by ~24 MB a time until it is killed mid-request, which the
# browser shows as an opaque 500. Two defences: the same file is only ever
# held once, and the number of retained sources is capped.
MAX_SOURCES = 6                      # LRU cap on registered sources
MAX_CACHED_FRAMES_PER_SOURCE = 3     # distinct (sheet, header_row) kept each
_FRAME_CACHE: dict[str, str] = {}    # content signature -> source id


def _content_signature(path: str) -> str:
    """Identify a workbook by (absolute path, size, mtime) + a cheap digest.

    Cheaper and more robust than hashing 39 MB on every open, while still
    distinguishing a file that has genuinely changed on disk.
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    h = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}"
                     .encode()).hexdigest()[:16]
    return h


def _evict_lru(exempt: str | None = None) -> list[str]:
    """Drop least-recently-used sources until the registry is within budget."""
    freed: list[str] = []
    while len(SOURCES) > MAX_SOURCES:
        candidates = [s for sid, s in SOURCES.items() if sid != exempt]
        if not candidates:
            break
        victim = min(candidates, key=lambda s: s.last_used)
        SOURCES.pop(victim.id, None)
        if victim.path.startswith(UPLOADS):
            # an uploaded copy is ours to remove; a user-supplied path is not
            try:
                os.remove(victim.path)
            except OSError:
                pass
        freed.append(victim.id)
    return freed


def _register_source(path: str, filename: str) -> Source:
    """Register a workbook, reusing an existing entry for the same file."""
    sig = _content_signature(path)
    existing_id = _FRAME_CACHE.get(sig)
    if existing_id and existing_id in SOURCES:
        src = SOURCES[existing_id]
        src.touch()
        return src
    sid = uuid.uuid4().hex[:12]
    src = Source(id=sid, filename=filename, path=path,
                 sheets=ingest.list_sheets(path),
                 size=os.path.getsize(path), last_used=time.time())
    SOURCES[sid] = src
    if sig:
        _FRAME_CACHE[sig] = sid
    _evict_lru(exempt=sid)
    return src


def _get_source(sid: str) -> Source:
    src = SOURCES.get(sid)
    if not src:
        raise HTTPException(404, f"Unknown source '{sid}'. Re-open the workbook "
                                 "(the session no longer holds it).")
    src.touch()
    return src


def _load(src: Source, sheet: str | None, header_row: int) -> pd.DataFrame:
    key = (sheet or "", int(header_row))
    if key in src.cache:
        src.cache[key] = src.cache.pop(key)     # mark as most-recently-used
        return src.cache[key]
    df = ingest.load_table(src.path, sheet_name=sheet or None,
                           header_row=header_row)
    src.cache[key] = df
    while len(src.cache) > MAX_CACHED_FRAMES_PER_SOURCE:
        src.cache.pop(next(iter(src.cache)))    # evict oldest frame
    return df


class DatasetSpec(BaseModel):
    source_id: str
    sheet: str | None = None
    header_row: int = 1
    split_column: str | None = None
    value: str | None = None       # the discriminator value for this dataset


def _frame_for(spec: DatasetSpec) -> pd.DataFrame:
    src = _get_source(spec.source_id)
    df = _load(src, spec.sheet, spec.header_row)
    if spec.split_column and spec.value is not None:
        if spec.split_column not in df.columns:
            raise HTTPException(400, f"Column '{spec.split_column}' not in sheet")
        df = df[df[spec.split_column].astype(str).str.strip() == str(spec.value)]
        df = df.reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@app.post("/api/source")
async def upload_source(file: UploadFile = File(...)):
    sid = uuid.uuid4().hex[:12]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename or "upload.xlsx")
    path = os.path.join(UPLOADS, f"{sid}__{safe}")
    with open(path, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    src = _register_source(path, file.filename or safe)
    return {"id": src.id, "filename": src.filename, "sheets": src.sheets,
            "size": src.size, "reused": src.id != sid}


class PathSource(BaseModel):
    path: str


@app.post("/api/source/from-path")
def source_from_path(req: PathSource):
    """Register a workbook already on disk without re-uploading it."""
    if not os.path.isfile(req.path):
        raise HTTPException(404, f"File not found: {req.path}")
    before = set(SOURCES)
    src = _register_source(os.path.abspath(req.path),
                           os.path.basename(req.path))
    return {"id": src.id, "filename": src.filename, "sheets": src.sheets,
            "size": src.size, "reused": src.id in before}


@app.get("/api/sources")
def list_sources():
    return [{"id": s.id, "filename": s.filename, "sheets": s.sheets,
             "size": s.size} for s in SOURCES.values()]


@app.get("/api/memory")
def memory_stats():
    """Expose what the session is holding, so growth is visible rather than fatal."""
    frames = {sid: int(s.cached_frames_bytes()) for sid, s in SOURCES.items()}
    return {
        "n_sources": len(SOURCES),
        "max_sources": MAX_SOURCES,
        "cached_frames_bytes": frames,
        "cached_total_mb": round(sum(frames.values()) / 1048576, 1),
        "n_prepared": len(PREPARED),
    }


@app.get("/api/source/{sid}/columns")
def source_columns(sid: str, sheet: str | None = None, header_row: int = 1,
                   limit: int = 200000):
    src = _get_source(sid)
    df = _load(src, sheet, header_row)
    # The preview rows must be JSON-safe. `astype(str)` is not enough: on a
    # float column carrying NaN, pandas 3 can leave the value as a float, and a
    # bare NaN is rejected by the JSON encoder with
    # "Out of range float values are not JSON compliant: nan" - which surfaced
    # as a 500 on this endpoint the moment a caller read a sheet whose preview
    # included a blank numeric cell. Fold every non-finite / missing value to ""
    # explicitly instead of trusting the cast.
    head = df.head(5)
    records = [
        {str(k): _json_cell(v) for k, v in row.items()}
        for row in head.to_dict(orient="records")
    ]
    return {
        "columns": [str(c) for c in df.columns],
        "rows": int(len(df)),
        "head": records,
    }


def _json_cell(v: Any) -> str:
    """A preview cell as a JSON-safe string. Missing / NaN / NaT -> ""."""
    if v is None:
        return ""
    try:
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return ""
    except Exception:
        pass
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        # pd.isna on a list/array returns an array, which is not a bool.
        pass
    return str(v)


@app.get("/api/source/{sid}/values")
def source_values(sid: str, column: str, sheet: str | None = None,
                  header_row: int = 1, limit: int = 5000):
    src = _get_source(sid)
    df = _load(src, sheet, header_row)
    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found")
    vals = P.distinct_values(df, column, limit)
    return {"column": column, "values": vals, "n": len(vals)}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class ProfileRequest(BaseModel):
    a: DatasetSpec
    b: DatasetSpec
    label_a: str = "Previous dataset"
    label_b: str = "Updated dataset"


@app.post("/api/profile")
def profile(req: ProfileRequest):
    df_a = _frame_for(req.a)
    df_b = _frame_for(req.b)
    pa = P.profile_dataset(df_a, req.label_a)
    pb = P.profile_dataset(df_b, req.label_b)

    def dim_values(df, col, limit=3000):
        return P.distinct_values(df, col, limit) if col else []

    return {
        "a": pa.to_dict(),
        "b": pb.to_dict(),
        "a_dim_values": {r: dim_values(df_a, c)
                         for r, c in pa.dimensions.items() if r != "dataset"},
        "b_dim_values": {r: dim_values(df_b, c)
                         for r, c in pb.dimensions.items() if r != "dataset"},
        "a_discriminators": (
            sorted(df_a[req.a.split_column].dropna().astype(str).unique().tolist())
            if req.a.split_column and req.a.split_column in df_a.columns else []
        ),
    }


# ---------------------------------------------------------------------------
# Mapping
#
# There is no "/api/mapping" endpoint any more. It proposed a target for every
# dimension member by matching names and, failing that, by value proximity, and
# the UI pre-filled its proposals. The brief is that the user defines the
# mapping, so that endpoint is gone rather than left reachable and unused.
#
# `backend/mapping.py` is still imported for `normalize_name` / `core_key`, which
# `category_mapping` uses, and `M.suggest_mapping` survives there as a library
# function. Nothing in the request path calls it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Market pairing (step 3)
# ---------------------------------------------------------------------------


class MarketMappingRequest(BaseModel):
    a: DatasetSpec
    b: DatasetSpec
    market_col_a: str
    market_col_b: str = ""
    metric_col_a: str = ""
    metric_col_b: str = ""
    # Optional hierarchy path column (the reference workbook has
    # "UU Market Name (New)"). Used for *evidence* only - it can flag that a
    # level looks wrong, but it never sets the level.
    path_col_a: str = ""
    # ...and, when the hierarchy lives in a separate lookup sheet rather than in
    # the data sheet, point at it explicitly. Left unset, we look for one.
    path_spec: DatasetSpec | None = None
    path_market_col: str = ""
    path_value_col: str = ""
    limit_values: int = 2000
    # The pairings the *user* has authored so far. Empty on first arrival, in
    # which case the response lists the two value sets and no pairings at all -
    # which is the point of the step. Nothing is classified behind their back.
    pairs: list[dict] = []


@app.post("/api/market-mapping")
def market_mapping(req: MarketMappingRequest):
    """List the market values on each side for the user to pair up.

    The brief narrowed this step to four things: take a market value from
    Dataset A, choose the corresponding value from Dataset B, set the level that
    pairing belongs to, and use that level to scope the analysis. Region and
    Channel are the vocabulary of the level, not separate things to be
    discovered.

    So this endpoint enumerates and advises; it does not classify. The hierarchy
    is still read, because a pairing marked "total" whose path sits under another
    market is worth pointing out - but that is a note against the user's choice,
    never a replacement for it.
    """
    df_a = _frame_for(req.a)
    df_b = _frame_for(req.b)
    col_a = req.market_col_a
    col_b = req.market_col_b or req.market_col_a
    if not col_a or col_a not in df_a.columns:
        raise HTTPException(400, f"market_col_a '{col_a}' is not in dataset A")

    def _side(df: pd.DataFrame, col: str, metric: str):
        if not col or col not in df.columns:
            return {}, {}, []
        keys = df[col].astype(str)
        keep = keys.ne("") & keys.ne("nan")
        values: dict[str, float] = {}
        if metric and metric in df.columns:
            m = pd.to_numeric(df.loc[keep, metric], errors="coerce")
            values = {str(k): float(v) for k, v in m.groupby(keys[keep]).sum().items()}
        counts = {str(k): int(v) for k, v in keys[keep].value_counts().items()}
        order = sorted(set(keys[keep].astype(str)))
        return values, counts, order

    va, ra, list_a = _side(df_a, col_a, req.metric_col_a)
    vb, rb, list_b = _side(df_b, col_b, req.metric_col_b)

    paths_a: dict[str, str] = {}
    paths_b: dict[str, str] = {}

    def _pairs_from(df: pd.DataFrame, mcol: str, pcol: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if not mcol or not pcol or mcol not in df.columns or pcol not in df.columns:
            return out
        for _, r in df[[mcol, pcol]].astype(str).drop_duplicates().iterrows():
            mk, path = str(r[mcol]), str(r[pcol])
            if mk and mk != "nan" and path and path != "nan" and mk not in out:
                out[mk] = path
        return out

    path_note = ""
    if req.path_spec is not None and req.path_market_col and req.path_value_col:
        paths_a = _pairs_from(_frame_for(req.path_spec),
                              req.path_market_col, req.path_value_col)
        path_note = f"{req.path_spec.sheet or ''}:{req.path_value_col}"
    elif req.path_col_a:
        paths_a = _pairs_from(df_a, col_a, req.path_col_a)
        path_note = f"{req.a.sheet or ''}:{req.path_col_a}"
    else:
        # The hierarchy usually lives in its own lookup sheet, not beside the
        # data. Look for one: a market-name column plus a column whose name
        # says "UU"/"hierarchy"/"path".
        try:
            src = _get_source(req.a.source_id)
            for sh in ingest.list_sheets(src.path):
                if sh == (req.a.sheet or ""):
                    continue
                try:
                    d = _load(src, sh, 1)
                except Exception:
                    continue
                cols = [str(c) for c in d.columns]
                mcol = next((c for c in cols
                             if c.strip().lower() in ("current market name", "market",
                                                      "markets", "market name")), None)
                pcol = next((c for c in cols
                             if re.search(r"\buu\b|hierarch|\bpath\b", c, re.I)), None)
                if mcol and pcol:
                    cand = _pairs_from(d, mcol, pcol)
                    if cand:
                        paths_a, path_note = cand, f"{sh}:{pcol}"
                        break
        except Exception:
            pass
    # The B side is looked up in its own frame, which is normally wired to the
    # same path column as A. Falling back to A's paths keeps the reference
    # workbook (where both sides share one hierarchy sheet) working.
    paths_b = _pairs_from(df_b, col_b, req.path_col_a) or paths_a

    res = MK.enumerate_markets(
        list_a, list_b, value_a=va, value_b=vb, rows_a=ra, rows_b=rb,
        paths_a=paths_a, paths_b=paths_b, pairs=req.pairs)
    out = res.to_dict()
    out["paths_used"] = bool(paths_a or paths_b)
    out["path_source"] = path_note or None
    return out


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------


class CategoryMappingRequest(BaseModel):
    a: DatasetSpec
    b: DatasetSpec
    cat_col_a: str
    sub_col_a: str = ""
    metric_col_a: str = ""
    cat_col_b: str
    sub_col_b: str = ""
    metric_col_b: str = ""
    # Accepted and ignored: the engine no longer scores a pairing by value
    # proximity. Kept in the schema so an older client does not 422.
    value_tol_pct: float = 60.0


@app.post("/api/category-mapping")
def category_mapping(req: CategoryMappingRequest):
    """List the category units on each side for the user to map.

    Names are never assumed to be equal, one A category may cover several B
    units or a category-plus-subcategory, and a category with no counterpart is
    reported rather than matched. Every returned row starts with no target: the
    user attaches them in the UI and the confirmed rows come back with the run.
    """
    df_a = _frame_for(req.a)
    df_b = _frame_for(req.b)
    if req.cat_col_a not in df_a.columns:
        raise HTTPException(400, f"Category column '{req.cat_col_a}' not in dataset A")
    if req.cat_col_b not in df_b.columns:
        raise HTTPException(400, f"Category column '{req.cat_col_b}' not in dataset B")
    res = CM.enumerate_category_units(
        df_a, df_b,
        req.cat_col_a, req.sub_col_a or None, req.metric_col_a,
        req.cat_col_b, req.sub_col_b or None, req.metric_col_b,
    )
    return res.to_dict()


# ---------------------------------------------------------------------------
# Analysis + QC
# ---------------------------------------------------------------------------


class CategoryMappingIn(BaseModel):
    """The user-confirmed category mapping.

    ``rows`` is what the user authored: every Dataset-1 unit with the Dataset-2
    targets they attached (none if they left it, in which case it is dropped from
    the analysis rather than reported as a one-sided category).

    ``include_new_in_b`` opts categories that exist only in Dataset B into the
    analysis in their own right. Off by default: a B-only category has no
    "before" figure, so reporting it as an impact is misleading unless the user
    has asked for it.
    """
    rows: list[dict] = []
    new_in_b: list[dict] = []
    include_new_in_b: bool = False


class MetricBlock(BaseModel):
    """One impact metric to measure, with its own per-side period wiring.

    A run may carry several of these (Sales Value, Volume, Numeric Distribution
    together). Each is resolved independently, because the two datasets need not
    name the same measure the same way, nor expose the same periods for it.

    ``growth_applicable`` is False for a distribution level (ND): its impact is
    the absolute change TY - YA, not a percentage.
    """
    key: str = ""              # stable id: sales_value | volume | nd | custom
    label: str = "Sales Value"
    family_a: str = ""
    family_b: str = ""
    a_prior: str = ""
    a_current: str = ""
    b_prior: str = ""
    b_current: str = ""
    is_rate: bool = False
    growth_applicable: bool = True
    weight_metric_a: str = ""
    weight_metric_b: str = ""


class RunRequest(BaseModel):
    a: DatasetSpec
    b: DatasetSpec
    label_a: str = "Previous dataset"
    label_b: str = "Updated dataset"
    # dimension columns (role -> column name), per dataset
    dim_col_a: dict[str, str] = {}
    dim_col_b: dict[str, str] = {}
    # metric wiring — single-metric form, kept for compatibility
    metric_label: str = ""
    a_prior: str = ""
    a_current: str = ""
    b_prior: str = ""
    b_current: str = ""
    is_rate: bool = False
    weight_metric_a: str = ""
    weight_metric_b: str = ""
    # multi-metric form: when non-empty, one combined report per category carries
    # a block for every metric selected. Supersedes the single-metric fields.
    metrics: list[MetricBlock] = []
    # selections
    markets: list[str] = []
    categories: list[str] = []
    top_n: int = 10
    client_brands: list[str] = []
    # market hierarchy: which level the analysis covers. The level is chosen by
    # the user, and `market_pairs` is what they authored in step 3 - the pairing
    # of an A market value with a B market value at that level. `baseline_market`
    # names the Total Market that share/contribution is measured against.
    market_level: str = ""              # total | region | channel | all
    market_levels: dict[str, str] = {}  # market value -> level (derived, advisory)
    market_pairs: list[dict] = []       # authored pairings from step 3
    baseline_market: str = ""
    # who the analysis is for, carried into the exports
    client_name: str = ""
    impact_name: str = ""
    # confirmed mappings
    mapping_a: dict[str, dict[str, str]] = {}
    mapping_b: dict[str, dict[str, str]] = {}
    category_mapping: CategoryMappingIn | None = None
    # trend
    trend_enabled: bool = False
    trend_metric_a: str = ""
    trend_metric_b: str = ""
    period_col_a: str = ""
    period_col_b: str = ""
    # trend-grain sources (optional, separate sheets)
    trend_a: DatasetSpec | None = None
    trend_b: DatasetSpec | None = None


def _validate_metric_wiring(req: RunRequest, df_a: pd.DataFrame,
                            df_b: pd.DataFrame) -> list[str]:
    """Return a list of reasons the metric wiring is unusable.

    Without this the run proceeds with blank column names and every figure comes
    back empty, which reads as "the metric selection does not work".
    """
    problems: list[str] = []

    def check_block(label: str, a_prior: str, a_current: str,
                    b_prior: str, b_current: str, weight_a: str,
                    weight_b: str, is_rate: bool) -> None:
        for side, df, prior, current in (
            (f"{label}: A (previous)", df_a, a_prior, a_current),
            (f"{label}: B (updated)", df_b, b_prior, b_current),
        ):
            for role, col in (("prior period", prior), ("current period", current)):
                if not col:
                    problems.append(f"Dataset {side}: no {role} column selected.")
                elif col not in df.columns:
                    problems.append(
                        f"Dataset {side}: column '{col}' for the {role} does not exist "
                        f"in this dataset. Available numeric columns: "
                        f"{', '.join(sorted(df.select_dtypes('number').columns)[:12])}"
                    )
        if is_rate:
            w = weight_a or weight_b
            if not w:
                problems.append(
                    f"{label}: a rate metric is selected but no weight column was "
                    f"chosen; weighted averaging needs one."
                )
            elif w not in df_a.columns and w not in df_b.columns:
                problems.append(f"{label}: weight column '{w}' exists in neither dataset.")

    if req.metrics:
        for m in req.metrics:
            check_block(m.label or m.key or "Metric",
                        m.a_prior, m.a_current, m.b_prior, m.b_current,
                        m.weight_metric_a, m.weight_metric_b, m.is_rate)
    else:
        check_block("Metric", req.a_prior, req.a_current, req.b_prior,
                    req.b_current, req.weight_metric_a, req.weight_metric_b,
                    req.is_rate)
    return problems


def _build_prepared(req: RunRequest) -> tuple[A.Prepared, pd.DataFrame, pd.DataFrame,
                                              pd.DataFrame | None, pd.DataFrame | None]:
    return _build_prepared_for(req, req.metrics[0] if req.metrics else None)


def _build_prepared_for(req: RunRequest, metric: "MetricBlock | None") -> tuple[
        A.Prepared, pd.DataFrame, pd.DataFrame,
        pd.DataFrame | None, pd.DataFrame | None]:
    """Build a prepared analysis for one metric block (or the legacy single form).

    ``_validate_metric_wiring`` has already run for every block by the time this
    is called, so the column names here are known to resolve.
    """
    df_a = _frame_for(req.a)
    df_b = _frame_for(req.b)

    problems = _validate_metric_wiring(req, df_a, df_b)
    if problems:
        raise HTTPException(400, {
            "error": "metric_wiring_incomplete",
            "message": ("The metric and period selection is incomplete, so the "
                        "analysis would come back empty."),
            "problems": problems,
        })

    dim_col_a = {k: v for k, v in req.dim_col_a.items() if v}
    dim_col_b = {k: v for k, v in req.dim_col_b.items() if v}

    cat_map_a: dict[str, str] = {}
    cat_map_b: dict[str, str] = {}
    excl_a: set[str] = set()
    excl_b: set[str] = set()
    if req.category_mapping and (req.category_mapping.rows
                                 or req.category_mapping.new_in_b):
        cat_map_a, cat_map_b, excl_a, excl_b = CM.resolve(
            req.category_mapping.rows, req.category_mapping.new_in_b,
            include_new_in_b=bool(req.category_mapping.include_new_in_b))

    # Which market values the analysis covers. When the user authored pairings in
    # step 3, the scope for a level is exactly the values they paired at that
    # level - and side A is filtered on A's own value while side B is filtered on
    # B's, because the two datasets need not name the same market the same way.
    # That is the reason the pairing has two sides at all.
    scope = list(req.markets)
    scope_b: list[str] = []
    baseline_b = ""
    if req.market_pairs:
        mk = MK.enumerate_markets([], [], pairs=req.market_pairs)
        lvl = (req.market_level or "total").strip() or "total"
        if not scope:
            scope, scope_b = MK.resolve_scope(mk, lvl)
        else:
            # An explicit market list wins for A; the pairing still supplies B's
            # counterpart names and the baseline's B-side name.
            scope_b = mk.b_scope_for(lvl)
        base_name = (req.baseline_market or "").strip()
        if base_name:
            for p in mk.pairs:
                if p.market_a == base_name and p.market_b:
                    baseline_b = p.market_b
                    break
    if not scope:
        # No authored pairing: fall back to the advisory level map, which is the
        # behaviour the reference workbook relied on before pairings existed.
        lvl = (req.market_level or "").strip()
        if lvl and lvl != "all" and req.market_levels:
            scope = sorted(m for m, l in req.market_levels.items() if l == lvl)

    if metric is not None:
        metric_label = metric.label or metric.key or req.metric_label
        a_prior, a_current = metric.a_prior, metric.a_current
        b_prior, b_current = metric.b_prior, metric.b_current
        is_rate = metric.is_rate
        growth_applicable = metric.growth_applicable
        weight_a, weight_b = metric.weight_metric_a, metric.weight_metric_b
        metric_key = metric.key or ""
    else:
        metric_label = req.metric_label
        a_prior, a_current = req.a_prior, req.a_current
        b_prior, b_current = req.b_prior, req.b_current
        is_rate = req.is_rate
        growth_applicable = not is_rate
        weight_a, weight_b = req.weight_metric_a, req.weight_metric_b
        metric_key = ""

    cfg = A.AnalysisConfig(
        metric_label=metric_label,
        metric_key=metric_key,
        a_prior=a_prior, a_current=a_current,
        b_prior=b_prior, b_current=b_current,
        is_rate=is_rate,
        growth_applicable=growth_applicable,
        weight_metric=weight_a or weight_b,
        weight_metric_b=weight_b,
        category_col=dim_col_a.get("category", ""),
        market_col=dim_col_a.get("market", ""),
        manufacturer_col=dim_col_a.get("manufacturer", ""),
        brand_col=dim_col_a.get("brand", ""),
        subcategory_col=dim_col_a.get("subcategory", ""),
        period_col=req.period_col_a,
        category_col_b=dim_col_b.get("category", ""),
        market_col_b=dim_col_b.get("market", ""),
        manufacturer_col_b=dim_col_b.get("manufacturer", ""),
        brand_col_b=dim_col_b.get("brand", ""),
        subcategory_col_b=dim_col_b.get("subcategory", ""),
        period_col_b=req.period_col_b,
        markets=scope,
        markets_b=scope_b,
        categories=req.categories,
        top_n=int(req.top_n),
        client_brands=req.client_brands,
        market_levels=req.market_levels,
        baseline_market=req.baseline_market,
        baseline_market_b=baseline_b,
        mapping_a=req.mapping_a,
        mapping_b=req.mapping_b,
        category_map_a=cat_map_a,
        category_map_b=cat_map_b,
        category_excluded_a=sorted(excl_a),
        category_excluded_b=sorted(excl_b),
        trend_enabled=req.trend_enabled,
        trend_a_metric=req.trend_metric_a,
        trend_b_metric=req.trend_metric_b,
    )
    prep = A.prepare(df_a, df_b, cfg)

    # Trend-grain frames, if supplied, are attached for the trend block
    ta = tb = None
    if req.trend_enabled and req.trend_a and req.trend_b:
        ta = _frame_for(req.trend_a)
        tb = _frame_for(req.trend_b)
        prep.notes.append("Trend data attached.")
    return prep, df_a, df_b, ta, tb


def catmap_results(authored_rows: list[dict],
                   new_in_b: list[dict] | None = None) -> dict:
    """Summarise user-authored category rows for QC, as a mapping-results dict.

    Same shape as `_mapping_results_from_request` returns, so a tool that drives
    the engine directly (rather than through the HTTP request model) can feed the
    QC the same view of the mapping the API would.
    """
    authored = [r for r in (authored_rows or []) if r.get("status")]
    mapped = [r for r in authored if r.get("status") == "mapped"]
    excluded = [r for r in authored if r.get("status") == "excluded"]
    unmapped = [r for r in authored if r.get("status") == "unmapped"]
    summary = {
        "dimension": "category",
        "n_source": len(authored), "n_target": len(new_in_b or []),
        "mapped": len(mapped), "excluded": len(excluded),
        "unmapped": len(unmapped),
        "new_in_b": len(new_in_b or []),
        "one_to_many": sum(1 for r in mapped if len(r.get("targets") or []) > 1),
        "merged_targets": sum(
            1 for r in mapped
            if any(t.get("subcategory") for t in (r.get("targets") or []))),
        "truncated": False,
        "confirmed": not unmapped,
        "unresolved": [r.get("source") for r in unmapped][:50],
    }
    return {"category": {"rows": authored, "summary": summary}}


def _mapping_results_from_request(req: "RunRequest") -> dict:
    """Summarise the user-confirmed mappings for the QC report.

    Two things this must not do. It must not report an "auto rate", because
    nothing is automatic any more - a rate over the machine's own guessing reads
    as a quality score for a decision the user made. And it must not report zero
    coverage failures when the mapping is incomplete; an unresolved row is
    reported as such so the panel says what it actually knows.
    """
    out: dict[str, Any] = {}
    for dim, mp in (req.mapping_a or {}).items():
        if not mp:
            continue
        out[dim] = {
            "rows": [{"source": s, "target": t, "status": "accepted"}
                     for s, t in mp.items()],
            "summary": {
                "dimension": dim,
                "n_source": len(mp), "n_target": len(mp),
                "mapped": len(mp),
                "unmapped": 0, "excluded": 0, "new_in_b": 0,
                "truncated": False, "confirmed": True,
            },
        }

    cm = req.category_mapping
    if cm and cm.rows:
        authored = [r for r in cm.rows if r.get("status")]
        mapped = [r for r in authored if r.get("status") == "mapped"]
        excluded = [r for r in authored if r.get("status") == "excluded"]
        unmapped = [r for r in authored if r.get("status") == "unmapped"]
        summary = {
            "dimension": "category",
            "n_source": len(authored), "n_target": len(cm.new_in_b),
            "mapped": len(mapped), "excluded": len(excluded),
            "unmapped": len(unmapped),
            "new_in_b": len(cm.new_in_b),
            "one_to_many": sum(1 for r in mapped if len(r.get("targets") or []) > 1),
            "merged_targets": sum(
                1 for r in mapped
                if any(t.get("subcategory") for t in (r.get("targets") or []))),
            "truncated": False,
            "confirmed": not unmapped,
            # A category the user left unresolved is not a mapping failure; it is
            # simply not part of the study. Named so the QC can say which.
            "unresolved": [r.get("source") for r in unmapped][:50],
        }
        out["category"] = {"rows": cm.rows, "summary": summary}
    return out


def _run_reports(req: RunRequest):
    """Run the analysis for every selected metric and combine into one per category.

    The brief asks that a run may cover Sales Value, Volume and ND together, and
    that the result is **one combined report per category** carrying a block per
    metric. So each metric is analysed independently (its own per-side columns,
    its own aggregation, its own growth rule) and the per-category results are
    merged by category name.
    """
    if not req.metrics:
        prep, df_a, df_b, ta, tb = _build_prepared(req)
        cats = req.categories or prep.categories
        reports = A.analyse(prep, cats)
        if ta is not None and tb is not None:
            for rep in reports:
                rep["trend"] = _trend_for(rep["category"], prep.cfg, ta, tb, req)
        return prep, df_a, df_b, reports, {}

    per_metric: list[tuple[A.Prepared, list[dict]]] = []
    first = None
    df_a = df_b = ta = tb = None
    cfgs_by_metric: dict[str, Any] = {}
    for m in req.metrics:
        prep, df_a, df_b, ta, tb = _build_prepared_for(req, m)
        if first is None:
            first = prep
        key = prep.cfg.metric_key or prep.cfg.metric_label
        cfgs_by_metric[key] = prep.cfg
        cats = req.categories or prep.categories
        per_metric.append((prep, A.analyse(prep, cats)))

    # Preserve the category order of the first metric, then append any category
    # that only a later metric produced (should not normally happen, but a
    # metric wired against a narrower market scope could).
    order: list[str] = [r["category"] for _, reps in per_metric for r in reps]
    order = list(dict.fromkeys(order))

    by_cat: dict[str, dict] = {}
    for prep, reps in per_metric:
        key = prep.cfg.metric_key or prep.cfg.metric_label
        for rep in reps:
            base = by_cat.get(rep["category"])
            if base is None:
                base = dict(rep)
                base["metrics"] = {}
                base["metric"] = rep["metric"]
                base["metric_key"] = key
                by_cat[rep["category"]] = base
            base["metrics"][key] = {
                "key": key,
                "label": rep["metric"],
                "is_rate_metric": rep.get("is_rate_metric", False),
                "growth_applicable": rep.get("growth_applicable", True),
                "total": rep["total"],
                "insights": rep.get("insights") or [],
                "channel_block": rep.get("channel_block") or [],
                "brand_block": rep.get("brand_block") or [],
                "manufacturer_top_n": rep.get("manufacturer_top_n") or [],
                "brand_top_n": rep.get("brand_top_n") or [],
                "client_brands": rep.get("client_brands") or [],
                "client_manufacturers": rep.get("client_manufacturers") or [],
                "contributors": rep.get("contributors") or [],
                "subcategory_block": (rep.get("blocks") or {}).get("subcategory") or [],
                "baseline": rep.get("baseline") or {},
            }

    reports = [by_cat[c] for c in order if c in by_cat]
    # The combined report keeps the FIRST metric as the headline (top-level
    # `total`, `insights`, `channel_block`...), so the existing renderers and the
    # exports keep working; the per-metric detail rides along in `metrics`.
    for rep in reports:
        first_key = next(iter(rep["metrics"]), None)
        if first_key:
            head = rep["metrics"][first_key]
            rep["metric_key"] = first_key
            rep["metric"] = head["label"]
            rep["total"] = head["total"]
            rep["insights"] = head["insights"]
            rep["channel_block"] = head["channel_block"]
            rep["brand_block"] = head["brand_block"]
            rep["manufacturer_top_n"] = head["manufacturer_top_n"]
            rep["brand_top_n"] = head["brand_top_n"]
            rep["client_brands"] = head["client_brands"]
            rep["client_manufacturers"] = head["client_manufacturers"]
            rep["contributors"] = head["contributors"]
            rep["is_rate_metric"] = head["is_rate_metric"]
            rep["growth_applicable"] = head["growth_applicable"]
            rep["baseline"] = head["baseline"]

    if ta is not None and tb is not None:
        for rep in reports:
            rep["trend"] = _trend_for(rep["category"], first.cfg, ta, tb, req)
    return first, df_a, df_b, reports, cfgs_by_metric


def _trend_for(category, cfg, ta, tb, req):
    pcol_a, pcol_b = req.period_col_a, req.period_col_b
    order_col = None
    for c in ("Period Order", "Period Order ", "period_order"):
        if c in ta.columns:
            order_col = c
            break
    out = {"period_col": pcol_a or pcol_b, "series": []}
    cat_col = cfg.category_col
    if not cat_col:
        return out
    try:
        sub_a = ta[ta[cat_col].astype(str) == str(category)]
        sub_b = tb[tb[cat_col].astype(str) == str(category)]
        ma = sub_a.groupby(pcol_a, dropna=True)[req.trend_metric_a].sum() if (
            pcol_a and req.trend_metric_a in ta.columns) else None
        mb = sub_b.groupby(pcol_b, dropna=True)[req.trend_metric_b].sum() if (
            pcol_b and req.trend_metric_b in tb.columns) else None
        if ma is None or mb is None:
            return out
        periods = list(dict.fromkeys(list(ma.index) + list(mb.index)))
        if order_col and order_col in ta.columns:
            omap = (ta[[pcol_a, order_col]].dropna().drop_duplicates()
                    .set_index(pcol_a)[order_col].to_dict())
            periods.sort(key=lambda p: float(omap.get(p, 1e9)))
        for p in periods:
            out["series"].append({
                "period": p,
                "before": float(ma.get(p)) if p in ma.index else None,
                "after": float(mb.get(p)) if p in mb.index else None,
            })
    except Exception:
        pass
    return out


def _compact_report(rep: dict, top_n: int) -> dict:
    """Trim a full report down to what the UI renders."""
    out = {
        "category": rep["category"],
        "metric": rep["metric"],
        "metric_key": rep.get("metric_key"),
        "is_rate_metric": rep.get("is_rate_metric"),
        "growth_applicable": rep.get("growth_applicable", True),
        "metrics": rep.get("metrics") or {},
        "markets": rep.get("markets"),
        "total": rep["total"],
        "insights": rep.get("insights") or [],
        "baseline": rep.get("baseline") or {},
        "channel_block": (rep.get("channel_block") or [])[:25],
        "brand_block": (rep.get("brand_block") or [])[:30],
        "manufacturer_top_n": rep.get("manufacturer_top_n") or [],
        "brand_top_n": rep.get("brand_top_n") or [],
        "client_brands": rep.get("client_brands") or [],
        "client_manufacturers": rep.get("client_manufacturers") or [],
        "contributors": rep.get("contributors") or [],
        "n_manufacturers": rep.get("n_manufacturers"),
        "n_brands": rep.get("n_brands"),
        "trend": rep.get("trend"),
    }
    return out


@app.post("/api/run")
def run(req: RunRequest):
    try:
        prep, df_a, df_b, reports, cfgs_by_metric = _run_reports(req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"{exc}\n{traceback.format_exc()}")

    cfg = prep.cfg
    qc_rep = QC.run_qc(reports, cfg, df_a, df_b,
                       mapping_results=_mapping_results_from_request(req),
                       cfgs_by_metric=cfgs_by_metric,
                       category_mapping=req.category_mapping)
    return {
        "n_categories": len(reports),
        "categories": [r["category"] for r in reports],
        "reports": [_compact_report(r, req.top_n) for r in reports],
        "qc": qc_rep.to_dict(),
        "notes": prep.notes,
        "metrics": [
            {"key": (m.key or m.label), "label": m.label,
             "is_rate": m.is_rate, "growth_applicable": m.growth_applicable}
            for m in req.metrics
        ] if req.metrics else ([{"key": "metric", "label": cfg.metric_label,
                                 "is_rate": cfg.is_rate,
                                 "growth_applicable": cfg.growth_applicable}]
                               if cfg.metric_label else []),
        "client_name": req.client_name,
        "impact_name": req.impact_name,
        "market": {
            "level": req.market_level or None,
            "scope": list(cfg.markets),
            "baseline_market": prep.baseline_name or None,
            "baseline_verified": bool(prep.baseline_verified),
            "baseline_before": prep.baseline_total[0],
            "baseline_after": prep.baseline_total[1],
        },
    }


class ExportRequest(RunRequest):
    run_name: str = "impact_run"
    include_excel: bool = True
    include_pptx: bool = True


@app.post("/api/export")
def export(req: ExportRequest):
    t0 = time.time()
    try:
        prep, df_a, df_b, reports, cfgs_by_metric = _run_reports(req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"{exc}\n{traceback.format_exc()}")

    cfg = prep.cfg
    run_dir = os.path.join(OUTPUTS, _safe_name(req.run_name))
    os.makedirs(run_dir, exist_ok=True)

    qc_global = QC.run_qc(reports, cfg, df_a, df_b,
                          mapping_results=_mapping_results_from_request(req),
                          cfgs_by_metric=cfgs_by_metric,
                          category_mapping=req.category_mapping)
    produced: dict[str, dict] = {}
    files: list[dict] = []

    # Context stamped into every deliverable, so two analyses for the same
    # client are distinguishable at a glance.
    meta = {
        "period_label": cfg.metric_label,
        "client_name": req.client_name,
        "impact_name": req.impact_name,
        "market_level": req.market_level,
        "market_scope": list(cfg.markets),
        "baseline_market": prep.baseline_name,
        "baseline_verified": bool(prep.baseline_verified),
        "baseline_before": prep.baseline_total[0],
        "baseline_after": prep.baseline_total[1],
    }

    for rep in reports:
        cat = rep["category"]
        cat_dir = os.path.join(run_dir, _safe_name(cat))
        os.makedirs(cat_dir, exist_ok=True)
        entry: dict[str, str] = {}
        base = _safe_name(cat)

        if req.include_excel:
            p = os.path.join(cat_dir, f"{base}_Impact.xlsx")
            export_excel.build_category_workbook(rep, qc_global.to_dict(), p, meta)
            entry["excel"] = p
        if req.include_pptx:
            p = os.path.join(cat_dir, f"{base}_Impact.pptx")
            export_pptx.build_category_deck(rep, qc_global.to_dict(), p, meta)
            entry["pptx"] = p
        produced[cat] = entry
        for kind, path in entry.items():
            files.append({"category": cat, "kind": kind, "path": path,
                          "rel": os.path.relpath(path, OUTPUTS).replace("\\", "/"),
                          "size": os.path.getsize(path)})

    # export-completeness check now that files exist
    qc_final = QC.run_qc(reports, cfg, df_a, df_b, produced=produced,
                         mapping_results=_mapping_results_from_request(req),
                         cfgs_by_metric=cfgs_by_metric,
                         category_mapping=req.category_mapping)

    # run-level index workbook
    index_path = os.path.join(run_dir, "00_QC_and_Index.xlsx")
    _write_index(index_path, reports, qc_final.to_dict(), req.run_name, meta)
    files.append({"category": "(run)", "kind": "index", "path": index_path,
                  "rel": os.path.relpath(index_path, OUTPUTS).replace("\\", "/"),
                  "size": os.path.getsize(index_path)})

    return {
        "run_name": req.run_name,
        "run_dir": run_dir,
        "rel_dir": os.path.relpath(run_dir, OUTPUTS).replace("\\", "/"),
        "n_categories": len(reports),
        "files": files,
        "qc": qc_final.to_dict(),
        "elapsed_sec": round(time.time() - t0, 2),
        "client_name": req.client_name,
        "impact_name": req.impact_name,
        "market": {
            "level": req.market_level or None,
            "scope": list(cfg.markets),
            "baseline_market": prep.baseline_name or None,
            "baseline_verified": bool(prep.baseline_verified),
        },
    }


def _write_index(path: str, reports: list[dict], qc: dict, run_name: str,
                 meta: dict | None = None):
    import xlsxwriter

    meta = meta or {}
    # Guard the parent here as well. The per-category exporters already do this
    # for their own file, but the index did not - and on a synced workspace
    # (OneDrive/Dropbox) a directory created earlier in the run can be
    # momentarily unavailable by the time the index is written, which surfaced
    # as FileCreateError deep inside xlsxwriter.
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb = xlsxwriter.Workbook(path, {"nan_inf_to_errors": True})
    fh = wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1,
                        "border_color": "#BFBFBF"})
    fl = wb.add_format({"border": 1, "border_color": "#BFBFBF"})
    fn = wb.add_format({"border": 1, "border_color": "#BFBFBF",
                        "num_format": "#,##0"})
    fp = wb.add_format({"border": 1, "border_color": "#BFBFBF",
                        "num_format": '+0.0"%";-0.0"%";0.0"%"'})
    ft = wb.add_format({"bold": True, "font_size": 14})

    ws = wb.add_worksheet("Category Summary")
    ws.set_column("A:A", 34)
    ws.set_column("B:I", 18)
    ws.write(0, 0, meta.get("impact_name") or f"Impact study - {run_name}", ft)
    bits: list[str] = []
    if meta.get("client_name"):
        bits.append(f"Client: {meta['client_name']}")
    bits.append(f"{len(reports)} categories")
    bits.append(f"QC: {qc.get('worst')}")
    if meta.get("baseline_market"):
        bits.append(f"shares measured against {meta['baseline_market']}")
    ws.write(1, 0, "  |  ".join(bits), fl)
    heads = ["Category", "Metric", "BEFORE MAT YA", "BEFORE MAT TY",
             "AFTER MAT YA", "AFTER MAT TY", "Abs change", "Level shift (pp)",
             "Contribution vs Total Market (%)"]
    for i, h in enumerate(heads):
        ws.write(3, i, h, fh)
    fp_note = fp
    r = 4
    for rep in reports:
        # One row per metric. A run may carry Sales Value, Volume and ND
        # together, and each has its own total; a single row per category would
        # silently show only the first metric's numbers.
        blocks = rep.get("metrics") or {"": rep}
        for key, blk in blocks.items():
            t = blk.get("total") or {}
            ws.write(r, 0, rep["category"], fl)
            ws.write(r, 1, blk.get("label") or rep.get("metric") or key, fl)
            for c, k in ((2, "before_prior"), (3, "before_current"),
                         (4, "after_prior"), (5, "after_current")):
                if t.get(k) is not None:
                    ws.write_number(r, c, t[k], fn)
            if t.get("abs_change") is not None:
                ws.write_number(r, 6, t["abs_change"], fn)
            if t.get("level_shift_pp") is not None:
                ws.write_number(r, 7, t["level_shift_pp"], fp_note)
            ins = {i.get("key"): i for i in (blk.get("insights") or [])}
            share = (ins.get("contribution") or {}).get("after_share_pct")
            if share is not None:
                ws.write_number(r, 8, share, fp_note)
            r += 1
    ws.freeze_panes(4, 1)

    ws2 = wb.add_worksheet("QC")
    ws2.set_column("A:A", 26)
    ws2.set_column("B:B", 9)
    ws2.set_column("C:C", 72)
    ws2.write(0, 0, f"Automated QC - {qc.get('worst')}", ft)
    for i, h in enumerate(["Check", "Status", "Message"]):
        ws2.write(2, i, h, fh)
    for r, c in enumerate(qc.get("checks", []), start=3):
        ws2.write(r, 0, c["name"], fl)
        ws2.write(r, 1, c["status"], fl)
        ws2.write(r, 2, c["message"], fl)
    wb.close()


def _safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", str(s)).strip()
    s = re.sub(r"\s+", "_", s)
    return (s or "unnamed")[:120]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@app.get("/api/files")
def list_files():
    out = []
    for root, _dirs, names in os.walk(OUTPUTS):
        for n in names:
            p = os.path.join(root, n)
            out.append({
                "name": n,
                "rel": os.path.relpath(p, OUTPUTS).replace("\\", "/"),
                "size": os.path.getsize(p),
                "mtime": os.path.getmtime(p),
            })
    out.sort(key=lambda x: -x["mtime"])
    return out


@app.get("/api/download")
def download(rel: str):
    p = os.path.normpath(os.path.join(OUTPUTS, rel))
    if not p.startswith(os.path.normpath(OUTPUTS)) or not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    return FileResponse(p, filename=os.path.basename(p))


@app.post("/api/open-folder")
def open_folder(rel: str = ""):
    p = os.path.normpath(os.path.join(OUTPUTS, rel))
    if not os.path.isdir(p):
        p = OUTPUTS
    try:
        os.startfile(p)  # type: ignore[attr-defined]
        return {"opened": p}
    except Exception as exc:
        return {"opened": None, "error": str(exc), "path": p}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"ok": True, "calamine": ingest._HAVE_CALAMINE,
            "rapidfuzz": M._HAVE_RAPIDFUZZ}


if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
