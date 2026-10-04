"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS partners (
    partner_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    qualification_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS partner_members (
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(partner_id, actor_id)
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    title TEXT NOT NULL,
    award_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS design_versions (
    design_version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    summary TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no)
);
CREATE TABLE IF NOT EXISTS negotiations (
    negotiation_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    status TEXT NOT NULL CHECK(status IN ('open', 'accepted')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    offer_id TEXT PRIMARY KEY,
    negotiation_id TEXT NOT NULL REFERENCES negotiations(negotiation_id),
    offer_no INTEGER NOT NULL CHECK(offer_no >= 1),
    parent_offer_id TEXT,
    side TEXT NOT NULL CHECK(side IN ('team', 'partner')),
    status TEXT NOT NULL CHECK(status IN ('open', 'reserved', 'superseded', 'withdrawn', 'accepted')),
    terms_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(negotiation_id, offer_no)
);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY,
    negotiation_id TEXT NOT NULL REFERENCES negotiations(negotiation_id),
    offer_id TEXT NOT NULL REFERENCES offers(offer_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    design_version_id TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    required_signers_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'terminated')),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    terminated_at TEXT,
    termination_reason TEXT
);
CREATE TABLE IF NOT EXISTS signatures (
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    actor_id TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY(contract_id, actor_id)
);
CREATE TABLE IF NOT EXISTS milestones (
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    milestone_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    gate INTEGER NOT NULL CHECK(gate IN (0, 1)),
    depends_on_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'done')),
    completed_at TEXT,
    PRIMARY KEY(contract_id, milestone_key)
);
CREATE TABLE IF NOT EXISTS contract_facts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL UNIQUE,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cost_versions (
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(contract_id, version_no)
);
CREATE TABLE IF NOT EXISTS share_versions (
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    shares_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(contract_id, version_no)
);
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    period TEXT NOT NULL,
    gross_amount_cents INTEGER NOT NULL,
    cost_version_no INTEGER NOT NULL,
    share_version_no INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, period)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
