"""Data Contract Playground - a local Streamlit dashboard over datacontract-cli."""
from __future__ import annotations

import csv
import importlib.metadata
import itertools
import os
import re
import shlex
from pathlib import Path

import streamlit as st
import yaml
from streamlit_monaco import st_monaco

from playground import catalogue, runner
from playground.fixtures import ensure_fixtures

ROOT = Path(__file__).resolve().parent
CONTRACTS_DIR = ROOT / "contracts"
CATALOG_DIR = ROOT / "catalog"


def _release_info() -> dict[str, str]:
    """App and key dependency versions, for the About page."""
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'(?m)^version = "([^"]+)"', text)
        app_version = match.group(1) if match else "unknown"
    except OSError:
        app_version = "unknown"
    try:
        cli_version = importlib.metadata.version("datacontract-cli")
    except importlib.metadata.PackageNotFoundError:
        cli_version = "unknown"
    return {"datacontract-playground": app_version, "datacontract-cli": cli_version,
            "streamlit": st.__version__}


RELEASE = _release_info()


def _auth_enabled() -> bool:
    """True when [auth] is configured (e.g. Community Cloud secrets). Runs
    without it (local dev) keep full access under the localhost model."""
    try:
        return bool(st.secrets.get("auth"))
    except Exception:  # secrets missing/unparseable: treat as unconfigured
        return False


def _access_granted(is_logged_in: bool, email: str | None,
                    allowed_emails: list[str]) -> bool:
    """Write/execute access: logged in, and on the allowlist when one is set."""
    if not is_logged_in:
        return False
    if not allowed_emails:
        return True
    return (email or "").strip().lower() in {a.strip().lower()
                                             for a in allowed_emails}


def _editing_enabled() -> bool:
    """Buffer editing and the Test page SQL filters unlock only when explicitly
    enabled (env PLAYGROUND_ALLOW_EDITING=1, or [playground] allow_editing =
    true in secrets). Default off, so a deployed instance can never modify
    contracts or run arbitrary SQL by accident."""
    if os.environ.get("PLAYGROUND_ALLOW_EDITING", "").lower() in ("1", "true", "yes"):
        return True
    try:
        return bool(st.secrets.get("playground", {}).get("allow_editing", False))
    except Exception:  # secrets missing/unparseable: stay read-only
        return False

st.set_page_config(page_title="Data Contract Playground", layout="wide")

# Make sure fixture data exists (deterministic, cheap no-op when present).
ensure_fixtures(CONTRACTS_DIR)
# Write sanitised catalog pages for static serving (no-op when up to date).
catalogue.ensure_static_catalog(CATALOG_DIR, ROOT / "static" / "catalog")

NAV_PAGES = ["Explore", "Test", "Export", "Catalog"]

EXPORT_GROUPS = {
    "documentation": ["html", "markdown", "mermaid"],
    "schema": ["jsonschema", "avro", "protobuf", "sql"],
    "dbt": ["dbt-models", "dbt-sources", "dbt-staging-sql"],
    "interchange": ["odcs", "dcs"],
    "quality": ["sodacl", "great-expectations"],
    "code": ["pydantic-model", "go", "spark"],
}
SQL_DIALECTS = ["snowflake", "bigquery", "postgres", "redshift", "databricks", "duckdb"]

# Display-only badge for meta.yaml's expected result (short form); the meta
# values themselves keep the CLI vocabulary (passed/failed/warning) because
# the smoke tests compare them against run.result.
RESULT_BADGE = {
    "passed": ":green[pass]",
    "failed": ":red[fail]",
    "warning": ":orange[warn]",
    "error": ":red[error]",
    "info": ":blue[info]",
}

