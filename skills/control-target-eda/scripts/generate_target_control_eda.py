#!/usr/bin/env python3
"""Generate interactive Plotly HTML EDA for target/control process tags."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
for dep_dir in (Path.cwd() / ".python_deps", SCRIPT_DIR.parent / ".python_deps"):
    if dep_dir.exists():
        sys.path.insert(0, str(dep_dir))

try:
    import numpy as np
    import pandas as pd
    import plotly
    import plotly.graph_objects as go
    import plotly.io as pio
    from plotly.subplots import make_subplots
except ImportError as exc:
    raise SystemExit(
        "Missing dependencies. Install locally with:\n"
        "python -m pip install --target .python_deps pandas numpy plotly pyarrow openpyxl"
    ) from exc


TIME_CANDIDATES = ["timestamp", "ts", "time", "datetime", "date_time"]
SUPPORTED_EXTS = {".parquet", ".csv", ".xlsx", ".xls"}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def split_csv_values(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    out: List[str] = []
    for value in values:
        out.extend([item.strip() for item in value.split(",") if item.strip()])
    return list(dict.fromkeys(out))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Directory containing data files")
    parser.add_argument("--files", action="append", help="Comma-separated files; may be repeated")
    parser.add_argument("--target", required=True, help="Target tag column")
    parser.add_argument("--controls", action="append", help="Comma-separated control tag columns")
    parser.add_argument("--time-col", help="Timestamp column. Auto-detected if omitted")
    parser.add_argument("--metadata", type=Path, help="Optional metadata JSON")
    parser.add_argument("--output-dir", type=Path, default=Path("plotly_eda_outputs"))
    parser.add_argument("--format", choices=["auto", "iidf", "two-row-csv", "csv", "excel", "parquet"], default="auto")
    parser.add_argument("--max-points-display", type=int, default=20000)
    parser.add_argument("--sampling-pairs", type=Path, help="Optional JSON config for raw-vs-aggregate comparison")
    parser.add_argument("--locale", choices=["zh", "en"], default="zh")
    return parser.parse_args()


def find_files(args: argparse.Namespace) -> List[Path]:
    files: List[Path] = []
    for item in split_csv_values(args.files):
        files.append(Path(item).expanduser())
    if args.data_dir:
        files.extend(path for path in sorted(args.data_dir.expanduser().iterdir()) if path.suffix.lower() in SUPPORTED_EXTS)
    files = [path.resolve() for path in files if path.exists() and path.suffix.lower() in SUPPORTED_EXTS]
    deduped = list(dict.fromkeys(files))
    if not deduped:
        raise SystemExit("No supported data files found. Use --data-dir or --files.")
    return deduped


def looks_like_datetime(value: str) -> bool:
    if value is None or str(value).strip() == "":
        return False
    parsed = pd.to_datetime([value], errors="coerce")
    return not pd.isna(parsed[0])


def detect_csv_header_mode(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        first = next(reader, [])
        second = next(reader, [])
    first_lower = [x.strip().lower() for x in first]
    if first_lower and first_lower[0] in TIME_CANDIDATES and second and not looks_like_datetime(second[0]):
        return "two-row-csv"
    return "csv"


def load_metadata(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if not path:
        return {}
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    rows: Iterable[Any]
    if isinstance(data, dict) and "tags" in data:
        rows = data["tags"]
    elif isinstance(data, dict) and "columns" in data:
        rows = data["columns"]
    elif isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = [{"tag": key, **value} if isinstance(value, dict) else {"tag": key, "description": value} for key, value in data.items()]
    else:
        rows = []

    meta: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tag = row.get("tag") or row.get("name") or row.get("code") or row.get("column")
        if tag:
            meta[str(tag)] = row
    return meta


def description_for(meta: Dict[str, Dict[str, Any]], tag: str, fallback: str = "") -> str:
    row = meta.get(tag, {})
    return str(row.get("description") or row.get("desc") or row.get("label") or fallback or tag)


def read_two_row_csv(path: Path) -> Tuple[pd.DataFrame, Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        codes = next(reader)
        desc = next(reader)
    df = pd.read_csv(path, skiprows=2, header=None, low_memory=False)
    df = df.iloc[:, : len(codes)]
    df.columns = codes
    field_map = {code: desc[i] if i < len(desc) and desc[i] else code for i, code in enumerate(codes) if code}
    return df, field_map


def read_file(path: Path, fmt: str) -> Tuple[pd.DataFrame, Dict[str, str]]:
    suffix = path.suffix.lower()
    chosen = fmt
    if fmt in ("auto", "iidf"):
        if suffix == ".parquet":
            chosen = "parquet"
        elif suffix in (".xlsx", ".xls"):
            chosen = "excel"
        elif suffix == ".csv":
            chosen = detect_csv_header_mode(path)

    if chosen == "parquet":
        return pd.read_parquet(path), {}
    if chosen == "excel":
        return pd.read_excel(path), {}
    if chosen == "two-row-csv":
        return read_two_row_csv(path)
    if chosen == "csv":
        return pd.read_csv(path, low_memory=False), {}
    raise SystemExit(f"Unsupported format for {path}: {fmt}")


def resolve_time_column(df: pd.DataFrame, requested: Optional[str]) -> str:
    if requested and requested in df.columns:
        return requested
    lowered = {str(col).lower(): col for col in df.columns}
    for candidate in TIME_CANDIDATES:
        if candidate in lowered:
            return str(lowered[candidate])
    raise SystemExit(f"Could not find a time column. Tried: {', '.join(TIME_CANDIDATES)}")


def numeric_frame(df: pd.DataFrame, time_col: str, tags: Sequence[str]) -> pd.DataFrame:
    wanted = [tag for tag in tags if tag in df.columns]
    missing = [tag for tag in tags if tag not in df.columns]
    if missing:
        print(f"Warning: missing tags skipped: {', '.join(missing)}", file=sys.stderr)
    out = df[[time_col] + wanted].copy()
    out.rename(columns={time_col: "_time"}, inplace=True)
    out["_time"] = pd.to_datetime(out["_time"], errors="coerce")
    for tag in wanted:
        out[tag] = pd.to_numeric(out[tag], errors="coerce")
    out = out.dropna(subset=["_time"]).sort_values("_time")
    return out


def sample_df(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if len(df) <= max_points:
        return df
    step = max(1, math.ceil(len(df) / max_points))
    return df.iloc[::step].copy()


def sample_series(series: pd.Series, max_points: int) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce").dropna()
    if len(series) <= max_points:
        return series
    return series.sample(max_points, random_state=7)


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join(f"<th>{esc(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def fig_html(fig: go.Figure, include_plotlyjs: bool = False) -> str:
    fig.update_layout(template=None)
    return pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs=("directory" if include_plotlyjs else False),
        config={"displaylogo": False, "responsive": True},
    )


def add_time_mode_buttons(fig: go.Figure) -> None:
    trace_count = len(fig.data)
    fig.update_layout(
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "x": 1,
                "xanchor": "right",
                "y": 1.12,
                "yanchor": "top",
                "buttons": [
                    {
                        "label": "Points + lines",
                        "method": "restyle",
                        "args": [{"mode": ["lines+markers"] * trace_count}],
                    },
                    {
                        "label": "Points only",
                        "method": "restyle",
                        "args": [{"mode": ["markers"] * trace_count}],
                    },
                ],
            }
        ]
    )


def write_page(path: Path, title: str, body: str) -> None:
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; background: #f6f8fb; color: #17202a; }}
    header {{ background: #14213d; color: #fff; padding: 22px 30px; }}
    main {{ max-width: 1360px; margin: 0 auto; padding: 22px 30px 46px; }}
    h1 {{ margin: 0; font-size: 25px; }}
    h2 {{ margin-top: 26px; border-left: 4px solid #2c7fb8; padding-left: 10px; font-size: 19px; }}
    a {{ color: #1b6ca8; }}
    .panel {{ background: #fff; border: 1px solid #dbe3eb; border-radius: 8px; padding: 16px; margin: 16px 0; overflow-x: auto; }}
    .center-table {{ max-width: 980px; margin: 16px auto; }}
    .muted {{ color: #66758a; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 13px; }}
    th, td {{ border: 1px solid #dce5ee; padding: 7px 8px; text-align: left; }}
    th {{ background: #edf2f7; }}
    .plotly-graph-div {{ margin: 0 auto; }}
  </style>
</head>
<body>
  <header><h1>{esc(title)}</h1></header>
  <main>{body}</main>
</body>
</html>""",
        encoding="utf-8",
    )


