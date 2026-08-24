"""UI smoke tests: every page renders, and the sidebar surfaces entry metadata."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
NAV_PAGES = ["Explore", "Test", "Export", "Catalog"]


def _run_all_pages() -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    for page in NAV_PAGES:
        at.radio(key="nav_page").set_value(page)
        at.run()
        assert not at.exception, f"{page} page raised: {at.exception}"
    return at


def test_all_pages_render():
    _run_all_pages()


def test_sidebar_shows_difficulty_and_data_format():
    # entries are sorted by meta order, so 1. Hello, Orders (beginner,
    # parquet) is the default selection on startup
    at = _run_all_pages()
    captions = [c.value for c in at.caption]
    assert any("Difficulty: beginner" in c and "data: parquet" in c
               for c in captions)


def test_changelog_section_only_when_meta_enables_it():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    assert "Compute changelog" not in [b.label for b in at.button]
    at.sidebar.selectbox(key="selected_contract").set_value(
        "7. Changelog Pair (v1 vs v2)")
    at.run()
    assert "Compute changelog" in [b.label for b in at.button]
    assert any("datacontract changelog" in c.value for c in at.code)


def test_sidebar_picker_is_numbered():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    options = at.sidebar.selectbox(key="selected_contract").options
    assert options[0] == "1. Hello, Orders"
    assert options[-1] == "7. Changelog Pair (v1 vs v2)"


def test_catalog_page_iframes_static_index():
    # the catalog index is embedded via srcdoc with a relative <base> into
    # static/catalog/ (written at startup); clicks stay inside the iframe
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.radio(key="nav_page").set_value("Catalog")
    at.run()
    assert not at.exception, at.exception
    assert (ROOT / "static" / "catalog" / "catalog.html").is_file()


def test_default_page_is_explore():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    assert at.radio(key="nav_page").value == "Explore"


def test_about_page_via_title_query_param():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    assert any("?page=About" in m.value for m in at.sidebar.markdown)
    at.query_params["page"] = "About"
    at.run()
    assert not at.exception, at.exception
    assert any("docs.datacontract.com" in m.value for m in at.markdown)
    assert any("open-data-contract-standard" in m.value for m in at.markdown)
    assert any("datacontract-playground" in c.value for c in at.code)


def test_url_contract_param_selects_contract():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.query_params["contract"] = "sla-freshness"
    at.run()
    assert at.sidebar.selectbox(key="selected_contract").value == "5. SLA Freshness"


def test_contract_picker_updates_url():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.sidebar.selectbox(key="selected_contract").set_value("3. SQL Custom Checks")
    at.run()
    assert "sql-custom-checks" in at.query_params.get("contract")


def test_nav_page_param_selects_page():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.query_params["page"] = "Export"
    at.run()
    assert at.radio(key="nav_page").value == "Export"


def test_nav_change_updates_url():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.radio(key="nav_page").set_value("Test")
    at.run()
    assert "Test" in at.query_params.get("page")


def test_access_granted_rules():
    from app import _access_granted
    assert _access_granted(True, "me@example.com", ["me@example.com"])
    assert _access_granted(True, " Me@Example.com ", ["me@example.com"])
    assert _access_granted(True, "anyone@example.com", [])
    assert not _access_granted(True, "other@example.com", ["me@example.com"])
    assert not _access_granted(True, None, ["me@example.com"])
    assert not _access_granted(False, "me@example.com", [])


def test_cli_command_shown_on_test_and_export():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.radio(key="nav_page").set_value("Test")
    at.run()
    assert any("datacontract test" in c.value for c in at.code)
    at.radio(key="nav_page").set_value("Export")
    at.run()
    assert any("datacontract export" in c.value for c in at.code)


def test_read_only_by_default(monkeypatch):
    # no unlock flag: no edit toggle, no SQL filters; premade tests still run
    monkeypatch.delenv("PLAYGROUND_ALLOW_EDITING", raising=False)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    assert "Edit" not in [t.label for t in at.toggle]
    at.radio(key="nav_page").set_value("Test")
    at.run()
    assert not any(i.label == "Row filter" for i in at.text_input)
    run_button = next(b for b in at.button if b.label == "Run tests")
    assert not run_button.disabled


def test_filters_render_when_editing_enabled(monkeypatch):
    monkeypatch.setenv("PLAYGROUND_ALLOW_EDITING", "1")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.radio(key="nav_page").set_value("Test")
    at.run()
    assert any(i.label == "Row filter" for i in at.text_input)


def test_explore_edit_mode_renders(monkeypatch):
    monkeypatch.setenv("PLAYGROUND_ALLOW_EDITING", "1")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.toggle(key="explore_edit").set_value(True)
    at.run()
    assert not at.exception, at.exception
