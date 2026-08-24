"""Smoke tests: every catalogue entry lints clean and tests as meta.expected."""
from pathlib import Path

import pytest

from playground import catalogue, runner
from playground.fixtures import ensure_fixtures

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT / "contracts"

ENTRIES = catalogue.scan(CONTRACTS_DIR)


@pytest.fixture(scope="session", autouse=True)
def _ensure_fixtures():
    """Fill in missing/stale fixtures once per run, not at import time."""
    ensure_fixtures(CONTRACTS_DIR)


def _entry_by_slug(slug: str) -> catalogue.Entry:
    return next(e for e in ENTRIES if e.slug == slug)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e.slug)
def test_lint_passes(entry: catalogue.Entry):
    if not entry.expected:
        pytest.skip(f"{entry.slug} has no expected result in meta.yaml")
    run = runner.lint(catalogue.load_yaml(entry), base_dir=entry.dir)
    assert run.has_passed(), run.model_dump_json(indent=2)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e.slug)
def test_result_matches_expected(entry: catalogue.Entry):
    if not entry.expected:
        pytest.skip(f"{entry.slug} has no expected result in meta.yaml")
    run = runner.test(catalogue.load_yaml(entry), base_dir=entry.dir)
    assert run.result == entry.expected, run.model_dump_json(indent=2)


def test_build_catalog_produces_index(tmp_path):
    result = runner.build_catalog(CONTRACTS_DIR, tmp_path / "catalog")
    assert result.index_html.is_file()
    assert "Data Contract" in result.index_html.read_text(encoding="utf-8")
    # one html page per contract file (plus meta pages from the glob)
    assert result.pages


# ---------------------------------------------------------------------------
# security hardening: server validation + path containment + catalog sanitising
# ---------------------------------------------------------------------------

_CONTRACT_TEMPLATE = """\
apiVersion: v3.1.0
kind: DataContract
id: security-probe
info:
  title: security probe
  version: 0.0.1
servers:
{server_block}
"""


def test_server_path_traversal_rejected(tmp_path):
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    path: ../../../../etc/hostname")
    with pytest.raises(ValueError, match="escapes the contract folder"):
        runner.test(yaml_str, base_dir=tmp_path)


def test_server_absolute_path_rejected(tmp_path):
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    path: /etc/hostname")
    with pytest.raises(ValueError, match="Absolute server paths"):
        runner.test(yaml_str, base_dir=tmp_path)


def test_non_local_server_rejected(tmp_path):
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: cloud\n    type: snowflake\n"
                     "    account: acme\n    host: acme.snowflakecomputing.com")
    with pytest.raises(ValueError, match="Only local servers"):
        runner.test(yaml_str, base_dir=tmp_path)


def test_server_symlink_escape_rejected(tmp_path):
    link = tmp_path / "linked-outside"
    try:
        link.symlink_to(tmp_path.parent)
    except OSError as exc:  # Windows needs privileges to create symlinks
        pytest.skip(f"cannot create symlink: {exc}")
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    path: linked-outside/probe.csv")
    with pytest.raises(ValueError, match="escapes the contract folder"):
        runner.test(yaml_str, base_dir=tmp_path)


def test_server_mid_path_traversal_rejected(tmp_path):
    (tmp_path / "data").mkdir()
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    path: data/../../../etc/hostname")
    with pytest.raises(ValueError, match="escapes the contract folder"):
        runner.test(yaml_str, base_dir=tmp_path)


@pytest.mark.parametrize(
    "call",
    [
        runner.lint,
        runner.test,
        lambda yaml_str: runner.export(yaml_str, "json"),
        lambda yaml_str: runner.changelog(yaml_str, yaml_str),
    ],
    ids=["lint", "test", "export", "changelog"],
)
def test_relative_path_without_base_dir_refused(call):
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    path: fixtures/data.csv")
    with pytest.raises(ValueError, match="refusing to run"):
        call(yaml_str)


def test_local_server_with_remote_field_rejected(tmp_path):
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    host: db.example.com\n"
                     "    path: fixtures/data.csv")
    with pytest.raises(ValueError, match="remote connection"):
        runner.test(yaml_str, base_dir=tmp_path)


def test_remote_field_match_is_case_insensitive(tmp_path):
    yaml_str = _CONTRACT_TEMPLATE.format(
        server_block="  - server: local\n    type: local\n"
                     "    HOST: db.example.com\n"
                     "    path: fixtures/data.csv")
    with pytest.raises(ValueError, match="remote connection"):
        runner.test(yaml_str, base_dir=tmp_path)


