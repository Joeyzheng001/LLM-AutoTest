"""F-ORCH-02：阻止旧进程使用新提示词/契约开始下一轮任务。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path


class RuntimeRevision:
    """记录进程创建时的代码和契约指纹；不采集配置或任务密钥。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.loaded_at = datetime.now(UTC).isoformat()
        self.revision = self._fingerprint()

    def _fingerprint(self) -> str:
        files: set[Path] = set()
        for service in ("common", "orchestrator", "test-executor"):
            files.update((self.root / "services" / service / "src").rglob("*.py"))
        files.update((self.root / "prompts").rglob("*.md"))
        files.update((self.root / "schemas").rglob("*.json"))
        files.update((self.root / "schemas").rglob("*.yaml"))
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.relative_to(self.root).as_posix().encode("utf-8"))
            digest.update(b"\x00")
            digest.update(path.read_bytes())
            digest.update(b"\x00")
        return digest.hexdigest()[:16]

    @property
    def restart_required(self) -> bool:
        """工作区内容与本进程启动版本不同即拒绝新任务。"""
        return self._fingerprint() != self.revision
