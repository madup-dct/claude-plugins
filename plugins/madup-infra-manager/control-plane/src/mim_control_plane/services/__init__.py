"""Service helpers that sit above the pure domain records."""

from .audit import AuditRecord, build_audit_record

__all__ = ["AuditRecord", "build_audit_record"]
