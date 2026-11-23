"""Importing every model here makes sure Alembic and SQLAlchemy know about all tables."""
from app.models.audit_log import AuditLog
from app.models.batch import Batch
from app.models.employee import Employee
from app.models.login_attempt import LoginAttempt
from app.models.payslip import Payslip
from app.models.system_state import SystemState
from app.models.user import User

__all__ = ["AuditLog", "Batch", "Employee", "LoginAttempt", "Payslip", "SystemState", "User"]
