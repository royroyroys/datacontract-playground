"""Thin wrapper over the datacontract-cli Python API.

Everything except ``build_catalog`` goes through
``datacontract.data_contract.DataContract``; the catalog command has no
Python API, so ``build_catalog`` shells out to the console script.

Path resolution
---------------
Contracts reference local fixture files via ``servers: [{path: ...}]`` with
paths relative to the contract file. ``datacontract-cli`` resolves relative
paths against the *current working directory*, which breaks as soon as the app
is started from anywhere else. To stay chdir-free, every public function here
loads the YAML, rewrites relative ``servers[].path`` values to absolute paths
against ``base_dir`` (the catalogue entry folder), and passes the modified
string to ``DataContract``. If ``base_dir`` is None the YAML is used as-is
only when no server carries a path; any relative ``servers[].path`` raises
ValueError (fail-closed).

Hardening: only ``type: local`` servers are accepted, and paths are contained
to ``base_dir`` (absolute paths and ``..`` traversal raise ValueError). This
runs for every DataContract construction, so lint/test/export/changelog are
all covered.

All functions let exceptions propagate; callers (the UI) wrap them in
try/except and show ``st.error``.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from datacontract.data_contract import DataContract

if TYPE_CHECKING:
    from datacontract.model.changelog import ChangelogResult
    from datacontract.model.run import Run


# Host/account/connection-string style fields: a server entry carrying any of
# these is a remote/cloud connection definition, which this playground never
# runs (local DuckDB fixtures only). All-lowercase; keys are compared with
# str.lower() so camelCase and upper-case spellings are caught too.
_REMOTE_SERVER_FIELDS = frozenset({
    "host", "hostname", "port", "account", "user", "username", "password",
    "token", "connectionstring", "connection_string", "connectionurl",
    "connection_url", "endpoint", "endpointurl", "endpoint_url", "project",
    "warehouse", "role", "serviceaccountkey", "service_account_key",
    "authtoken", "auth_token", "clientid", "client_id", "clientsecret",
    "client_secret", "region", "catalog", "httppath", "http_path",
})


def _server_label(server: dict) -> str:
    name = server.get("server") or server.get("name")
    return f"server {name!r}" if name else "a server entry"


def _validate_server(server: dict) -> None:
    """Reject non-local servers and remote connection definitions."""
    server_type = server.get("type", server.get("serverType"))
    label = _server_label(server)
    if server_type is not None and str(server_type).lower() != "local":
        raise ValueError(
            "Only local servers are supported in this playground, but "
            f"{label} has type {server_type!r}. Remote engines are never "
            "contacted from here - use `type: local` with a `path:` relative "
            "to the contract folder."
        )
    remote_fields = sorted(k for k in server
                           if isinstance(k, str)
                           and k.lower() in _REMOTE_SERVER_FIELDS)
    if remote_fields:
        raise ValueError(
            f"{label.capitalize()} looks like a remote connection "
            f"(field{'s' if len(remote_fields) > 1 else ''}: "
            f"{', '.join(remote_fields)}). Only local servers are supported "
            "in this playground - connection details for remote engines must "
            "not be used here."
        )


def _resolve_server_paths(yaml_str: str, base_dir: str | Path | None) -> str:
    """Validate servers and rewrite relative paths to absolute ones under base_dir.

    Security hardening (two layers, applied before DataContract is built):

    * local-only: every ``servers[]`` entry with a ``type``/``serverType``
      other than ``local`` is rejected, as are entries that carry
      host/account/connection-string style fields.
    * containment: absolute ``servers[].path`` values are rejected; relative
      ones must resolve to a location inside ``base_dir`` (path traversal
      like ``../../etc/passwd`` raises ValueError).

    Raises ValueError with a user-facing message (shown via st.error).
    """
    doc = yaml.safe_load(yaml_str)
    if not isinstance(doc, dict):
        return yaml_str
    servers = doc.get("servers")
    if not isinstance(servers, list):
        return yaml_str
    base = Path(base_dir).resolve() if base_dir is not None else None
    changed = False
    for server in servers:
        if not isinstance(server, dict):
            continue
        _validate_server(server)
        path = server.get("path")
        if not (isinstance(path, str) and path):
            continue
        if Path(path).is_absolute():
            raise ValueError(
                f"Absolute server paths are not allowed in this playground: "
                f"{_server_label(server)} uses {path!r}. Use a path relative "
                "to the contract folder instead."
            )
        if base is None:
            raise ValueError(
                f"Server path {path!r} cannot be checked without a contract "
                "folder (base_dir); refusing to run. Pass base_dir so paths "
                "can be contained to the contract folder."
            )
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(
                f"Server path {path!r} escapes the contract folder "
                f"({base}). Paths must stay inside the contract folder - "
                "path traversal is not allowed."
            )
        server["path"] = str(resolved)
        changed = True
    if not changed:
        return yaml_str
    return yaml.safe_dump(doc, sort_keys=False)


def _contract(yaml_str: str, base_dir: str | Path | None = None, **kwargs) -> DataContract:
    resolved = _resolve_server_paths(yaml_str, base_dir)
    kwargs.setdefault("server", None)
    return DataContract(data_contract_str=resolved, **kwargs)


def lint(yaml_str: str, all_errors: bool = False,
         base_dir: str | Path | None = None) -> "Run":
    """Lint a contract. Returns datacontract Run (result/checks/logs).

    Note: ``all_errors`` is a DataContract constructor kwarg (lint() itself
    takes no arguments in datacontract-cli 1.1).
    """
    dc = _contract(yaml_str, base_dir, all_errors=all_errors)
    return dc.lint()


def test(yaml_str: str, server: str | None = None, checks: list[str] | None = None,
         dimension: str | None = None, quality_id: str | None = None,
         tag: str | None = None, row_filter: str | None = None,
         include_failed_samples: bool = False,
         metadata_only: bool = False,
         base_dir: str | Path | None = None) -> "Run":
    """Run data contract tests against the local fixture server."""
    return _contract(
        yaml_str,
        base_dir,
        server=server,
        check_categories=set(checks) if checks else None,
        dimensions={dimension} if dimension else None,
        quality_ids={quality_id} if quality_id else None,
        tags={tag} if tag else None,
        filter=row_filter,
        include_failed_samples=include_failed_samples,
        metadata_only=metadata_only,
    ).test()


def export(yaml_str: str, fmt: str,
           base_dir: str | Path | None = None, **export_kwargs) -> "str | bytes":
    """Export the contract to one of datacontract-cli's export formats.

    Extra kwargs are forwarded to ``DataContract.export()`` (e.g.
    ``sql_server_type="postgres"`` for the sql format).
    """
    return _contract(yaml_str, base_dir).export(fmt, **export_kwargs)


def changelog(yaml_a: str, yaml_b: str,
              base_dir_a: str | Path | None = None,
              base_dir_b: str | Path | None = None) -> "ChangelogResult":
    """Changelog between two contracts (the supported path; diff/breaking are gone)."""
    dc_a = _contract(yaml_a, base_dir_a)
    dc_b = _contract(yaml_b, base_dir_b)
    return dc_a.changelog(dc_b)


# ---------------------------------------------------------------------------
# catalog (datacontract catalog) - no Python API, so this one shells out
# ---------------------------------------------------------------------------

@dataclass
class CatalogResult:
    """Outcome of a ``datacontract catalog`` run."""
    output_dir: Path
    index_html: Path
    pages: list[Path] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    expected_pages: int = 0

    @property
    def complete(self) -> bool:
        """False when fewer pages landed on disk than files matched by the glob
        (observed on slow/network filesystems that drop fresh writes)."""
        return len(self.pages) >= self.expected_pages


def _datacontract_cli() -> str:
    """Locate the datacontract console script (sibling of the interpreter)."""
    sibling = Path(sys.executable).with_name("datacontract")
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("datacontract")
    if found:
        return found
    raise FileNotFoundError(
        "datacontract console script not found next to "
        f"{sys.executable} or on PATH"
    )


def build_catalog(contracts_dir: str | Path, output_dir: str | Path,
                  files_glob: str = "**/contract*.yaml",
                  timeout: float = 300,
                  attempts: int = 3) -> CatalogResult:
    """Build an HTML catalog of the contracts via ``datacontract catalog``.

    There is no Python API for the catalog command, so this shells out to the
    console script. The glob is evaluated relative to the parent of
    ``contracts_dir`` so the generated pages mirror the
    ``<output_dir>/<contracts-dir-name>/<slug>/...`` layout. ``output_dir`` is
    recreated from scratch on every call. Raises RuntimeError (with the CLI's
    stderr/stdout) when the command fails.

    Some filesystems (network/overlay mounts) silently drop files written by
    the CLI subprocess, so the catalog is built into a local temp dir and then
    copied into place; the publish step is retried (up to ``attempts`` times)
    until every matched contract file has a page on disk. Check
    ``result.complete`` for the final state.
    """
    contracts_dir = Path(contracts_dir).resolve()
    output_dir = Path(output_dir)
    pattern = f"{contracts_dir.name}/{files_glob}"
    expected_pages = len(list(contracts_dir.parent.glob(pattern)))
    # Build once in a local temp dir: direct CLI writes into network/overlay
    # mounts are unreliable (files reported as "Created" go missing).
    tmp = Path(tempfile.mkdtemp(prefix="datacontract-catalog-"))
    try:
        proc = subprocess.run(
            [_datacontract_cli(), "catalog",
             "--files", pattern, "--output", str(tmp)],
            cwd=contracts_dir.parent,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"datacontract catalog exited with {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        index_src = tmp / "index.html"
        if not index_src.is_file():
            raise RuntimeError(
                "datacontract catalog reported success but produced no "
                f"{index_src}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        # Publish: replace output_dir with the temp build. Only the copy is
        # retried; the (expensive) CLI build runs once.
        result: CatalogResult | None = None
        for _ in range(max(1, attempts)):
            if output_dir.exists():
                shutil.rmtree(output_dir)
                time.sleep(0.5)  # let deletions settle before recreating the tree
            shutil.copytree(tmp, output_dir)
            index_html = output_dir / "index.html"
            pages = sorted(p for p in output_dir.rglob("*.html") if p != index_html)
            result = CatalogResult(
                output_dir=output_dir,
                index_html=index_html,
                pages=pages,
                # show the destination paths, not the temp build dir
                stdout=proc.stdout.replace(str(tmp), str(output_dir)),
                stderr=proc.stderr,
                expected_pages=expected_pages,
            )
            if result.complete and index_html.is_file():
                return result
        if result is not None and not result.index_html.is_file():
            raise RuntimeError(
                "datacontract catalog produced no "
                f"{result.index_html} after publishing\nstdout:\n{result.stdout}"
            )
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