def test_sanitize_catalog_html_strips_script():
    dirty = (
        '<div class="page" onclick="evil()">'
        '<script src="https://evil.example/x.js"></script>'
        '<script>alert("xss")</script>'
        '<a href="javascript:alert(1)">click</a>'
        '<style>.page{color:red}</style>'
        '<table><tr><td>cell</td></tr></table>'
        '</div>'
    )
    clean = catalogue.sanitize_catalog_html(dirty)
    assert "<script" not in clean
    assert "evil.example" not in clean
    assert "alert" not in clean
    assert "onclick" not in clean
    assert "javascript:" not in clean
    # catalog styling and tables survive
    assert "<style>" in clean and "color:red" in clean
    assert "<table>" in clean


def test_sanitize_catalog_html_drops_dialog_overlay():
    # the catalog's YAML modal is a <dialog>: not an allowed tag, so nh3 would
    # strip the tag but keep its fixed-position backdrop as a page overlay
    dirty = (
        '<main><p>catalog content</p></main>'
        '<dialog aria-modal="true">'
        '<div class="fixed inset-0 bg-gray-500 bg-opacity-75"></div>'
        '<div class="fixed inset-0 z-10">modal body</div>'
        '</dialog>'
    )
    clean = catalogue.sanitize_catalog_html(dirty)
    assert "catalog content" in clean
    assert "<dialog" not in clean
    assert "fixed inset-0" not in clean
    assert "modal body" not in clean


def test_sanitize_catalog_html_rewrites_home_link_only():
    # inter-page links stay relative (they resolve as static URLs under the
    # catalog's static dir); only the home link is rewritten, depth-relative,
    # so it never hits the origin root
    dirty = (
        '<a href="contracts/hello-orders/contract.odcs.html">open</a>'
        '<a href="/">home</a>'
        '<a href="https://datacontract.com">ext</a>'
    )
    clean = catalogue.sanitize_catalog_html(dirty, home_href="../../catalog.html")
    assert 'href="contracts/hello-orders/contract.odcs.html"' in clean
    assert 'href="../../catalog.html"' in clean
    assert 'href="https://datacontract.com"' in clean
    assert "target=" not in clean


def test_with_static_base_prepends_relative_base():
    # the srcdoc index gets a relative <base> into the static catalog dir so
    # its links resolve there (under / locally, /~/+/ on Streamlit Cloud)
    out = catalogue.with_static_base("<p>x</p>")
    assert out.startswith('<base href="app/static/catalog/">')


def test_ensure_static_catalog_writes_sanitised_pages(tmp_path):
    src_dir = tmp_path / "catalog"
    (src_dir / "contracts" / "x").mkdir(parents=True)
    (src_dir / "index.html").write_text(
        '<a href="/">home</a><script>alert(1)</script>', encoding="utf-8")
    (src_dir / "contracts" / "x" / "page.html").write_text(
        '<a href="/">home</a>', encoding="utf-8")
    out_dir = tmp_path / "static" / "catalog"
    catalogue.ensure_static_catalog(src_dir, out_dir)
    index = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "<script" not in index
    assert 'href="catalog.html"' in index
    # nested pages get a depth-relative home link
    page = (out_dir / "contracts" / "x" / "page.html").read_text(encoding="utf-8")
    assert 'href="../../catalog.html"' in page
    # entry point duplicated under a non-index name (index.html gets
    # canonicalised to the directory by proxies; directories 404)
    assert (out_dir / "catalog.html").is_file()
    # up-to-date outputs are not rewritten
    (out_dir / "index.html").write_text("sentinel", encoding="utf-8")
    catalogue.ensure_static_catalog(src_dir, out_dir)
    assert (out_dir / "index.html").read_text(encoding="utf-8") == "sentinel"


def test_changelog_pair_has_entries():
    entry = _entry_by_slug("changelog-pair")
    v1 = entry.dir / "contract.v1.odcs.yaml"
    v2 = entry.dir / "contract.v2.odcs.yaml"
    result = runner.changelog(
        v1.read_text(encoding="utf-8"),
        v2.read_text(encoding="utf-8"),
        base_dir_a=entry.dir,
        base_dir_b=entry.dir,
    )
    dump = result.model_dump() if hasattr(result, "model_dump") else result
    entries = dump.get("entries") or dump.get("changes") or []
    assert len(entries) >= 3


def test_row_filter_passes_through():
    entry = _entry_by_slug("hello-orders")
    run = runner.test(catalogue.load_yaml(entry), base_dir=entry.dir,
                      row_filter="total_amount >= 0")
    assert run.result == "passed", run.model_dump_json(indent=2)
