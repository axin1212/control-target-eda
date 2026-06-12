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
    parser.add_argument("--locale", choices=["zh", "en"], default="zh", help="Compatibility option; generated reports are always Chinese")
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


def detect_text_encoding(path: Path) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                handle.read(4096)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def detect_csv_header_mode(path: Path) -> str:
    encoding = detect_text_encoding(path)
    with path.open("r", encoding=encoding, newline="") as handle:
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
            meta[str(tag)] = dict(row)
    if isinstance(data, dict):
        descriptions = data.get("descriptions")
        if isinstance(descriptions, dict):
            for tag, description in descriptions.items():
                row = meta.setdefault(str(tag), {"tag": str(tag)})
                if isinstance(description, dict):
                    row.update(description)
                elif description not in (None, ""):
                    row["description"] = str(description)
    return meta


def description_for(meta: Dict[str, Dict[str, Any]], tag: str, fallback: str = "") -> str:
    row = meta.get(tag, {})
    return str(row.get("description") or row.get("desc") or row.get("label") or fallback or tag)


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def chinese_description(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    first_cjk = next((idx for idx, char in enumerate(text) if "\u4e00" <= char <= "\u9fff"), None)
    if first_cjk is not None:
        text = text[first_cjk:].strip()
    role_suffixes = {
        "SP": "设定值",
        "PV": "测量值",
        "MV": "操纵量",
        "CV": "被控变量",
    }
    for suffix, replacement in role_suffixes.items():
        if text.endswith(suffix):
            return text[: -len(suffix)] + replacement
    return text


def schema_note_for(meta: Dict[str, Dict[str, Any]], tag: str, fallback: str = "") -> str:
    row = meta.get(tag, {})
    dtype = row.get("type") or row.get("dtype") or row.get("data_type") or ""
    desc = chinese_description(description_for(meta, tag, fallback))
    parts = [desc]
    if dtype:
        parts.append(f"类型：{dtype}")
    return "；".join(parts)


def read_two_row_csv(path: Path) -> Tuple[pd.DataFrame, Dict[str, str]]:
    encoding = detect_text_encoding(path)
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        codes = next(reader)
        desc = next(reader)
    df = pd.read_csv(path, skiprows=2, header=None, low_memory=False, encoding=encoding)
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
        return pd.read_csv(path, low_memory=False, encoding=detect_text_encoding(path)), {}
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


def shorten_text(value: object, max_chars: int = 28) -> str:
    text = str(value)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def readable_name(value: object, max_chars: int = 24) -> str:
    return shorten_text(value, max_chars)


def wrap_for_axis(value: object, first: int = 16, second: int = 16) -> str:
    text = str(value)
    if len(text) <= first:
        return text
    return text[:first] + "<br>" + shorten_text(text[first:], second)


def fig_html(fig: go.Figure, include_plotlyjs: bool = False) -> str:
    fig.update_layout(template=None)
    return pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs=("directory" if include_plotlyjs else False),
        config={"displaylogo": False, "responsive": True},
    )


def apply_readable_layout(
    fig: go.Figure,
    *,
    height: Optional[int] = None,
    top: int = 100,
    bottom: int = 120,
    left: int = 110,
    right: int = 80,
    legend: bool = True,
) -> None:
    layout: Dict[str, Any] = {
        "margin": {"l": left, "r": right, "t": top, "b": bottom},
        "font": {"size": 12},
        "title": {"x": 0.5, "xanchor": "center", "y": 0.98, "yanchor": "top"},
    }
    if height is not None:
        layout["height"] = height
    if legend:
        layout["legend"] = {
            "orientation": "h",
            "x": 0,
            "xanchor": "left",
            "y": -0.18,
            "yanchor": "top",
            "font": {"size": 11},
            "itemsizing": "constant",
        }
    fig.update_layout(**layout)
    fig.update_xaxes(automargin=True, tickfont={"size": 11})
    fig.update_yaxes(automargin=True, tickfont={"size": 11})


