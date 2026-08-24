# datacontract-playground

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://datacontract-playground.streamlit.app/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A playground for data contracts using [datacontract-cli](https://github.com/datacontract/datacontract-cli),
[ODCS](https://bitol-io.github.io/open-data-contract-standard/v3.1.0/) v3.1, and
[Streamlit](https://streamlit.io). Browse the bundled catalogue, lint contracts,
run tests against local fixtures, export to other formats, and compare versions.

## Run it

```bash
uv run streamlit run app.py
```

`uv` auto-syncs the environment; the app auto-generates missing data fixtures on
start. Open http://localhost:8501.

Run the test suite:

```bash
uv run pytest
```

## Catalogue

Pick a contract in the sidebar, then navigate: Explore / Test / Export / Catalog.

| # | Entry | What it demonstrates | Expected |
|---|---|---|---|
| 1 | Hello, Orders (`hello-orders`) | Minimal ODCS v3.1 contract: typed schema + `rowCount` + `duplicateCount(order_id)` library rules | passed |
| 2 | Quality Failures (`quality-failures`) | Failing `nullValues(email)` and `validValues(country)` rules, with diagnostics and failed rows | failed |
| 3 | SQL Custom Checks (`sql-custom-checks`) | `type: sql` quality rules with `${object}` / `${property}` placeholders over a CSV fixture | passed |
| 4 | Dimensions and Severity (`dimensions-and-severity`) | `dimension:` annotations, `severity: warning` downgrading a failing check, rule `tags` | warning |
| 5 | SLA Freshness (`sla-freshness`) | `slaProperties` freshness checked against `updated_at` | passed |
| 6 | Multi-Table (`multi-table`) | Two schema objects over two parquet files; try the mermaid / html / sql exports | passed |
| 7 | Changelog Pair (`changelog-pair`) | `contract.v1` vs `contract.v2`: renamed field, widened type, removed quality rule, added table; Changelog section on Explore | passed (v2) |

## Testing your own contracts locally

Buffer editing and the Test page's SQL filters are off by default, so a deployed
instance stays read-only. To unlock them for local use:

```bash
PLAYGROUND_ALLOW_EDITING=1 uv run streamlit run app.py
```

Then drop a folder into `contracts/` (an ODCS file, a `meta.yaml` with
`title, summary, tags, difficulty, expected, data_format, order`, and a `data/`
folder) and it appears in the sidebar picker.

**Check a contract before you test it.** `type: sql` quality rules are executed
by DuckDB as your local user, and DuckDB SQL can read files and make network
calls - running an untrusted contract is arbitrary code execution with your
permissions. Only test contracts you have reviewed. The app still rejects
non-local servers and any server path that resolves outside the contract
folder.