def label(record: Dict[str, Any], tag: str) -> str:
    return f"{tag} {record['field_map'].get(tag, tag)}"


def compact_label(record: Dict[str, Any], tag: str) -> str:
    desc = record["field_map"].get(tag, tag)
    if len(desc) > 18:
        desc = desc[:18] + "..."
    return f"{tag}<br>{desc}"


def format_num(value: Any, digits: int = 4) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def stats_row(series: pd.Series) -> List[str]:
    s = pd.to_numeric(series, errors="coerce")
    return [
        f"{s.notna().sum():,}",
        f"{s.isna().sum():,}",
        format_num(s.mean()),
        format_num(s.std()),
        format_num(s.quantile(0.05)),
        format_num(s.quantile(0.25)),
        format_num(s.quantile(0.5)),
        format_num(s.quantile(0.75)),
        format_num(s.quantile(0.95)),
        format_num(s.min()),
        format_num(s.max()),
    ]


def distribution_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, locale: str) -> None:
    tags = [target] + [tag for tag in controls if tag != target]
    subplot_titles = []
    for tag in tags:
        first = next((record for record in records if tag in record["df"].columns), records[0])
        subplot_titles.append(label(first, tag))

    fig = make_subplots(rows=len(tags), cols=1, shared_xaxes=False, subplot_titles=subplot_titles, vertical_spacing=0.012)
    summary_rows: List[List[str]] = []
    key_rows: List[List[str]] = []
    for row_idx, tag in enumerate(tags, start=1):
        for record in records:
            if tag not in record["df"].columns:
                continue
            values = sample_series(record["df"][tag], 50000)
            if values.empty:
                continue
            fig.add_trace(
                go.Box(
                    x=values,
                    name=record["name"],
                    legendgroup=record["name"],
                    showlegend=row_idx == 1,
                    boxpoints=False,
                    line={"width": 1.2},
                    hovertemplate=f"{esc(label(record, tag))}<br>{esc(record['name'])}<br>value=%{{x:.4f}}<extra></extra>",
                ),
                row=row_idx,
                col=1,
            )
            s = record["df"][tag]
            summary_rows.append([record["name"], tag, record["field_map"].get(tag, tag)] + stats_row(s))
            key_rows.append(
                [
                    record["name"],
                    tag,
                    format_num(s.quantile(0.25)),
                    format_num(s.mean()),
                    format_num(s.quantile(0.75)),
                    format_num(s.std()),
                    format_num(s.quantile(0.5)),
                ]
            )

    fig.update_layout(title="Target/control distribution overview", height=max(520, 210 * len(tags)), boxmode="group")
    body = "<p class='muted'>Statistics use full data; plotted distributions may be sampled for rendering only.</p>"
    body += "<div class='panel'>" + fig_html(fig, include_plotlyjs=True) + "</div>"
    body += "<div class='panel center-table'><h2>Key metrics</h2>" + table(["File", "Tag", "Q1", "Mean", "Q3", "Std", "P50"], key_rows) + "</div>"
    body += "<div class='panel'><h2>Summary</h2>" + table(
        ["File", "Tag", "Description", "Valid", "Missing", "Mean", "Std", "P05", "Q1", "P50", "Q3", "P95", "Min", "Max"],
        summary_rows,
    ) + "</div>"
    filename = "目标控制统计分布.html" if locale == "zh" else "target_control_distribution.html"
    title = "目标位号与控制位号统计分布" if locale == "zh" else "Target and Control Distribution"
    write_page(out_dir / filename, title, body)