def add_time_mode_buttons(fig: go.Figure) -> None:
    trace_count = len(fig.data)
    fig.update_layout(
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "x": 1,
                "xanchor": "right",
                "y": 1.08,
                "yanchor": "top",
                "buttons": [
                    {
                        "label": "点+连线",
                        "method": "restyle",
                        "args": [{"mode": ["lines+markers"] * trace_count}],
                    },
                    {
                        "label": "只显示点",
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
    .plotly-graph-div {{ margin: 0 auto; min-width: 980px; }}
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
    desc = chinese_description(record["field_map"].get(tag, tag))
    if desc and desc != tag and has_cjk(desc):
        return f"{desc}（{tag}）"
    return tag


def tag_description(record: Dict[str, Any], tag: str) -> str:
    desc = chinese_description(record["field_map"].get(tag, tag))
    return desc if desc and desc != tag else record["field_map"].get(tag, tag)


def compact_label(record: Dict[str, Any], tag: str) -> str:
    desc = tag_description(record, tag)
    return f"{tag}<br>{wrap_for_axis(desc, 14, 14)}"


def heatmap_axis_label(record: Dict[str, Any], tag: str) -> str:
    desc = tag_description(record, tag)
    desc_part = shorten_text(desc, 16)
    tag_part = shorten_text(tag, 18)
    return f"{desc_part}<br>{tag_part}"


def heatmap_hover_label(record: Dict[str, Any], tag: str) -> str:
    return shorten_text(label(record, tag), 34)


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


def tag_reference_table(record: Dict[str, Any], tags: Sequence[str]) -> str:
    rows = [[tag_description(record, tag), tag] for tag in tags]
    return "<div class='panel center-table'><h2>位号中文解释</h2>" + table(["中文解释", "位号"], rows) + "</div>"


def distribution_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, locale: str) -> None:
    tags = [target] + [tag for tag in controls if tag != target]
    subplot_titles = []
    for tag in tags:
        first = next((record for record in records if tag in record["df"].columns), records[0])
        subplot_titles.append(shorten_text(label(first, tag), 46))

    fig = make_subplots(rows=len(tags), cols=1, shared_xaxes=False, subplot_titles=subplot_titles, vertical_spacing=0.018)
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
                    name=readable_name(record["name"], 22),
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
            summary_rows.append([record["name"], tag, tag_description(record, tag)] + stats_row(s))
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

    distribution_height = max(620, 230 * len(tags))
    fig.update_layout(title="目标位号与控制位号统计分布概览", boxmode="group")
    apply_readable_layout(fig, height=distribution_height, top=130, bottom=150, left=130, right=80)
    fig.update_annotations(font_size=13)
    body = "<p class='muted'>统计指标使用全量数据；图形渲染在数据量较大时仅对显示点做抽样。</p>"
    body += tag_reference_table(records[0], tags)
    body += "<div class='panel'>" + fig_html(fig, include_plotlyjs=True) + "</div>"
    body += "<div class='panel center-table'><h2>关键指标</h2>" + table(["文件", "位号", "Q1", "均值", "Q3", "标准差", "P50"], key_rows) + "</div>"
    body += "<div class='panel'><h2>统计摘要</h2>" + table(
        ["文件", "位号", "描述", "有效", "缺失", "均值", "标准差", "P05", "Q1", "P50", "Q3", "P95", "最小", "最大"],
        summary_rows,
    ) + "</div>"
    filename = "目标控制统计分布.html"
    title = "目标位号与控制位号统计分布"
    write_page(out_dir / filename, title, body)


def time_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, max_points: int, locale: str) -> None:
    all_tags = [target] + [tag for tag in controls if tag != target]
    parts = [
        "<p class='muted'>目标位号显示原始值；控制位号标准化为 z-score，便于在同一坐标尺度下比较变化节奏。右上角按钮可切换“点+连线”和“只显示点”。</p>",
        tag_reference_table(records[0], all_tags),
    ]
    for idx, record in enumerate(records):
        tags = [tag for tag in controls if tag in record["df"].columns]
        dfp = sample_df(record["df"][["_time", target] + tags].dropna(subset=["_time"]), max_points)
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.1,
            subplot_titles=[shorten_text(label(record, target), 52), "控制位号 z-score"],
        )
        target_df = dfp[["_time", target]].dropna()
        fig.add_trace(
            go.Scatter(
                x=target_df["_time"],
                y=target_df[target],
                mode="lines+markers",
                name=shorten_text(label(record, target), 28),
                line={"width": 1.4, "color": "#1f77b4"},
                marker={"size": 3, "opacity": 0.55},
                hovertemplate="时间=%{x}<br>目标=%{y:.4f}<extra></extra>",
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
                    name=shorten_text(label(record, tag), 28),
                    line={"width": 1},
                    marker={"size": 2.5, "opacity": 0.45},
                    hovertemplate=f"时间=%{{x}}<br>{esc(label(record, tag))} z=%{{y:.4f}}<extra></extra>",
                ),
                row=2,
                col=1,
            )
        fig.update_layout(title=f"{readable_name(record['name'], 42)}：目标与控制位号沿时间轴变化", hovermode="x unified")
        add_time_mode_buttons(fig)
        apply_readable_layout(fig, height=820, top=125, bottom=150, left=95, right=80)
        fig.update_annotations(font_size=13)
        parts.append(f"<div class='panel'><h2>{esc(record['name'])}</h2>{fig_html(fig, include_plotlyjs=(idx == 0))}</div>")
    filename = "目标控制时间变化.html"
    title = "目标位号与控制位号时间变化"
    write_page(out_dir / filename, title, "".join(parts))


