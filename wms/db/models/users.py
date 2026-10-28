from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import ActiveMixin, Base, IdMixin, TimestampMixin, fk
from wms.db.types import UTCDateTime


class Role(IdMixin, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def permission_set(self) -> set[str]:
        return {p.permission for p in self.permissions}


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(String(60), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="permissions")


class User(IdMixin, TimestampMixin, ActiveMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    role_id: Mapped[int] = fk("roles.id", nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    role: Mapped[Role] = relationship(lazy="joined")

    def has(self, permission: str) -> bool:
        return permission in self.role.permission_set

    def __repr__(self) -> str:
        return f"<User {self.username}>"
