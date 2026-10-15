"""Fail release checks if approved branding is missing, unused, or stale."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from html.parser import HTMLParser
from pathlib import Path


class _BrandImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.divs: list[bool] = []
        self.sources: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div":
            self.divs.append(
                (self.divs[-1] if self.divs else False)
                or "brand-mark" in attributes.get("class", "").split()
            )
        if tag == "img" and self.divs and self.divs[-1]:
            self.sources.append(attributes.get("src", ""))

    def handle_endtag(self, tag):
        if tag == "div" and self.divs:
            self.divs.pop()


def verify_web_assets(root: Path, branding: dict) -> None:
    for relative, expected in branding["assets"].items():
        path = root / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Branding differs from {branding['reference']}: {path}")
    parser = _BrandImages()
    parser.feed((root / "web" / "index.html").read_text(encoding="utf-8"))
    if parser.sources != ["logo.png"]:
        raise ValueError(f"GUI brand-mark must use logo.png, not a fallback SVG: {root}")
    css = (root / "web" / "style.css").read_text(encoding="utf-8")
    if ".brand-mark img" not in css or "object-fit: contain" not in css:
        raise ValueError(f"GUI logo sizing rules missing: {root}")


def _ico_frames(path: Path) -> list[tuple[int, int, int, str]]:
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError("Truncated ICO header")
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved != 0 or kind != 1 or count == 0 or 6 + count * 16 > len(data):
        raise ValueError("Invalid ICO directory")
    frames = []
    for index in range(count):
        width, height, _, _, _, bits, size, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + index * 16,
        )
        if size == 0 or offset < 6 + count * 16 or offset + size > len(data):
            raise ValueError("Invalid ICO image offset")
        frames.append((width or 256, height or 256, bits,
                       hashlib.sha256(data[offset:offset + size]).hexdigest()))
    return frames


def verify_exe_icon(executable: Path, icon: Path) -> None:
    import pefile

    pe = pefile.PE(str(executable), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        resources = {entry.id: entry for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries}
        if 3 not in resources or 14 not in resources:
            raise ValueError(f"Application icon resources missing: {executable}")
        images = {entry.id: entry for entry in resources[3].directory.entries}
        group = resources[14].directory.entries[0]
        expected = sorted(_ico_frames(icon))
        for language in group.directory.entries:
            resource = language.data.struct
            data = pe.get_data(resource.OffsetToData, resource.Size)
            if len(data) < 6:
                raise ValueError("Truncated executable icon directory")
            reserved, kind, count = struct.unpack_from("<HHH", data)
            if reserved != 0 or kind != 1 or 6 + count * 14 > len(data):
                raise ValueError("Invalid executable icon directory")
            frames = []
            for index in range(count):
                width, height, _, _, _, bits, size, identifier = struct.unpack_from(
                    "<BBBBHHIH", data, 6 + index * 14,
                )
                image = images[identifier]
                variants = image.directory.entries
                image_language = next((entry for entry in variants if entry.id == language.id), variants[0])
                payload = image_language.data.struct
                if payload.Size != size:
                    raise ValueError("Executable icon image size mismatch")
                frames.append((width or 256, height or 256, bits,
                               hashlib.sha256(pe.get_data(payload.OffsetToData, size)).hexdigest()))
            if sorted(frames) != expected:
                raise ValueError(f"Executable contains a stale application icon: {executable}")
    finally:
        pe.close()


def verify_distribution(dist: Path, branding: dict) -> None:
    verify_web_assets(dist / "_internal", branding)
    executable = next((dist / name for name in ("game-translator.exe", "EngAixt.exe")
                       if (dist / name).is_file()), None)
    if executable is None:
        raise ValueError(f"Distribution executable missing: {dist}")
    verify_exe_icon(executable, dist / "_internal" / "web" / "app.ico")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    branding = json.loads(Path(__file__).with_name("branding.json").read_text(encoding="utf-8"))
    try:
        verify_web_assets(args.source_root, branding)
        if args.dist:
            verify_distribution(args.dist, branding)
        if args.exe:
            verify_exe_icon(args.exe, args.source_root / "web" / "app.ico")
    except (ValueError, OSError, KeyError, AttributeError, struct.error) as exc:
        print(f"BRANDING CHECK FAILED: {exc}")
        return 1
    print(f"Branding matches {branding['reference']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