# datacontract-cli 1.1 check categories (older CLI versions used
# properties/quality/slaProperties/custom).
CHECK_CATEGORIES = ["schema", "quality", "servicelevel", "custom"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _scan(contracts_dir: str):
    return catalogue.scan(Path(contracts_dir))


def _rel(path: Path) -> str:
    """Path relative to the repo root, for display and state keys."""
    return str(Path(path).relative_to(ROOT))


def _buffer_key(path: Path) -> str:
    return f"yaml_buffer::{_rel(path)}"


def get_buffer(path: Path) -> str:
    """Current YAML buffer for a contract file (session state), seeded from disk."""
    key = _buffer_key(path)
    if key not in st.session_state:
        st.session_state[key] = Path(path).read_text(encoding="utf-8")
    return st.session_state[key]


def is_modified(path: Path) -> bool:
    """True when the saved buffer differs from the file on disk."""
    key = _buffer_key(path)
    if key not in st.session_state:
        return False
    return st.session_state[key] != Path(path).read_text(encoding="utf-8")


def _clear_run_state() -> None:
    for key in ("last_run", "last_run_json"):
        st.session_state.pop(key, None)


def _banner(result: str) -> None:
    if result == "passed":
        st.success(f"Result: {result}")
    elif result == "warning":
        st.warning(f"Result: {result}")
    elif result in ("failed", "error"):
        st.error(f"Result: {result}")
    else:
        st.info(f"Result: {result}")


def _checks_dataframe(run_dump: dict) -> None:
    checks = run_dump.get("checks") or []
    if not checks:
        st.info("No checks in this run.")
        return
    st.dataframe(checks, width="stretch")


def _logs_expander(run_dump: dict) -> None:
    with st.expander("Logs"):
        logs = run_dump.get("logs") or []
        if logs:
            st.code("\n".join(str(line) for line in logs))
        else:
            st.caption("No logs.")


def _preview_data_file(path: Path, limit: int = 20) -> None:
    """20-row preview: parquet via duckdb, csv via the csv module."""
    if path.suffix == ".parquet":
        import duckdb
        df = duckdb.sql(
            f"SELECT * FROM read_parquet(?) LIMIT {int(limit)}",
            params=[str(path)],
        ).df()
        st.dataframe(df, width="stretch")
    elif path.suffix == ".csv":
        with open(path, newline="", encoding="utf-8") as fh:
            # incremental read capped at limit+1 rows (header + limit data rows)
            rows = list(itertools.islice(csv.reader(fh), limit + 1))
        st.dataframe(rows[1 : limit + 1], width="stretch")
        st.caption("First column header: " + ", ".join(rows[0]) if rows else "")
    else:
        st.caption(f"No preview available for {path.name}")


def _server_names(yaml_str: str) -> list[str]:
    try:
        doc = yaml.safe_load(yaml_str) or {}
        return [s.get("server") for s in doc.get("servers") or [] if s.get("server")]
    except yaml.YAMLError:
        return []


def _lint_results(state_key: str) -> None:
    """Show the stored lint result for *state_key* (banner, checks, logs)."""
    dump = st.session_state.get(state_key)
    if dump:
        _banner(dump.get("result", "info"))
        checks = dump.get("checks") or []
        if checks:
            st.dataframe(checks, width="stretch")
        else:
            st.info("No lint findings.")
        _logs_expander(dump)


def _validate_controls(path: Path) -> None:
    """Validate button + all_errors toggle, and the stored lint result for *path*."""
    v1, v2, _ = st.columns([1, 1, 4], vertical_alignment="bottom")
    with v1:
        run_lint = st.button("Validate", key="explore::validate", width="stretch",
                             help="Lint the current buffer")
    with v2:
        all_errors = st.checkbox("all_errors", value=False,
                                 key="explore::all_errors",
                                 help="Collect every lint issue")
    lint_key = f"lint::{_rel(path)}"
    if run_lint:
        try:
            run = runner.lint(get_buffer(path), all_errors=all_errors,
                              base_dir=Path(path).parent)
            st.session_state[lint_key] = run.model_dump()
        except Exception as exc:
            st.session_state.pop(lint_key, None)
            st.error(f"Lint crashed: {exc}")
    _lint_results(lint_key)


def _changelog_section(entry: catalogue.Entry) -> None:
    """Changelog UI for an entry; shown on Explore when meta sets changelog: true."""
    change_options: dict[str, tuple[str, Path | None]] = {
        "Current buffer": (get_buffer(entry.main_yaml), entry.dir)}
    change_options[entry.main_yaml.name] = (catalogue.load_yaml(entry), entry.dir)
    for label, path in entry.extra_yamls.items():
        change_options[path.name] = (path.read_text(encoding="utf-8"), entry.dir)
    names = list(change_options.keys())
    c1, c2 = st.columns(2)
    with c1:
        name_a = st.selectbox(
            "Contract A (old)", names,
            index=names.index(entry.main_yaml.name)
            if entry.main_yaml.name in names else 0)
    with c2:
        name_b = st.selectbox("Contract B (new)", names,
                              index=1 if len(names) > 1 else 0)
    a_file = entry.main_yaml.name if name_a == "Current buffer" else name_a
    b_file = entry.main_yaml.name if name_b == "Current buffer" else name_b
    st.caption("CLI command ([docs](https://docs.datacontract.com/commands/changelog))")
    st.code(f"datacontract changelog contracts/{entry.slug}/{a_file} "
            f"contracts/{entry.slug}/{b_file}", language="bash")
    if st.button("Compute changelog", type="primary"):
        try:
            yaml_a, dir_a = change_options[name_a]
            yaml_b, dir_b = change_options[name_b]
            result = runner.changelog(yaml_a, yaml_b, base_dir_a=dir_a,
                                      base_dir_b=dir_b)
            st.session_state["last_changelog"] = result
        except Exception as exc:
            st.session_state.pop("last_changelog", None)
            st.error(f"Changelog failed: {exc}")
    result = st.session_state.get("last_changelog")
    if result is not None:
        # datacontract-cli 1.1 ChangelogResult has no severity tiers
        # (diff/breaking were removed); entries are typed added/removed/updated,
        # so we group by change type instead.
        dump = result.model_dump() if hasattr(result, "model_dump") else result
        entries_list = dump.get("entries") or []
        if not entries_list:
            st.info("No changelog entries.")
        else:
            groups: dict[str, list] = {}
            for e in entries_list:
                groups.setdefault(str(e.get("type", "updated")), []).append(e)
            for change_type in ["removed", "updated", "added"]:
                group_entries = groups.pop(change_type, [])
                if not group_entries:
                    continue
                st.subheader(change_type.capitalize())
                for e in group_entries:
                    text = f"- `{e.get('path', '')}`"
                    if e.get("old_value") is not None or e.get("new_value") is not None:
                        text += f": {e.get('old_value')} → {e.get('new_value')}"
                    st.markdown(text)
            for change_type, group_entries in groups.items():
                st.subheader(change_type.capitalize())
                for e in group_entries:
                    st.markdown(f"- `{e.get('path', '')}`")
        with st.expander("Raw changelog"):
            st.json(dump)


# ---------------------------------------------------------------------------
# catalogue data + session-state callbacks
# ---------------------------------------------------------------------------

entries = _scan(str(CONTRACTS_DIR))
if not entries:
    st.error(f"No contracts found under {CONTRACTS_DIR}")
    st.stop()

titles = [f"{i}. {e.title}" for i, e in enumerate(entries, 1)]
entry_by_title = dict(zip(titles, entries))

def _on_contract_pick() -> None:
    """Sidebar contract picker changed: reset Explore file state, mirror in URL."""
    st.session_state.pop("explore_entry_file", None)
    _clear_run_state()
    st.query_params["contract"] = entry_by_title[
        st.session_state["selected_contract"]].slug


st.session_state.setdefault("selected_contract", titles[0])

# Deep links: ?page=<nav page> and ?contract=<slug> seed the navigation state.
title_by_slug = {e.slug: t for e, t in zip(entries, titles)}
_qp_page = st.query_params.get("page")
if _qp_page in NAV_PAGES:
    st.session_state["nav_page"] = _qp_page
_qp_contract = st.query_params.get("contract")
if _qp_contract in title_by_slug:
    st.session_state["selected_contract"] = title_by_slug[_qp_contract]

# ---------------------------------------------------------------------------
# sidebar: nav menu, contract picker, selected-contract info box
# ---------------------------------------------------------------------------

def _on_nav_change() -> None:
    """Mirror the nav choice in the URL (clears the ?page=About override)."""
    st.query_params["page"] = st.session_state["nav_page"]


st.sidebar.markdown(
    '<a href="?page=About" target="_self" '
    'style="color: inherit; text-decoration: none;">'
    "<h1>Data Contract Playground</h1></a>",
    unsafe_allow_html=True,
)
page = st.sidebar.radio("Navigate", NAV_PAGES, key="nav_page",
                        on_change=_on_nav_change)
if st.query_params.get("page") == "About":
    page = "About"

editing_flag = _editing_enabled()
if _auth_enabled():
    allowed_emails = list(st.secrets.get("access", {}).get("allowed_emails", []))
    full_access = _access_granted(st.user.is_logged_in,
                                  getattr(st.user, "email", None),
                                  allowed_emails)
    if st.user.is_logged_in:
        who = getattr(st.user, "name", None) or getattr(st.user, "email", "account")
        note = "" if full_access else " (read-only: not on the allowlist)"
        st.sidebar.caption(f"Signed in as {who}{note}")
        st.sidebar.button("Log out", on_click=st.logout)
    else:
        st.sidebar.caption("Read-only mode - log in for edit and test."
                           if editing_flag else "Read-only mode.")
        st.sidebar.button("Log in", on_click=st.login)
else:
    full_access = True  # local run without [auth]: localhost model
# Edit buffer and test filters need both the explicit unlock and access.
editing = editing_flag and full_access
selected_title = st.sidebar.selectbox(
    "Contract", titles, key="selected_contract", on_change=_on_contract_pick)
entry = entry_by_title[selected_title]
main_rel = _rel(entry.main_yaml)
modified = is_modified(entry.main_yaml)

st.sidebar.divider()
with st.sidebar.container(border=True):
    st.markdown(f"**{entry.title}**")
    st.caption(f"`{main_rel}`")
    if entry.expected:
        st.markdown(f"Expected: {RESULT_BADGE.get(entry.expected, entry.expected)}")
    st.caption(f"Difficulty: {entry.difficulty}"
               + (f" · data: {entry.data_format}" if entry.data_format else ""))
    if entry.tags:
        st.caption(" ".join(f"`{t}`" for t in entry.tags))
    if modified:
        st.warning("modified: buffer differs from disk")
    else:
        st.caption("in sync with disk")

buffer = get_buffer(entry.main_yaml)

# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

if page == "Explore":
    st.header("Explore")
    cur_entry = entry  # the contract picked in the sidebar
    cur_files = ([entry.main_yaml]
                 + [p for _, p in sorted(entry.extra_yamls.items())])

    file_names = [p.name for p in cur_files]
    if st.session_state.get("explore_entry_file") not in file_names:
        st.session_state["explore_entry_file"] = entry.main_yaml.name
    cur_path = next(p for p in cur_files
                    if p.name == st.session_state["explore_entry_file"])
    cur_rel = _rel(cur_path)
    is_main = cur_path == entry.main_yaml

    fcol, ecol = st.columns([5, 1], vertical_alignment="bottom")
    with fcol:
        st.markdown(f"**File:** `{cur_rel}`")
    with ecol:
        if editing:
            edit_mode = st.toggle("Edit", key="explore_edit")
        else:
            edit_mode = False  # read-only: no buffer editing

    if len(cur_files) > 1:
        st.selectbox("File", file_names, key="explore_entry_file",
                     label_visibility="collapsed")

    if not edit_mode:
        if is_main:
            st.subheader(cur_entry.title)
            st.write(cur_entry.summary)
            cols = st.columns([3, 1])
            with cols[0]:
                if cur_entry.tags:
                    st.write(" ".join(f"`{t}`" for t in cur_entry.tags))
            with cols[1]:
                if cur_entry.expected:
                    st.markdown(
                        f"Expected: {RESULT_BADGE.get(cur_entry.expected, cur_entry.expected)}")
            if cur_entry.notes:
                st.info(cur_entry.notes)
        else:
            st.caption(
                f"Additional contract file of **{cur_entry.title}** "
                f"(main contract: `{cur_entry.main_yaml.name}`)."
            )
        if is_modified(cur_path):
            st.caption(":orange[modified - showing the saved buffer, not the file on disk]")
        st.code(get_buffer(cur_path), language="yaml")
        _validate_controls(cur_path)

        st.divider()
        st.subheader("Data files")
        if not cur_entry.data_files:
            st.caption("No data files.")
        for path in cur_entry.data_files:
            st.markdown(f"**{path.name}**")
            try:
                _preview_data_file(path)
            except Exception as exc:  # preview is best-effort
                st.error(f"Preview failed: {exc}")

        if cur_entry.changelog:
            st.divider()
            st.subheader("Changelog")
            _changelog_section(cur_entry)

    else:
        st.caption(
            "Validate, Test, Export and Changelog operate on the buffer "
            "saved here, not the file on disk."
        )
        if not is_main:
            st.caption(
                f"Additional contract file of **{cur_entry.title}** - Test, "
                f"Export and Changelog use the main contract "
                f"(`{cur_entry.main_yaml.name}`)."
            )
        cur_buffer = get_buffer(cur_path)
        edited = st_monaco(value=cur_buffer, language="yaml", height="600px")
        if edited is None:  # before the frontend component reports back
            edited = cur_buffer
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save to buffer", type="primary"):
                st.session_state[_buffer_key(cur_path)] = edited
                st.success("Buffer saved.")
        with c2:
            if st.button("Revert to file"):
                st.session_state[_buffer_key(cur_path)] = Path(cur_path).read_text(
                    encoding="utf-8")
                st.rerun()
        _validate_controls(cur_path)

elif page == "Test":
    st.header("Test")
    fcol, ecol = st.columns([5, 1], vertical_alignment="bottom")
    with fcol:
        st.markdown(f"**File:** `{main_rel}`")
    with ecol:
        if entry.expected:
            st.markdown(
                f"Expected: {RESULT_BADGE.get(entry.expected, entry.expected)}")
    if modified:
        st.caption(":orange[modified buffer (unsaved changes)]")
    st.caption(
        "`type: sql` quality rules are executed by DuckDB as the local user "
        "and can read files and make network calls - only test contracts you "
        "have reviewed."
    )
    servers = _server_names(buffer)
    if len(servers) > 1:
        server = st.selectbox("Server", servers)
    else:
        server = servers[0] if servers else None
        if server:
            st.caption(f"server: {server}")
    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    with c1:
        categories = st.multiselect("Check categories", CHECK_CATEGORIES)
    with c2:
        metadata_only = st.checkbox(
            "metadata_only", value=False,
            help="Run only checks that read the schema, not row values")
    with c3:
        include_failed_samples = st.checkbox(
            "include_failed_samples", value=False,
            help="Collect rows that failed each check - may display raw "
                 "data rows from the dataset")
    # Free-text filters (row filter is a raw SQL predicate) only when unlocked.
    dimension = tag = quality_id = row_filter = ""
    if editing:
        with st.expander("More filters"):
            f1, f2 = st.columns(2)
            with f1:
                dimension = st.text_input("Dimension", value="")
                tag = st.text_input("Tag", value="")
            with f2:
                quality_id = st.text_input("Quality id", value="")
                row_filter = st.text_input(
                    "Row filter", value="", placeholder="total_amount >= 0",
                    help="SQL predicate filtering the rows under test "
                         "(single-schema contracts)")
    test_cmd = [f"cd contracts/{entry.slug} && datacontract test",
                entry.main_yaml.name]
    if server:
        test_cmd += ["--server", server]
    if categories:
        test_cmd += ["--checks", ",".join(categories)]
    if dimension:
        test_cmd += ["--dimension", dimension]
    if quality_id:
        test_cmd += ["--quality-id", quality_id]
    if tag:
        test_cmd += ["--tag", tag]
    if row_filter:
        test_cmd += ["--filter", shlex.quote(row_filter)]
    if metadata_only:
        test_cmd.append("--metadata-only")
    if include_failed_samples:
        test_cmd.append("--include-failed-samples")
    st.caption("CLI command ([docs](https://docs.datacontract.com/commands/test))")
    st.code(" ".join(test_cmd), language="bash")
    if not full_access:
        st.caption("Log in to run tests - execution is restricted to "
                   "allowlisted users.")
    run_tests = st.button("Run tests", type="primary", disabled=not full_access)
    if run_tests and full_access:  # enforcement: never run without access
        try:
            run = runner.test(
                buffer,
                server=server,
                checks=categories or None,
                dimension=dimension or None,
                quality_id=quality_id or None,
                tag=tag or None,
                row_filter=row_filter or None,
                include_failed_samples=include_failed_samples,
                metadata_only=metadata_only,
                base_dir=entry.dir,
            )
            st.session_state["last_run"] = run.model_dump()
            st.session_state["last_run_json"] = run.model_dump_json(indent=2)
        except Exception as exc:
            st.error(f"Test crashed: {exc}")
    dump = st.session_state.get("last_run")
    if dump:
        checks = dump.get("checks") or []
        counts: dict[str, int] = {}
        for chk in checks:
            counts[chk.get("result", "unknown")] = counts.get(chk.get("result", "unknown"), 0) + 1
        mcols = st.columns(4)
        for col, name in zip(mcols, ["passed", "failed", "warning", "error"]):
            col.metric(name.capitalize(), counts.get(name, 0))
        _banner(dump.get("result", "info"))
        st.divider()
        st.subheader("Checks")
        if not checks:
            st.info("No checks in this run.")
        for chk in checks:
            label = f"[{chk.get('result')}] {chk.get('name') or chk.get('type') or chk.get('key')}"
            with st.expander(label):
                st.dataframe([{k: v for k, v in chk.items()
                               if k not in ("implementation", "diagnostics",
                                            "reason", "failedSamples")}],
                             width="stretch")
                if chk.get("reason"):
                    st.markdown("**Reason**")
                    st.code(str(chk["reason"]))
                if chk.get("implementation"):
                    st.markdown("**Implementation (SQL)**")
                    st.code(str(chk["implementation"]), language="sql")
                if chk.get("diagnostics"):
                    st.markdown("**Diagnostics**")
                    st.json(chk["diagnostics"])
                if chk.get("failedSamples"):
                    st.markdown("**Failed samples**")
                    st.dataframe(chk["failedSamples"], width="stretch")
        st.download_button("Download run JSON",
                           data=st.session_state.get("last_run_json", "{}"),
                           file_name=f"{entry.slug}-run.json",
                           mime="application/json")
        _logs_expander(dump)

elif page == "Export":
    st.header("Export")
    st.markdown(f"**File:** `{main_rel}`")
    group = st.selectbox("Format group", list(EXPORT_GROUPS.keys()))
    fmt = st.selectbox("Format", EXPORT_GROUPS[group])
    dialect = None
    if fmt == "sql":
        dialect = st.selectbox("SQL dialect", SQL_DIALECTS)
    export_cmd = f"datacontract export {fmt} {main_rel}"
    if dialect:
        export_cmd += f" --dialect {dialect}"
    st.caption("CLI command ([docs](https://docs.datacontract.com/commands/export))")
    st.code(export_cmd, language="bash")
    if st.button("Run export", type="primary"):
        try:
            kwargs = {}
            if dialect:
                kwargs["sql_server_type"] = dialect
            out = runner.export(buffer, fmt, base_dir=entry.dir, **kwargs)
            st.session_state["last_export"] = out
            st.session_state["last_export_fmt"] = fmt
        except Exception as exc:
            st.session_state.pop("last_export", None)
            st.error(f"Export failed: {exc}")
    out = st.session_state.get("last_export")
    if out is not None:
        fmt_done = st.session_state.get("last_export_fmt", "")
        lang = {
            "html": "html", "markdown": "markdown", "mermaid": "markdown",
            "jsonschema": "json", "avro": "json", "protobuf": "protobuf",
            "sql": "sql", "odcs": "yaml", "dcs": "yaml", "sodacl": "yaml",
            "great-expectations": "json", "pydantic-model": "python",
            "go": "go", "spark": "json",
        }.get(fmt_done)
        is_bytes = isinstance(out, (bytes, bytearray))
        text = out.decode("utf-8", errors="replace") if is_bytes else str(out)
        st.code(text, language=lang)
        st.download_button("Download export",
                           data=bytes(out) if is_bytes else text,
                           file_name=f"{entry.slug}.{fmt_done.replace('-', '_')}.txt")

elif page == "Catalog":
    st.header("Catalog")
    st.caption(
        "Prebuilt catalog of all contracts, generated with "
        "[`datacontract catalog`](https://docs.datacontract.com/commands/catalog) - "
        "click a contract in the index to open its page here."
    )
    static_index = ROOT / "static" / "catalog" / "catalog.html"
    if not static_index.is_file():
        st.info("No prebuilt catalog found (catalog/index.html is missing).")
    else:
        # srcdoc with a relative <base> into static/catalog/: clicks navigate
        # only the iframe, to real static files (see catalogue.with_static_base)
        st.iframe(
            catalogue.with_static_base(
                static_index.read_text(encoding="utf-8")),
            height=700,
        )

elif page == "About":
    st.header("Data Contract Playground")
    st.markdown(
        "A playground for data contracts using datacontract-cli, ODCS, and "
        "streamlit."
    )
    st.subheader("datacontract-cli")
    st.markdown(
        "The CLI and Python library this app wraps (lint, test, export, "
        "changelog, catalog). "
        "[Docs](https://docs.datacontract.com) · "
        "[GitHub](https://github.com/datacontract/datacontract-cli)"
    )
    st.subheader("ODCS")
    st.markdown(
        "The Open Data Contract Standard, the YAML format (v3.1) of every "
        "contract in the bundled catalogue. "
        "[Specification](https://bitol-io.github.io/open-data-contract-standard/v3.1.0/)"
    )
    st.subheader("Release info")
    st.code("\n".join(f"{k} {v}" for k, v in RELEASE.items()), language="text")
