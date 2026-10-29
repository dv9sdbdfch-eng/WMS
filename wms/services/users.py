"""Login, users and roles."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wms.core.exceptions import AuthenticationError, DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import utcnow
from wms.core.permissions import ALL_PERMISSIONS, P
from wms.core.security import MIN_PASSWORD_LENGTH, hash_password, verify_password
from wms.db.models import Role, RolePermission, User
from wms.services import audit
from wms.services.base import BaseService, clean_str

USER_FIELDS = ("username", "full_name", "email", "role_id", "is_active")


def _check_password(pw: str) -> None:
    if not pw or len(pw) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "password")


class AuthService(BaseService):
    def login(self, username: str, password: str) -> User:
        uname = (username or "").strip().lower()
        with self.db.session() as s:
            user = s.scalar(select(User).where(func.lower(User.username) == uname))
            if user is None or not user.is_active or not verify_password(password or "", user.password_hash):
                audit.record(s, self.ctx, "LOGIN_FAILED", "USER", user.id if user else None, uname)
                s.commit()
                raise AuthenticationError("Incorrect username or password, or the account is disabled.")
            user.last_login_at = utcnow()
            self.ctx.user = user
            audit.record(s, self.ctx, "LOGIN", "USER", user.id, user.username)
            _ = user.role.permission_set  # load before session closes
            return user

    def logout(self, closing: bool = False) -> None:
        """End the session (Log out, or the program was closed)."""
        user = self.ctx.user
        if user is None:
            return
        with self.db.session() as s:
            audit.record(s, self.ctx, "LOGOUT", "USER", user.id, user.username,
                         "Program closed" if closing else "Logged out")
        self.ctx.user = None

    def change_own_password(self, current: str, new: str) -> None:
        _check_password(new)
        with self.db.session() as s:
            user = s.get(User, self.user.id)
            if not verify_password(current or "", user.password_hash):
                raise ValidationError("Current password is incorrect.", "current_password")
            if verify_password(new, user.password_hash):
                raise ValidationError("New password must be different from the current one.", "password")
            user.password_hash = hash_password(new)
            user.must_change_password = False
            audit.record(s, self.ctx, "PASSWORD_CHANGED", "USER", user.id, user.username)
        self.ctx.user.must_change_password = False


class UserService(BaseService):
    def list_users(self, include_inactive: bool = True) -> list[User]:
        with self.db.read_session() as s:
            q = select(User).order_by(User.username)
            if not include_inactive:
                q = q.where(User.is_active.is_(True))
            return list(s.scalars(q))

    def list_roles(self) -> list[Role]:
        with self.db.read_session() as s:
            return list(s.scalars(select(Role).order_by(Role.id)))

    def _active_admins(self, s: Session, excluding: int | None = None) -> int:
        q = (
            select(func.count(User.id))
            .join(Role)
            .join(RolePermission)
            .where(User.is_active.is_(True), RolePermission.permission == P.USERS_MANAGE)
        )
        if excluding:
            q = q.where(User.id != excluding)
        return s.scalar(q) or 0

    def create_user(self, data: dict, password: str) -> User:
        self.require(P.USERS_MANAGE)
        username = clean_str(data.get("username"), "username", required=True, max_len=50).lower()
        if " " in username:
            raise ValidationError("Username cannot contain spaces.", "username")
        full_name = clean_str(data.get("full_name"), "full_name", required=True, max_len=120)
        _check_password(password)
        with self.db.session() as s:
            if s.scalar(select(User.id).where(func.lower(User.username) == username)):
                raise DuplicateError(f"Username '{username}' is already taken.")
            role = s.get(Role, data.get("role_id"))
            if role is None:
                raise ValidationError("Select a role.", "role_id")
            user = User(
                username=username,
                full_name=full_name,
                email=clean_str(data.get("email")),
                role=role,
                password_hash=hash_password(password),
                must_change_password=bool(data.get("must_change_password", True)),
                is_active=True,
            )
            s.add(user)
            s.flush()
            audit.record(s, self.ctx, "USER_CREATED", "USER", user.id, username,
                         new={"username": username, "full_name": full_name, "role": role.name})
            saved_id = user.id
        return self.reload(User, saved_id)

    def update_user(self, user_id: int, data: dict) -> User:
        self.require(P.USERS_MANAGE)
        with self.db.session() as s:
            user = s.get(User, user_id)
            if user is None:
                raise NotFoundError("User not found.")
            old = {"full_name": user.full_name, "email": user.email, "role": user.role.name, "is_active": user.is_active}
            user.full_name = clean_str(data.get("full_name", user.full_name), "full_name", required=True, max_len=120)
            user.email = clean_str(data.get("email", user.email))
            if "role_id" in data and data["role_id"] != user.role_id:
                role = s.get(Role, data["role_id"])
                if role is None:
                    raise ValidationError("Select a role.", "role_id")
                user.role = role
            if "is_active" in data:
                user.is_active = bool(data["is_active"])
            s.flush()
            if self._active_admins(s) == 0:
                raise ValidationError("At least one active user must keep the 'Manage users' permission.")
            new = {"full_name": user.full_name, "email": user.email, "role": user.role.name, "is_active": user.is_active}
            o, n = audit.diff(old, new)
            if n:
                audit.record(s, self.ctx, "USER_UPDATED", "USER", user.id, user.username, old=o, new=n)
            saved_id = user.id
        return self.reload(User, saved_id)

    def reset_password(self, user_id: int, new_password: str) -> None:
        self.require(P.USERS_MANAGE)
        _check_password(new_password)
        with self.db.session() as s:
            user = s.get(User, user_id)
            if user is None:
                raise NotFoundError("User not found.")
            user.password_hash = hash_password(new_password)
            user.must_change_password = True
            audit.record(s, self.ctx, "PASSWORD_RESET", "USER", user.id, user.username)

    # -- roles --------------------------------------------------------------
    def save_role(self, role_id: int | None, code: str, name: str, description: str | None,
                  permissions: set[str]) -> Role:
        self.require(P.USERS_MANAGE)
        unknown = set(permissions) - set(ALL_PERMISSIONS)
        if unknown:
            raise ValidationError(f"Unknown permissions: {', '.join(sorted(unknown))}")
        name = clean_str(name, "name", required=True, max_len=80)
        with self.db.session() as s:
            if role_id:
                role = s.get(Role, role_id)
                if role is None:
                    raise NotFoundError("Role not found.")
                if role.code == "ADMIN":
                    raise ValidationError("The Administrator role always has full access and cannot be changed.")
                old_perms = sorted(role.permission_set)
            else:
                code = clean_str(code, "code", required=True, max_len=30).upper().replace(" ", "_")
                if s.scalar(select(Role.id).where(Role.code == code)):
                    raise DuplicateError(f"Role code '{code}' already exists.")
                role = Role(code=code, name=name, is_system=False)
                s.add(role)
                old_perms = []
            role.name = name
            role.description = clean_str(description)
            role.permissions = [RolePermission(permission=p) for p in sorted(permissions)]
            s.flush()
            if self._active_admins(s) == 0:
                raise ValidationError("At least one active user must keep the 'Manage users' permission.")
            audit.record(s, self.ctx, "ROLE_SAVED", "USER", role.id, role.name,
                         old={"permissions": old_perms}, new={"permissions": sorted(permissions)})
            saved_id = role.id
        return self.reload(Role, saved_id)
