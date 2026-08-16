from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from core.exe_selector import find_main_exe

if TYPE_CHECKING:
    pass


@dataclass
class TextItem:
    file: str
    key: str = ""
    original: str = ""
    translated: str = ""
    context: str = ""
    line: int = 0
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EngineCapabilities:
    """Declarative engine behavior used by pipeline, GUI, and diagnostics."""

    extract: bool = True
    repack: bool = True
    static_patch: bool = True
    runtime_patch: bool = False
    creates_launcher: bool = False
    portable_after_patch: bool = False
    requires_python: bool = False
    requires_frida: bool = False
    needs_external_tool: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "extract": self.extract,
            "repack": self.repack,
            "static_patch": self.static_patch,
            "runtime_patch": self.runtime_patch,
            "creates_launcher": self.creates_launcher,
            "portable_after_patch": self.portable_after_patch,
            "requires_python": self.requires_python,
            "requires_frida": self.requires_frida,
            "needs_external_tool": self.needs_external_tool,
            "notes": list(self.notes),
        }


class EngineBase(ABC):
    name: str = "base"
    label: str = "基础引擎"
    support_level: str = "stable"  # stable / beta / experimental / partial / planned
    supports_extract: bool = True
    supports_repack: bool = True
    detect_priority: int = 80
    limitations: list[str] = []
    capabilities: EngineCapabilities | None = None

    @abstractmethod
    def detect(self, path: Path) -> bool: ...

    @abstractmethod
    def unpack(self, path: Path, workspace: Path) -> list[TextItem]: ...

    @abstractmethod
    def repack(self, items: list[TextItem], workspace: Path) -> None: ...

    def find_exe(self, path: Path) -> Path | None:
        exe = find_main_exe(path, recursive=True)
        if exe:
            return exe
        exts = (".bat", ".sh")
        candidates = []
        game_dir = path if path.is_dir() else path.parent
        for root, dirs, files in game_dir.walk():
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
            for f in files:
                if f.lower().endswith(exts):
                    candidates.append(Path(root) / f)
        if not candidates:
            return None
        return min(candidates, key=lambda p: (p.parent != game_dir, len(p.parts), p.name.lower() not in ("start.bat", "run.bat")))

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        """Return (score, evidence) for engine detection.

        Existing engines only need to implement detect(); this wrapper gives the
        detector enough information to rank candidates and write diagnostics.
        """
        if not self.detect(path):
            return 0, []
        if self.name == "generic":
            return 1, ["通用兜底扫描"]
        return self.detect_priority, [f"{self.label} 特征匹配"]

    def support_summary(self) -> dict:
        capabilities = self.get_capabilities()
        return {
            "name": self.name,
            "label": self.label,
            "support_level": self.support_level,
            "supports_extract": capabilities.extract,
            "supports_repack": capabilities.repack,
            "capabilities": capabilities.to_dict(),
            "limitations": list(self.limitations),
        }

    def get_capabilities(self) -> EngineCapabilities:
        if self.capabilities is not None:
            return self.capabilities
        return EngineCapabilities(
            extract=self.supports_extract,
            repack=self.supports_repack,
            static_patch=self.supports_repack,
        )

    def backup(self, path: Path, workspace: Path):
        backup_dir = workspace / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / path.name
        if path.is_file():
            shutil.copy2(path, dest)
        elif path.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(path, dest)

    def restore(self, workspace: Path):
        backup_dir = workspace / "backup"
        if backup_dir.exists():
            for item in backup_dir.iterdir():
                dest = workspace / item.name if (Path("/") / item.name).is_absolute() is False else Path(item.name)
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)


class EngineRegistry:
    def __init__(self):
        self._engines: list[EngineBase] = []

    def register(self, engine: EngineBase):
        self._engines.append(engine)

    def detect(self, path: Path) -> EngineBase | None:
        candidates = self.detect_candidates(path)
        if candidates:
            return candidates[0][0]
        return None

    def detect_candidates(self, path: Path) -> list[tuple[EngineBase, int, list[str]]]:
        candidates: list[tuple[EngineBase, int, list[str]]] = []
        for order, engine in enumerate(self._engines):
            try:
                score, evidence = engine.detect_confidence(path)
                if score > 0:
                    if score >= 94 and engine.name != "generic":
                        candidates.append((engine, score, evidence or [f"{engine.label} matched"]))
                        break
                    # Stable sort by score, then registration order.
                    candidates.append((engine, score, evidence or [f"{engine.label} 匹配"]))
            except Exception:
                continue
        candidates.sort(key=lambda row: row[1], reverse=True)
        return candidates

    def list_engines(self) -> list[EngineBase]:
        return list(self._engines)

    def detect_generic(self) -> EngineBase | None:
        for engine in self._engines:
            if engine.name == "generic":
                return engine
        return None


registry = EngineRegistry()
