# Research Data

Research datasets, derived feature tables, and quality inputs belong under this directory.

The runtime SQLite database remains at `data/cngn.db` because the engine and dashboard use that
path as local application state. Exported or derived research artifacts should be written here or
under `research/results/` instead of adding new top-level data folders.

This directory is ignored by default. Check in only small fixtures under `research/data/fixtures/`,
small summarized reports under `research/data/reports/`, and README files that document larger
local artifacts.