def time_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, max_points: int, locale: str) -> None:
    parts = ["<p class='muted'>The target is shown in raw units; controls are standardized as z-scores for visual comparison.</p>"]
    for idx, record in enumerate(records):
        tags = [tag for tag in controls if tag in record["df"].columns]
        dfp = sample_df(record["df"][["_time", target] + tags].dropna(subset=["_time"]), max_points)
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, subplot_titles=[label(record, target), "Controls z-score"])
        target_df = dfp[["_time", target]].dropna()
        fig.add_trace(
            go.Scatter(
                x=target_df["_time"],
                y=target_df[target],
                mode="lines+markers",
                name=label(record, target),
                line={"width": 1.4, "color": "#1f77b4"},
                marker={"size": 3, "opacity": 0.55},
                hovertemplate="time=%{x}<br>target=%{y:.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )
        for tag in tags:
            series = pd.to_numeric(dfp[tag], errors="coerce")
            std = series.std()
            if pd.isna(std) or std == 0:
                continue
            z = (series - series.mean()) / std
            fig.add_trace(
                go.Scatter(
                    x=dfp["_time"],
                    y=z,
                    mode="lines+markers",
                    name=label(record, tag),
                    line={"width": 1},
                    marker={"size": 2.5, "opacity": 0.45},
                    hovertemplate=f"time=%{{x}}<br>{esc(label(record, tag))} z=%{{y:.4f}}<extra></extra>",
                ),
                row=2,
                col=1,
            )
        fig.update_layout(title=f"{record['name']}: target and controls over time", height=760, hovermode="x unified", legend={"orientation": "h", "y": -0.18})
        add_time_mode_buttons(fig)
        parts.append(f"<div class='panel'><h2>{esc(record['name'])}</h2>{fig_html(fig, include_plotlyjs=(idx == 0))}</div>")
    filename = "目标控制时间变化.html" if locale == "zh" else "target_control_time.html"
    title = "目标位号与控制位号时间变化" if locale == "zh" else "Target and Control Time Trends"
    write_page(out_dir / filename, title, "".join(parts))


def correlation_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, max_points: int, locale: str) -> None:
    parts = ["<p class='muted'>Correlation is computed on full paired data. Scatter plots are sampled for display only.</p>"]
    first_plot = True
    for record in records:
        tags = [target] + [tag for tag in controls if tag in record["df"].columns and tag != target]
        df = record["df"][tags].dropna(how="all")
        corr = df.corr(numeric_only=True)
        labels = [compact_label(record, tag) for tag in corr.columns]
        fig_heat = go.Figure(
            go.Heatmap(
                z=corr.values,
                x=labels,
                y=labels,
                zmin=-1,
                zmax=1,
                colorscale="RdBu",
                reversescale=True,
                hovertemplate="x=%{x}<br>y=%{y}<br>corr=%{z:.4f}<extra></extra>",
            )
        )
        fig_heat.update_layout(title=f"{record['name']}: correlation heatmap", height=720)

        rows: List[List[str]] = []
        bars: List[Tuple[str, str, float]] = []
        for tag in tags:
            if tag == target:
                continue
            pair = record["df"][[target, tag]].dropna()
            value = pair[target].corr(pair[tag]) if len(pair) >= 3 and pair[tag].std() != 0 else np.nan
            rows.append([tag, record["field_map"].get(tag, tag), f"{len(pair):,}", format_num(value)])
            if not pd.isna(value):
                bars.append((tag, record["field_map"].get(tag, tag), float(value)))
        bars.sort(key=lambda item: abs(item[2]), reverse=True)

        fig_bar = go.Figure(
            go.Bar(
                x=[value for _, _, value in bars],
                y=[f"{tag}<br>{desc[:18]}" for tag, desc, _ in bars],
                orientation="h",
                marker_color=["#2a9d8f" if value >= 0 else "#d95f45" for _, _, value in bars],
                hovertemplate="%{y}<br>corr=%{x:.4f}<extra></extra>",
            )
        )
        fig_bar.update_layout(title=f"{record['name']}: controls vs target correlation", height=max(420, 26 * len(bars) + 160), xaxis_title="Pearson corr", yaxis={"autorange": "reversed"})

        scatter_titles = [f"{tag} {desc[:18]}" for tag, desc, _ in bars[:4]]
        fig_scatter = make_subplots(rows=2, cols=2, subplot_titles=scatter_titles)
        for pos, (tag, _desc, value) in enumerate(bars[:4], start=1):
            pair = sample_df(record["df"][[target, tag]].dropna(), max_points)
            row = 1 if pos <= 2 else 2
            col = 1 if pos in (1, 3) else 2
            fig_scatter.add_trace(
                go.Scatter(
                    x=pair[tag],
                    y=pair[target],
                    mode="markers",
                    marker={"size": 3, "opacity": 0.35},
                    name=f"{tag} corr={value:.3f}",
                    hovertemplate=f"{esc(tag)}=%{{x:.4f}}<br>{esc(target)}=%{{y:.4f}}<extra></extra>",
                ),
                row=row,
                col=col,
            )
        fig_scatter.update_layout(title=f"{record['name']}: top related controls", height=720, showlegend=False)
        parts.append(
            f"<div class='panel'><h2>{esc(record['name'])}</h2>"
            + fig_html(fig_heat, include_plotlyjs=first_plot)
            + fig_html(fig_bar)
            + fig_html(fig_scatter)
            + "<h2>Details</h2>"
            + table(["Control tag", "Description", "Paired rows", "Correlation"], rows)
            + "</div>"
        )
        first_plot = False
    filename = "目标控制相关性分析.html" if locale == "zh" else "target_control_correlation.html"
    title = "目标位号与控制位号相关性分析" if locale == "zh" else "Target and Control Correlation"
    write_page(out_dir / filename, title, "".join(parts))


def overview_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, locale: str) -> None:
    rows = []
    for record in records:
        series = record["df"][target] if target in record["df"].columns else pd.Series(dtype=float)
        rows.append(
            [
                record["name"],
                f"{len(record['df']):,}",
                record["df"]["_time"].min(),
                record["df"]["_time"].max(),
                f"{series.notna().sum():,}",
                f"{series.isna().sum():,}",
                format_num(series.mean()),
                format_num(series.std()),
            ]
        )
    links = [
        ("统计分布" if locale == "zh" else "Distribution", "目标控制统计分布.html" if locale == "zh" else "target_control_distribution.html"),
        ("时间变化" if locale == "zh" else "Time trends", "目标控制时间变化.html" if locale == "zh" else "target_control_time.html"),
        ("相关性分析" if locale == "zh" else "Correlation", "目标控制相关性分析.html" if locale == "zh" else "target_control_correlation.html"),
    ]
    link_html = "".join(f"<li><a href='{esc(href)}'>{esc(text)}</a></li>" for text, href in links)
    body = f"""
<div class="panel">
  <h2>Target</h2>
  <p><b>{esc(target)}</b></p>
  <p class="muted">Controls: {esc(', '.join(controls))}</p>
</div>
<div class="panel"><h2>Reports</h2><ul>{link_html}</ul></div>
<div class="panel"><h2>Data overview</h2>{table(["File", "Rows", "Start", "End", "Target valid", "Target missing", "Mean", "Std"], rows)}</div>
"""
    title = "目标控制 Plotly EDA" if locale == "zh" else "Target-Control Plotly EDA"
    write_page(out_dir / "index.html", title, body)


