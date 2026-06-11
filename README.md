# claude-usage-monitor

Self-monitor Claude Code usage against your paid plan windows, forecast whether
you're on pace to exceed a window before it resets, and (in a later phase)
manage model/effort based on remaining budget.

## Why

Claude Code's plan limits are *rolling windows* (5-hour and 7-day on Pro/Max),
but there's no built-in "you're on pace to run out by Thursday" warning. This
tool fills that gap by reading the data Claude Code already pushes to the
statusline on every update and turning it into a live burn-rate forecast.

## How it works (Phase 1)

Claude Code pipes a JSON payload to your configured **statusline command** on
every status update. On Pro/Max that payload includes `rate_limits.five_hour`
and `rate_limits.seven_day`, each with `used_percentage` and `resets_at`. On
each tick this tool:

1. **Records** a usage sample to a local SQLite DB at `~/.claude/budget.db`.
2. **Forecasts** end-of-window usage for both the 5h and 7d windows with a
   budget-burndown model — cumulative usage divided by how far through the
   window you are:

   ```
   projected = used% ÷ fraction_of_window_elapsed
   ```

   This reflects where you are in the period relative to what you've spent,
   works from a single sample, and isn't thrown off by short-term spikes.
3. **Renders** a compact status line, e.g.:

   ```
   Opus  ·  ctx 8%  ·  5h 23%→proj 71% ✅  ·  7d 41%→proj 96% ⚠ (2d3h)  ·  → sonnet/medium
   ```

The `→ model/effort` segment is a **recommendation only** in Phase 1 — it does
not change anything. (Interactive Claude Code can't switch model/effort
mid-session; acting on the recommendation is a Phase 2 concern.)

### Privacy

This repo is public, so it ships **code and a config template only**. Your
actual usage data (`~/.claude/budget.db`) and any personal config override
(`~/.claude/budget.local.toml`) live outside the repo and are gitignored.

## Install

```bash
cd ~/claude-usage-monitor
uv sync
```

### Step 0 — confirm the statusline schema (recommended once)

The exact statusline field names are verified empirically. Point Claude Code's
statusline at the capture helper for a few turns:

```jsonc
// ~/.claude/settings.json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /home/josh/claude-usage-monitor/scripts/capture-statusline.py"
  }
}
```

Inspect `~/.claude/statusline-capture.jsonl` to confirm the fields (especially
whether `rate_limits` is present on your account tier), then switch to the real
status line below.

### Wire up the monitor

```jsonc
// ~/.claude/settings.json
{
  "statusLine": {
    "type": "command",
    "command": "uv run --project /home/josh/claude-usage-monitor claude-usage-statusline"
  }
}
```

## Configure

Defaults live in `config/budget.toml`. To customize without committing personal
values, copy it to `~/.claude/budget.local.toml` and edit there (gitignored,
takes precedence).

## Develop / test

```bash
uv run pytest

# Smoke-test the status line with a saved payload:
cat tests/sample_payload.json | uv run claude-usage-statusline
```

## Dashboard

A graphical dashboard shows six views — **time, pace, usage, models, effort,
conversations** — combining the live `budget.db` time-series with conversation
metadata parsed from your Claude Code transcripts.

```bash
# One-time (and any time after): build/refresh the transcript index.
uv run claude-usage-index

# Launch the auto-refreshing dashboard (installs the optional extra).
uv sync --extra dashboard
uv run --extra dashboard claude-usage-dashboard   # -> http://localhost:8501
```

How the data is sourced:
- **Pace / usage / effort / time** come from `~/.claude/budget.db` (the
  statusline monitor) — so these are sparse until the monitor has been running.
- **Conversations / models** are parsed from `~/.claude/projects/*/*.jsonl`
  (full history: titles, durations, message counts, models). Token counts there
  are unreliable and are never surfaced as cost.
- **Per-session cost** comes from `statusline-capture.jsonl`, so it only exists
  for sessions the monitor observed.

The index lives in `~/.claude/usage-index.db` (separate from `budget.db`, never
committed). Re-indexing is incremental — only changed/new transcripts are
re-parsed — and the dashboard keeps it warm automatically.

## Roadmap

- **Phase 1:** monitor + pace forecast + recommendation seam (done).
- **Dashboard (this):** six-view Streamlit dashboard over the index + budget.db.
- **Phase 2:** act on the recommendation — a preflight launcher that sets
  model/effort before `claude` starts, and/or a Claude Agent SDK harness for
  fine-grained per-task control.
- **Later:** optional OpenTelemetry backbone for cross-session history.
