"""Writes audit_log entries. Never pass passwords, Omang digits or full phone numbers in `details`."""
import ipaddress

from sqlalchemy.orm import Session

from app.models import AuditLog


def mask_ip(ip: str | None) -> str | None:
    """192.168.1.57 -> 192.168.1.x ; IPv6 keeps only the first 3 groups."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if addr.version == 4:
        return ".".join(ip.split(".")[:3]) + ".x"
    return ":".join(addr.exploded.split(":")[:3]) + "::x"


def record(
    db: Session,
    action: str,
    *,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: str | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            ip_address=mask_ip(ip),
        )
    )
    db.commit()
