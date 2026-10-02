"""Point 14 RBAC administration schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EffectiveAccessOut(BaseModel):
    user_id: uuid.UUID
    full_name: str | None = None
    email: str | None = None
    is_active: bool = True
    role: str
    role_codes: list[str]
    is_super_admin: bool
    all_stores: bool
    permissions: list[str]
    store_ids: list[uuid.UUID]
    direct_store_ids: list[uuid.UUID] = []
    scopes: list["ScopeEntry"] = []
    mfa_enabled: bool = False
    mfa_required: bool = False
    must_change_password: bool = False


class PermissionOut(BaseModel):
    code: str
    module: str
    module_label: str
    feature: str
    action: str
    description: str | None
    transactional: bool


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    is_active: bool
    is_system: bool
    scope_level: str
    max_discount_percent: float
    max_discount_value: float
    mfa_required: bool = False
    user_count: int = 0
    permission_count: int = 0


class RoleCreateIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    name: str = Field(min_length=2, max_length=80)
    description: str | None = None
    scope_level: str = Field(default="assigned", pattern="^(global|assigned)$")
    max_discount_percent: float = Field(default=0, ge=0, le=100)
    max_discount_value: float = Field(default=0, ge=0)
    mfa_required: bool = False
    permission_codes: list[str] = []
    clone_from_role_id: uuid.UUID | None = None
    reason: str | None = None


class RoleUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    description: str | None = None
    scope_level: str | None = Field(default=None, pattern="^(global|assigned)$")
    max_discount_percent: float | None = Field(default=None, ge=0, le=100)
    max_discount_value: float | None = Field(default=None, ge=0)
    mfa_required: bool | None = None
    reason: str | None = None


class RoleActivationIn(BaseModel):
    is_active: bool
    reason: str | None = None


class RolePermissionsIn(BaseModel):
    permission_codes: list[str]
    reason: str | None = None


class MatrixChange(BaseModel):
    role_id: uuid.UUID
    permission_code: str
    granted: bool


class MatrixUpdateIn(BaseModel):
    changes: list[MatrixChange] = Field(min_length=1, max_length=2000)
    reason: str | None = None


class MatrixOut(BaseModel):
    roles: list[RoleOut]
    permissions: list[PermissionOut]
    grants: dict[str, list[str]]  # role_id -> permission codes


class RoleUserOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str | None
    is_active: bool
    assignment: str  # primary|additional


class UserRolesIn(BaseModel):
    primary_role_code: str
    additional_role_codes: list[str] = []
    reason: str | None = None


class ScopeEntry(BaseModel):
    scope_type: str = Field(pattern="^(company|region|cluster|city|warehouse|department)$")
    scope_id: uuid.UUID | None = None
    scope_value: str | None = None


class UserScopesIn(BaseModel):
    store_ids: list[uuid.UUID] = []
    scopes: list[ScopeEntry] = []
    reason: str | None = None


class MfaResetIn(BaseModel):
    reason: str = Field(min_length=3)


class SuperAdminIn(BaseModel):
    is_super_admin: bool
    reason: str = Field(min_length=3)


class ApprovalRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    request_type: str
    name: str
    threshold_amount: float | None
    min_amount: float | None
    max_amount: float | None
    approver_permission: str
    approver_role_codes: list[str] | None
    levels: int
    maker_checker: bool
    conditions: dict | None
    is_active: bool
    updated_at: datetime | None


class ApprovalRuleIn(BaseModel):
    request_type: str
    name: str = Field(min_length=2)
    threshold_amount: float | None = Field(default=None, ge=0)
    min_amount: float | None = Field(default=None, ge=0)
    max_amount: float | None = Field(default=None, ge=0)
    approver_permission: str = "approval.request.approve"
    approver_role_codes: list[str] | None = None
    levels: int = Field(default=1, ge=1, le=5)
    maker_checker: bool = True
    conditions: dict | None = None
    is_active: bool = True
    reason: str | None = None


class RbacAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    user_id: uuid.UUID | None
    role_code: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    old_value: dict | list | None
    new_value: dict | list | None
    reason: str | None


EffectiveAccessOut.model_rebuild()