def sampling_impact_page(config_path: Path, records: Sequence[Dict[str, Any]], target: str, out_dir: Path, locale: str) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    pairs = config.get("pairs", config if isinstance(config, list) else [])
    by_file = {record["path"].name: record for record in records}
    parts: List[str] = []
    first = True
    for pair in pairs:
        raw = by_file.get(pair.get("raw_file", ""))
        aggregate = by_file.get(pair.get("aggregate_file", ""))
        if raw is None or aggregate is None:
            continue
        rule = pair.get("resample", "1min")
        raw_series = raw["df"][["_time", target]].dropna().set_index("_time")[target].sort_index()
        agg_series = aggregate["df"][["_time", target]].dropna().set_index("_time")[target].sort_index()
        start = max(raw_series.index.min(), agg_series.index.min())
        end = min(raw_series.index.max(), agg_series.index.max())
        raw_overlap = raw_series.loc[start:end]
        agg_overlap = agg_series.loc[start:end]
        variants = {
            "aggregate_file": agg_overlap,
            "raw_original": raw_overlap,
            f"raw_to_{rule}_last": raw_overlap.resample(rule).last().dropna(),
            f"raw_to_{rule}_mean": raw_overlap.resample(rule).mean().dropna(),
            f"raw_to_{rule}_median": raw_overlap.resample(rule).median().dropna(),
        }
        fig = go.Figure()
        rows = []
        for name, values in variants.items():
            fig.add_trace(go.Box(y=sample_series(values, 50000), name=name, boxpoints="outliers", boxmean="sd"))
            rows.append([name] + stats_row(values))
        fig.update_layout(title=f"{pair.get('label', raw['name'])}: sampling impact", height=680, boxmode="group")
        parts.append(
            f"<div class='panel'><h2>{esc(pair.get('label', raw['name']))}</h2>"
            + fig_html(fig, include_plotlyjs=first)
            + table(["Series", "Valid", "Missing", "Mean", "Std", "P05", "Q1", "P50", "Q3", "P95", "Min", "Max"], rows)
            + "</div>"
        )
        first = False
    if parts:
        title = "降采样影响分析" if locale == "zh" else "Sampling Impact Analysis"
        write_page(out_dir / ("降采样影响分析.html" if locale == "zh" else "sampling_impact.html"), title, "".join(parts))


