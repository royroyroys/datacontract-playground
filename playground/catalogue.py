"""Catalogue scanning: discover contract entries under contracts/.

Each catalogue entry is a folder containing one or more ODCS YAML files,
a data/ folder with fixtures, and a meta.yaml describing the entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import nh3
import yaml


@dataclass
class Entry:
    slug: str
    title: str
    summary: str
    tags: list[str]
    difficulty: str
    expected: str | None
    dir: Path
    main_yaml: Path
    extra_yamls: dict[str, Path] = field(default_factory=dict)
    data_files: list[Path] = field(default_factory=list)
    data_format: str | None = None
    notes: str | None = None
    order: int = 999  # meta order: position in the sidebar picker (unset = last)
    changelog: bool = False  # meta changelog: show the Changelog section on Explore


def _meta_defaults(slug: str) -> dict:
    return {
        "title": slug,
        "summary": "",
        "tags": [],
        "difficulty": "beginner",
        "expected": None,
        "data_format": None,
        "notes": None,
        "order": 999,
        "changelog": False,
    }


def _pick_main_yaml(yamls: list[Path]) -> Path:
    """Prefer contract.odcs.yaml; otherwise the last in sorted order, so that
    versioned pairs (contract.v1.odcs.yaml, contract.v2.odcs.yaml) default to
    the newest version as the runnable contract."""
    for p in yamls:
        if p.name == "contract.odcs.yaml":
            return p
    return sorted(yamls)[-1]


def scan(contracts_dir: Path) -> list[Entry]:
    """Scan contracts_dir for catalogue entries.

    Reads each folder's meta.yaml; tolerates missing meta (title=slug).
    Entries are sorted by meta ``order`` (unset sorts last), then slug.
    Folders without any *.yaml contract file are skipped.
    """
    entries: list[Entry] = []
    if not contracts_dir.is_dir():
        return entries
    for folder in sorted(p for p in contracts_dir.iterdir() if p.is_dir()):
        yamls = sorted(
            p for p in folder.glob("*.yaml") if p.name != "meta.yaml"
        ) + sorted(folder.glob("*.yml"))
        if not yamls:
            continue
        meta = _meta_defaults(folder.name)
        meta_path = folder / "meta.yaml"
        if meta_path.is_file():
            with open(meta_path, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            meta.update({k: v for k, v in loaded.items() if v is not None})
        main_yaml = _pick_main_yaml(yamls)
        extra_yamls = {p.stem: p for p in yamls if p != main_yaml}
        data_dir = folder / "data"
        data_files = (sorted(p for p in data_dir.iterdir() if p.is_file())
                      if data_dir.is_dir() else [])
        entries.append(
            Entry(
                slug=folder.name,
                title=str(meta["title"]),
                summary=str(meta["summary"]),
                tags=list(meta["tags"] or []),
                difficulty=str(meta["difficulty"]),
                expected=meta["expected"],
                dir=folder,
                main_yaml=main_yaml,
                extra_yamls=extra_yamls,
                data_files=data_files,
                data_format=meta.get("data_format"),
                notes=meta.get("notes"),
                order=int(meta["order"]),
                changelog=bool(meta["changelog"]),
            )
        )
    entries.sort(key=lambda e: (e.order, e.slug))
    return entries


# --- catalog HTML sanitisation ---------------------------------------------
#
# The catalog HTML produced by ``datacontract catalog`` is embedded into the
# app via st.iframe. It is sanitised with nh3 (ammonia) first:
# <script> tags (and their contents), on* event handlers and javascript:
# URLs are stripped, while the Tailwind <style> blocks, tables and inline SVG
# icons the catalog needs are kept.
_CATALOG_EXTRA_TAGS = {
    "style",  # catalog embeds Tailwind via inline <style> blocks
    # inline SVG icons used by the generated pages
    "svg", "g", "path",
}
_CATALOG_GENERIC_ATTRIBUTES = {
    "class", "id", "style", "title", "role", "aria-hidden", "aria-label",
    "colspan", "rowspan", "scope", "align",
}
_CATALOG_TAG_ATTRIBUTES = {
    "svg": {"class", "viewBox", "xmlns", "fill", "stroke", "stroke-width",
            "stroke-linecap", "stroke-linejoin", "aria-hidden", "width",
            "height"},
    "path": {"d", "fill", "fill-rule", "clip-rule", "stroke", "stroke-width",
             "stroke-linecap", "stroke-linejoin", "class"},
    "g": {"fill", "stroke", "stroke-width", "class", "transform"},
}
# Tags whose *contents* are dropped together with the tag itself. nh3's
# default set is {"script", "style"}; keep "script" (JS must never render as
# text) but allow "style" since the catalog's CSS lives in <style> blocks.
# "dialog" is dropped with its contents: the catalog's YAML modal is not an
# allowed tag, so nh3 would strip the <dialog> wrapper but keep its children,
# rendering the fixed-position backdrop as an overlay on top of the page.
# The modal needs JS (stripped) to open, so nothing usable is lost.
_CATALOG_CLEAN_CONTENT_TAGS = set(nh3.CLEAN_CONTENT_TAGS) - {"style"} | {"dialog"}

# The sanitised catalog is written to static/catalog/ (see
# ensure_static_catalog) and embedded via st.iframe as srcdoc with a relative
# <base href="app/static/catalog/"> injected (with_static_base). The srcdoc
# iframe inherits the app document's URL as its base - / on localhost, /~/+/
# on Streamlit Cloud - so the relative base resolves to the real static files
# in both. Root-absolute URLs are NOT used: on Cloud they hit the platform
# shell's SPA fallback instead of the app. Clicks navigate only the iframe
# (default _self): no app reload, and no target="_top" (Cloud's wrapper
# blocks top navigation). The catalog's home link (href="/") is rewritten to
# a depth-relative catalog.html so it stays inside the static catalog.
_STATIC_BASE = "app/static/catalog/"


def _rewrite_catalog_links(html: str, home_href: str) -> str:
    return html.replace('href="/"', f'href="{home_href}"')


def with_static_base(html: str) -> str:
    """Prepend the relative <base> pointing the srcdoc catalog index at the
    statically served catalog directory. Applied only to the srcdoc copy,
    never to the written files: as a served page the relative base would
    double the path."""
    return f'<base href="{_STATIC_BASE}">' + html


def sanitize_catalog_html(html: str, home_href: str = "catalog.html") -> str:
    """Sanitise generated catalog HTML before serving it statically."""
    attributes = {tag: set(attrs) for tag, attrs in nh3.ALLOWED_ATTRIBUTES.items()}
    attributes["*"] = set(_CATALOG_GENERIC_ATTRIBUTES)
    for tag, attrs in _CATALOG_TAG_ATTRIBUTES.items():
        attributes.setdefault(tag, set()).update(attrs)
    return _rewrite_catalog_links(nh3.clean(
        html,
        tags=set(nh3.ALLOWED_TAGS) | _CATALOG_EXTRA_TAGS,
        clean_content_tags=_CATALOG_CLEAN_CONTENT_TAGS,
        attributes=attributes,
        strip_comments=True,
    ), home_href)


def ensure_static_catalog(catalog_dir: Path, static_dir: Path) -> None:
    """Write sanitised catalog pages to static_dir for Streamlit static serving.

    The app embeds the catalog index via st.iframe srcdoc (see
    with_static_base); clicks navigate the iframe to these static files, so
    inter-page links resolve as real static URLs. Sanitising happens here
    (once per stale file) instead of on every render.
    """
    if not catalog_dir.is_dir():
        return
    for src in catalog_dir.rglob("*.html"):
        dst = static_dir / src.relative_to(catalog_dir)
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        home_href = "../" * len(dst.relative_to(static_dir).parent.parts) + "catalog.html"
        clean = sanitize_catalog_html(
            src.read_text(encoding="utf-8"), home_href)
        dst.write_text(clean, encoding="utf-8")
        if src.name == "index.html" and src.parent == catalog_dir:
            # iframe entry point under a non-index name: proxies canonicalise
            # index.html to the directory, which the static endpoint 404s
            dst.with_name("catalog.html").write_text(clean, encoding="utf-8")


def load_yaml(entry: Entry, path: Path | None = None) -> str:
    """Read a contract YAML as text (default: the entry's main yaml)."""
    return (path or entry.main_yaml).read_text(encoding="utf-8")
