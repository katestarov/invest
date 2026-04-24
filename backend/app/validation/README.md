# Validation And Diagnostics

This package contains reproducible research-style tooling for two goals:

- validating that the scoring model is stable, interpretable, and methodically consistent
- measuring non-functional characteristics such as response time, cache impact, retries, and degraded-mode behavior

## Scripts

- `app.validation.score_validation` - score stability, sensitivity analysis, missingness robustness, ranking stability, peer baseline robustness
- `app.validation.benchmark_diagnostics` - synthetic benchmark for response time, cache effect, degraded mode, retry and timeout settings

## How To Run

Run from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m app.validation.score_validation
.\.venv\Scripts\python.exe -m app.validation.benchmark_diagnostics
```

## Generated Artifacts

Artifacts are written to `backend/validation_outputs/`:

- `score_validation/`
- `benchmark_diagnostics/`

Each folder contains:

- `CSV` tables
- `JSON` summaries
- `SVG` charts
- `summary.md` with short conclusions

## Important Note

These scripts do not prove investment truth. They validate engineering robustness, explainability, and internal methodological consistency of the scoring model on controlled scenarios.