def main() -> None:
    args = parse_args()
    controls = split_csv_values(args.controls)
    tags = [args.target] + [tag for tag in controls if tag != args.target]
    files = find_files(args)
    metadata = load_metadata(args.metadata)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    plotly_js = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
    if plotly_js.exists():
        shutil.copy2(plotly_js, args.output_dir / "plotly.min.js")

    records: List[Dict[str, Any]] = []
    for path in files:
        print(f"Reading {path.name} ...", flush=True)
        raw_df, csv_field_map = read_file(path, args.format)
        time_col = resolve_time_column(raw_df, args.time_col)
        df = numeric_frame(raw_df, time_col, tags)
        if args.target not in df.columns:
            print(f"Warning: target {args.target} missing in {path.name}; skipping file", file=sys.stderr)
            continue
        field_map = {tag: description_for(metadata, tag, csv_field_map.get(tag, tag)) for tag in tags}
        records.append({"name": path.stem, "path": path, "df": df, "field_map": field_map})

    if not records:
        raise SystemExit("No files contain the requested target tag.")

    distribution_page(records, args.target, controls, args.output_dir, args.locale)
    time_page(records, args.target, controls, args.output_dir, args.max_points_display, args.locale)
    correlation_page(records, args.target, controls, args.output_dir, args.max_points_display, args.locale)
    if args.sampling_pairs:
        sampling_impact_page(args.sampling_pairs, records, args.target, args.output_dir, args.locale)
    overview_page(records, args.target, controls, args.output_dir, args.locale)
    print(f"Wrote Plotly HTML reports to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
