"""Application settings (config.json) - changed only through here so every change is audited."""

from __future__ import annotations

from wms.core.permissions import P
from wms.services import audit
from wms.services.base import BaseService

EDITABLE = ("company_name", "auto_backup_on_exit", "auto_backup_hours", "backups_to_keep", "backup_dir",
            "backup_copy_dir", "max_attachment_mb")


class SettingsService(BaseService):
    def save_general(self, changes: dict) -> dict:
        """Apply and save the editable settings; returns the changed values. Unknown keys are refused."""
        self.require(P.SETTINGS_MANAGE)
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")
        cfg = self.ctx.config
        old = {k: getattr(cfg, k) for k in EDITABLE}
        for k, v in changes.items():
            setattr(cfg, k, v)
        cfg.company_name = (cfg.company_name or "").strip() or "My Company Ltd"
        cfg.backups_to_keep = max(int(cfg.backups_to_keep or 30), 3)
        cfg.auto_backup_hours = min(max(int(cfg.auto_backup_hours or 0), 0), 24 * 7)
        cfg.backup_dir = (cfg.backup_dir or "").strip()
        cfg.backup_copy_dir = (cfg.backup_copy_dir or "").strip()
        cfg.max_attachment_mb = min(max(int(cfg.max_attachment_mb or 25), 1), 200)
        new = {k: getattr(cfg, k) for k in EDITABLE}
        try:
            cfg.save()
        except Exception:
            for k, v in old.items():
                setattr(cfg, k, v)
            raise
        o, n = audit.diff(old, new)
        if n:
            with self.db.session() as s:
                audit.record(s, self.ctx, "SETTINGS_CHANGED", "SETTING", None, "Settings", old=o, new=n)
        return n
