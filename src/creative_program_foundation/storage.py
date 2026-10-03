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
    organization_id TEXT NOT NULL UNIQUE REFERENCES organizations(organization_id),
    partner_type TEXT NOT NULL,
    name TEXT NOT NULL,
    license_no TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS partner_qualifications (
    qualification_id TEXT PRIMARY KEY,
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected', 'suspended')),
    valid_until TEXT,
    note TEXT,
    reviewed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS partner_representatives (
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    actor_id TEXT NOT NULL UNIQUE REFERENCES actors(actor_id),
    can_sign INTEGER NOT NULL CHECK(can_sign IN (0, 1)),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(partner_id, actor_id)
);
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    required_countersign_roles_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    actor_id TEXT NOT NULL UNIQUE REFERENCES actors(actor_id),
    member_role TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(team_id, actor_id)
);
CREATE TABLE IF NOT EXISTS signing_grants (
    grant_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    scope_type TEXT NOT NULL CHECK(scope_type IN ('team', 'work')),
    scope_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    granted_by TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    title TEXT NOT NULL,
    award_name TEXT,
    metadata_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS design_versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_code TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_code)
);
CREATE TABLE IF NOT EXISTS design_signatures (
    version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(version_id, actor_id)
);
CREATE TABLE IF NOT EXISTS version_prerequisites (
    version_id TEXT NOT NULL REFERENCES design_versions(version_id),
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY(version_id, code)
);
CREATE TABLE IF NOT EXISTS version_prerequisite_facts (
    version_id TEXT NOT NULL,
    code TEXT NOT NULL,
    completed_by TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(version_id, code)
);
CREATE TABLE IF NOT EXISTS cost_sheets (
    cost_sheet_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_no INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    sheet_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no)
);
CREATE TABLE IF NOT EXISTS share_sheets (
    share_sheet_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    version_no INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    sheet_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, version_no)
);
CREATE TABLE IF NOT EXISTS intentions (
    intention_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    channels_json TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'terminated')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    terminated_at TEXT,
    terminate_reason TEXT
);
CREATE TABLE IF NOT EXISTS offers (
    offer_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    prev_offer_id TEXT,
    intention_id TEXT,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    design_version_id TEXT NOT NULL,
    cost_sheet_id TEXT NOT NULL,
    share_sheet_id TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'countered', 'reserved', 'accepted', 'withdrawn', 'expired', 'terminated')),
    valid_until TEXT NOT NULL,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(thread_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS offer_reservations (
    reservation_id TEXT PRIMARY KEY,
    offer_id TEXT NOT NULL REFERENCES offers(offer_id),
    prior_status TEXT NOT NULL,
    reserved_by TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    reserve_until TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS offer_signatures (
    offer_id TEXT NOT NULL REFERENCES offers(offer_id),
    party TEXT NOT NULL CHECK(party IN ('team', 'partner')),
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(offer_id, party)
);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY,
    offer_id TEXT NOT NULL UNIQUE REFERENCES offers(offer_id),
    thread_id TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'terminated')),
    design_version_id TEXT NOT NULL,
    cost_sheet_id TEXT NOT NULL,
    cost_sheet_hash TEXT NOT NULL,
    share_sheet_id TEXT NOT NULL,
    share_sheet_hash TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    signed_by_team TEXT NOT NULL,
    signed_by_partner TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    end_date TEXT,
    terminated_at TEXT,
    terminated_by TEXT,
    terminate_reason TEXT
);
CREATE TABLE IF NOT EXISTS contract_milestones (
    milestone_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('sample', 'delivery', 'approval')),
    planned_qty TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    UNIQUE(contract_id, code),
    UNIQUE(contract_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS contract_facts (
    fact_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    sequence_no INTEGER NOT NULL,
    fact_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS sample_confirmations (
    sample_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    design_version_id TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK(approved IN (0, 1)),
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    milestone_code TEXT NOT NULL,
    design_version_id TEXT NOT NULL,
    quantity TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_orders (
    change_order_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    new_design_version_id TEXT,
    new_cost_sheet_id TEXT,
    new_share_sheet_id TEXT,
    reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'approved', 'rejected')),
    team_signature TEXT,
    team_signed_at TEXT,
    partner_signature TEXT,
    partner_signed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breaches (
    breach_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    sequence_no INTEGER NOT NULL,
    party TEXT NOT NULL CHECK(party IN ('team', 'partner')),
    description TEXT NOT NULL,
    remedy_deadline TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'remedied', 'rejected')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS breach_remediations (
    remediation_id TEXT PRIMARY KEY,
    breach_id TEXT NOT NULL REFERENCES breaches(breach_id),
    note TEXT,
    accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    kind TEXT NOT NULL CHECK(kind IN ('regular', 'correction')),
    corrects_settlement_id TEXT,
    period_start TEXT,
    period_end TEXT,
    final_settlement INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    gross_revenue TEXT NOT NULL,
    period_quantity TEXT NOT NULL,
    delivered_qty TEXT NOT NULL,
    cost_sheet_id TEXT NOT NULL,
    cost_sheet_hash TEXT NOT NULL,
    share_sheet_id TEXT NOT NULL,
    share_sheet_hash TEXT NOT NULL,
    minimum_quantity TEXT,
    shortfall INTEGER NOT NULL DEFAULT 0,
    partner_amount TEXT NOT NULL,
    team_amount TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'disputed', 'upheld', 'corrected')),
    detail_json TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_entries (
    entry_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    entry_kind TEXT NOT NULL,
    recipient_id TEXT,
    amount TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS settlement_disputes (
    dispute_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    resolution TEXT,
    resolution_action TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_work ON offers(work_id);
CREATE INDEX IF NOT EXISTS idx_offers_thread ON offers(thread_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_reservations_offer ON offer_reservations(offer_id);
CREATE INDEX IF NOT EXISTS idx_contracts_work ON contracts(work_id, status);
CREATE INDEX IF NOT EXISTS idx_facts_contract ON contract_facts(contract_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_deliveries_contract ON deliveries(contract_id, created_at);
CREATE INDEX IF NOT EXISTS idx_entries_settlement ON settlement_entries(settlement_id);
CREATE INDEX IF NOT EXISTS idx_grants_lookup ON signing_grants(team_id, actor_id, status);
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
