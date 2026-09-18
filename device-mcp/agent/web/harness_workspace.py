"""Native Harness 的每 Run 文件工作区与路径边界。"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessWorkspace:
    root: Path
    input_dir: Path
    work_dir: Path
    output_dir: Path

    @classmethod
    def create(cls, attempt_dir: str | Path) -> "HarnessWorkspace":
        root = Path(attempt_dir).resolve() / "workspace"
        input_dir = root / "input"
        work_dir = root / "work"
        output_dir = root / "output"
        for directory in (input_dir, work_dir, output_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return cls(root, input_dir, work_dir, output_dir)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def import_file(self, source: str | Path) -> dict:
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_file():
            raise ValueError("附件不是普通文件")
        name = source_path.name
        source_digest = self._digest(source_path)
        destination = self.input_dir / name
        if destination.exists():
            if self._digest(destination) == source_digest:
                return {
                    "filename": destination.name,
                    "path": str(destination),
                    "sha256": source_digest,
                    "bytes": destination.stat().st_size,
                }
            destination = self.input_dir / (
                f"{source_path.stem}-{source_digest[:10]}{source_path.suffix}"
            )
            if destination.exists() and self._digest(destination) == source_digest:
                return {
                    "filename": destination.name,
                    "path": str(destination),
                    "sha256": source_digest,
                    "bytes": destination.stat().st_size,
                }
        shutil.copy2(source_path, destination)
        try:
            destination.chmod(0o444)
        except OSError:
            pass
        return {
            "filename": destination.name,
            "path": str(destination),
            "sha256": source_digest,
            "bytes": destination.stat().st_size,
        }

    def resolve(self, raw: str | Path, *, write: bool = False, must_exist: bool = False) -> Path:
        value = Path(str(raw or ""))
        candidate = value if value.is_absolute() else self.work_dir / value
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise ValueError("路径不存在或无法解析") from exc
        allowed = (self.work_dir, self.output_dir) if write else (
            self.input_dir, self.work_dir, self.output_dir,
        )
        if not any(resolved == root or root in resolved.parents for root in allowed):
            raise ValueError("路径超出当前 Run 工作区")
        if write and (resolved == self.input_dir or self.input_dir in resolved.parents):
            raise ValueError("input 目录只读")
        # resolve() 会展开已有 symlink；对将创建的文件还要检查最近的已存在父目录。
        parent = resolved if resolved.exists() and resolved.is_dir() else resolved.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        real_parent = Path(os.path.realpath(parent))
        if not any(real_parent == root or root in real_parent.parents for root in allowed):
            raise ValueError("符号链接逃逸当前 Run 工作区")
        return resolved
