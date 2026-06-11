---
name: target-control-eda
description: Use when analyzing industrial process data with a target tag and control tags, especially FDE/IIDF-style Parquet, CSV, or Excel files that need first-pass interactive Plotly EDA.
---

# Target Control EDA

Use this skill to produce the first visual inspection pack for a process-control scenario: target tag distribution, target/control time trends, and full-data correlation analysis.

## Required Inputs

- Data files or a data directory. Prefer FDE/IIDF-style Parquet; CSV and Excel are supported.
- A target tag. Do not silently guess it when the user has not confirmed it.
- Control tags. If not supplied, inspect columns and metadata first, then ask the user to confirm.
- A time column. Default candidates are `timestamp`, `ts`, `time`, and `datetime`.

IIDF minimum contract used by this skill:

- Internal data is Parquet when available.
- CSV/Excel can be read and treated with the same semantics.
- Time is held in one timestamp column, typically `timestamp`.
- Tag columns are data columns; optional metadata can provide descriptions, units, limits, and tag roles.

## Workflow

1. Locate files and inspect schema before plotting.
2. Confirm the target tag and control tags if the user did not provide them explicitly.
3. Run the bundled script:

```bash
python scripts/generate_target_control_eda.py \
  --data-dir /path/to/data \
  --target TARGET_TAG \
  --controls TAG_A,TAG_B,TAG_C \
  --time-col timestamp \
  --output-dir /path/to/plotly_eda_outputs
```

4. Open `index.html` and guide the user through the three primary reports:
   - `target_control_distribution.html`
   - `target_control_time.html`
   - `target_control_correlation.html`

Use `--locale zh` when Chinese page titles are preferred.

## Script Options

- `--data-dir`: directory containing data files.
- `--files`: explicit file list; comma-separated or repeated.
- `--target`: required target tag.
- `--controls`: comma-separated control tags.
- `--time-col`: timestamp column name; auto-detected when omitted.
- `--metadata`: optional JSON metadata file for tag descriptions and units.
- `--output-dir`: output directory for standalone HTML reports.
- `--format`: `auto`, `iidf`, `two-row-csv`, `csv`, `excel`, or `parquet`.
- `--max-points-display`: display sampling cap; statistics and correlations remain full-data.
- `--sampling-pairs`: optional JSON config for raw-vs-aggregated sampling impact analysis.
- `--locale`: `zh` or `en`.

## Visualization Rules

- Do not use Plotly WebGL traces. Use `go.Scatter`, not `go.Scattergl`.
- Keep statistics and correlations on full available data unless the user explicitly asks otherwise.
- Sampling is only for rendering large time-series and scatter plots.
- Distribution pages should combine horizontal distribution overview with key metrics and full summary tables.
- Correlation is a screening view, not causal evidence. Mention lag, state segmentation, and process constraints when interpreting results.

## Dependencies

The script requires Python packages: `pandas`, `numpy`, and `plotly`. For all supported formats, add `pyarrow` and `openpyxl`.

Prefer project-local dependencies:

```bash
python -m pip install --target .python_deps pandas numpy plotly pyarrow openpyxl
```

The script automatically adds `.python_deps` from the current working directory and the skill directory to `sys.path`.

