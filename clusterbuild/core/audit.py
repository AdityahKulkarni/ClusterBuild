"""Local audit log -- this user's own action history (Phase 1).

Not shared across the team (see plan trade-off notes for the standalone
CLI), but useful for the user's own troubleshooting/rollback, and it's the
hook point if a team later opts into pushing these events to a shared
webhook for lightweight cross-team visibility.
"""

from __future__ import annotations

from clusterbuild.core.state import AuditLogEntry, get_session


def record(action: str, detail: str = "") -> None:
    session = get_session()
    try:
        session.add(AuditLogEntry(action=action, detail=detail))
        session.commit()
    finally:
        session.close()


def recent(limit: int = 50) -> list[AuditLogEntry]:
    session = get_session()
    try:
        return (
            session.query(AuditLogEntry)
            .order_by(AuditLogEntry.at.desc())
            .limit(limit)
            .all()
        )
    finally:
        session.close()