def correlation_page(records: Sequence[Dict[str, Any]], target: str, controls: Sequence[str], out_dir: Path, max_points: int, locale: str) -> None:
    all_tags = [target] + [tag for tag in controls if tag != target]
    parts = [
        "<p class='muted'>相关性基于全量配对数据计算；散点图仅在显示层面抽样。相关性用于筛查共变关系，不代表因果。</p>",
        tag_reference_table(records[0], all_tags),
    ]
    first_plot = True
    for record in records:
        tags = [target] + [tag for tag in controls if tag in record["df"].columns and tag != target]
        df = record["df"][tags].dropna(how="all")
        corr = df.corr(numeric_only=True)
        labels = [heatmap_axis_label(record, tag) for tag in corr.columns]
        hover_labels = [heatmap_hover_label(record, tag) for tag in corr.columns]
        customdata = [
            [[hover_labels[row_idx], hover_labels[col_idx]] for col_idx in range(len(hover_labels))]
            for row_idx in range(len(hover_labels))
        ]
        fig_heat = go.Figure(
            go.Heatmap(
                z=corr.values,
                x=labels,
                y=labels,
                customdata=customdata,
                zmin=-1,
                zmax=1,
                colorscale="RdBu",
                reversescale=True,
                hoverlabel={"align": "left", "font_size": 12},
                hovertemplate="横轴=%{customdata[1]}<br>纵轴=%{customdata[0]}<br>相关系数=%{z:.4f}<extra></extra>",
            )
        )
        fig_heat.update_layout(title=f"{readable_name(record['name'], 42)}：相关性热力图")
        apply_readable_layout(fig_heat, height=760, top=110, bottom=150, left=150, right=80, legend=False)

        rows: List[List[str]] = []
        bars: List[Tuple[str, str, float]] = []
        for tag in tags:
            if tag == target:
                continue
            pair = record["df"][[target, tag]].dropna()
            value = pair[target].corr(pair[tag]) if len(pair) >= 3 and pair[tag].std() != 0 else np.nan
            rows.append([tag, tag_description(record, tag), f"{len(pair):,}", format_num(value)])
            if not pd.isna(value):
                bars.append((tag, tag_description(record, tag), float(value)))
        bars.sort(key=lambda item: abs(item[2]), reverse=True)

        fig_bar = go.Figure(
            go.Bar(
                x=[value for _, _, value in bars],
                y=[f"{tag}<br>{wrap_for_axis(desc, 14, 14)}" for tag, desc, _ in bars],
                orientation="h",
                marker_color=["#2a9d8f" if value >= 0 else "#d95f45" for _, _, value in bars],
                hovertemplate="%{y}<br>corr=%{x:.4f}<extra></extra>",
            )
        )
        fig_bar.update_layout(title=f"{readable_name(record['name'], 42)}：控制位号与目标位号相关性", xaxis_title="Pearson 相关系数", yaxis={"autorange": "reversed"})
        apply_readable_layout(fig_bar, height=max(520, 34 * len(bars) + 180), top=105, bottom=95, left=190, right=80, legend=False)

        scatter_titles = [shorten_text(f"{tag} {desc}", 34) for tag, desc, _ in bars[:4]]
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
                    name=f"{shorten_text(label(record, tag), 20)} corr={value:.3f}",
                    hovertemplate=f"{esc(label(record, tag))}=%{{x:.4f}}<br>{esc(label(record, target))}=%{{y:.4f}}<extra></extra>",
                ),
                row=row,
                col=col,
            )
        fig_scatter.update_layout(title=f"{readable_name(record['name'], 42)}：相关性最高的控制位号散点图", showlegend=False)
        apply_readable_layout(fig_scatter, height=760, top=125, bottom=95, left=95, right=70, legend=False)
        fig_scatter.update_annotations(font_size=12)
        parts.append(
            f"<div class='panel'><h2>{esc(record['name'])}</h2>"
            + fig_html(fig_heat, include_plotlyjs=first_plot)
            + fig_html(fig_bar)
            + fig_html(fig_scatter)
            + "<h2>相关性明细</h2>"
            + table(["控制位号", "描述", "配对样本", "相关系数"], rows)
            + "</div>"
        )
        first_plot = False
    filename = "目标控制相关性分析.html"
    title = "目标位号与控制位号相关性分析"
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
    first_record = records[0]
    tags = [target] + [tag for tag in controls if tag != target]
    schema_rows = [
        [
            tag,
            tag_description(first_record, tag),
            first_record.get("schema_map", {}).get(tag, tag_description(first_record, tag)),
        ]
        for tag in tags
    ]
    controls_html = "".join(
        f"<li>{esc(label(first_record, tag))}</li>"
        for tag in controls
    )
    links = [
        ("统计分布", "目标控制统计分布.html"),
        ("时间变化", "目标控制时间变化.html"),
        ("相关性分析", "目标控制相关性分析.html"),
    ]
    link_html = "".join(f"<li><a href='{esc(href)}'>{esc(text)}</a></li>" for text, href in links)
    body = f"""
<div class="panel">
  <h2>目标位号</h2>
  <p><b>{esc(label(first_record, target))}</b></p>
  <p class="muted">控制位号：</p>
  <ul>{controls_html}</ul>
</div>
<div class="panel"><h2>报告入口</h2><ul>{link_html}</ul></div>
<div class="panel"><h2>位号释义 / Schema</h2>{table(["位号", "中文释义", "Schema 注释"], schema_rows)}</div>
<div class="panel"><h2>数据概览</h2>{table(["文件", "行数", "开始时间", "结束时间", "目标有效", "目标缺失", "均值", "标准差"], rows)}</div>
"""
    title = "目标控制 Plotly EDA"
    write_page(out_dir / "报告入口.html", title, body)


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
            "聚合文件": agg_overlap,
            "原始数据": raw_overlap,
            f"原始数据->{rule}_last": raw_overlap.resample(rule).last().dropna(),
            f"原始数据->{rule}_mean": raw_overlap.resample(rule).mean().dropna(),
            f"原始数据->{rule}_median": raw_overlap.resample(rule).median().dropna(),
        }
        fig = go.Figure()
        rows = []
        for name, values in variants.items():
            fig.add_trace(go.Box(y=sample_series(values, 50000), name=shorten_text(name, 24), boxpoints="outliers", boxmean="sd"))
            rows.append([name] + stats_row(values))
        fig.update_layout(title=f"{readable_name(pair.get('label', raw['name']), 42)}：降采样影响", boxmode="group")
        apply_readable_layout(fig, height=720, top=110, bottom=145, left=95, right=80)
        parts.append(
            f"<div class='panel'><h2>{esc(pair.get('label', raw['name']))}</h2>"
            + fig_html(fig, include_plotlyjs=first)
            + table(["口径", "有效", "缺失", "均值", "标准差", "P05", "Q1", "P50", "Q3", "P95", "最小", "最大"], rows)
            + "</div>"
        )
        first = False
    if parts:
        title = "降采样影响分析"
        write_page(out_dir / "降采样影响分析.html", title, "".join(parts))


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
        if args.target not in raw_df.columns:
            print(f"Warning: target {args.target} missing in {path.name}; skipping file", file=sys.stderr)
            continue
        try:
            time_col = resolve_time_column(raw_df, args.time_col)
        except SystemExit as exc:
            print(f"Warning: {exc}; skipping {path.name}", file=sys.stderr)
            continue
        df = numeric_frame(raw_df, time_col, tags)
        field_map = {tag: description_for(metadata, tag, csv_field_map.get(tag, tag)) for tag in tags}
        schema_map = {tag: schema_note_for(metadata, tag, csv_field_map.get(tag, tag)) for tag in tags}
        records.append({"name": path.stem, "path": path, "df": df, "field_map": field_map, "schema_map": schema_map})

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
