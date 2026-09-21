"""Dangerous-DDL rule: data loss only, metadata drops are allowed.

Real-world origin: an idempotent tenant-column migration
(`ALTER TABLE x ADD COLUMN IF NOT EXISTS tenant_id ...`) plus a legacy
constraint cleanup (`ALTER TABLE x DROP CONSTRAINT IF EXISTS ...`) were
flagged as "dangerous DDL outside a migration file", blocking a legitimate
SRM store fix. `DROP CONSTRAINT` / `DROP DEFAULT` / `DROP NOT NULL` remove
metadata, never rows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    module_path = ROOT / "scripts" / "check-backend-quality.py"
    spec = importlib.util.spec_from_file_location("check_backend_quality", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _ddl(source: str, rel: str = "apps/api/src/store.ts") -> list:
    return checker._ddl_warnings(source, rel)


def test_flags_row_destroying_ddl() -> None:
    assert _ddl("DROP TABLE users;")
    assert _ddl("ALTER TABLE users DROP COLUMN email;")
    assert _ddl("ALTER TABLE users DROP email;")
    assert _ddl("TRUNCATE TABLE audit_log;")


def test_allows_metadata_only_drops() -> None:
    assert not _ddl("ALTER TABLE srm_po DROP CONSTRAINT IF EXISTS srm_po_kh_id_fkey;")
    assert not _ddl("ALTER TABLE srm_item ALTER COLUMN tenant_id DROP DEFAULT;")
    assert not _ddl("ALTER TABLE srm_item ALTER COLUMN tenant_id DROP NOT NULL;")


def test_allows_idempotent_tenant_column_migration() -> None:
    migration = (
        "ALTER TABLE srm_item ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL "
        "DEFAULT 'sverpweb-stable';\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS srm_item_tenant_key "
        "ON srm_item (tenant_id, sp_id);\n"
    )
    assert not _ddl(migration)


def test_migration_paths_stay_exempt() -> None:
    assert not _ddl("DROP TABLE users;", "apps/api/src/persistence/migrations.ts")


def test_quoted_prose_is_not_ddl() -> None:
    assert not _ddl('const hint = "never drop table rows in production";')
