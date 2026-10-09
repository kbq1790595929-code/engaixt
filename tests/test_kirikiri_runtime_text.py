import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
CASES = [
    ("punctuation", "\u300e\u2026\u2026\u2026\u2026\u300f", "", True),
    ("punctuation", "...!?", "", True),
    ("punctuation", "\u3042\u308c\u306f\u2026\u2026\u3002", "", False),
    ("punctuation", "\u300c\u3058\u3063\u2026\u2026\u300d", "", False),
    ("punctuation", "  ", "", False),
    ("evidence", "\u6700\u524d\u9762\u9ed2", "\u6700\u524d\u9762\u9ed2", False),
    ("evidence", "\u30ed\u30a6\u30bd\u30af", "\u30ed\u30a6\u30bd\u30af", False),
    ("evidence", "\u30e1\u30c3\u30bb\u30fc\u30b8\u30a6\u30a3\u30f3\u30c9\u30a6",
     "\u30e1\u30c3\u30bb\u30fc\u30b8\u30a6\u30a3\u30f3\u30c9\u30a6", False),
    ("evidence", "\u3048\u3063", "\u3048\u3063", True),
    ("evidence", "\u3000\u3048\u3063", "\u3048\u3063", True),
    ("evidence", "\u30c9\u30f3\n", "\u30c9\u30f3", True),
    ("evidence", "[text]\u52dd\u5229[r]", "\u52dd\u5229\n", False),
    ("evidence", "\u300e\u2026\u2026\u2026\u2026\u300f",
     "\u300e\u2026\u2026\u2026\u2026\u300f", True),
    ("evidence", "\u3042\u308c\u306f\u2026\u2026\u3002",
     "\u3042\u308c\u306f\u2026\u2026\u3002", True),
]


def _wide_literal(text):
    return 'L"' + "".join(f"\\u{ord(ch):04x}" if ord(ch) >= 32 else f"\\x{ord(ch):02x}" for ch in text) + '"'


@pytest.fixture(scope="module")
def native_text_probe(tmp_path_factory):
    compiler = ROOT / "native/toolchains/llvm-mingw-20260616-msvcrt-i686/bin/i686-w64-mingw32-clang++.exe"
    if os.name != "nt":
        compiler = shutil.which("clang++") or shutil.which("g++")
    elif not compiler.exists():
        compiler = shutil.which("i686-w64-mingw32-clang++.exe")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    tmp = tmp_path_factory.mktemp("krkr_text_native")
    source = tmp / "probe.cpp"
    statements = []
    for index, (kind, raw, visible, expected) in enumerate(CASES):
        expression = (
            f"kirikiri_text::IsPunctuationOnly({_wide_literal(raw)})"
            if kind == "punctuation"
            else f"kirikiri_text::HasUtf8DialogueEvidence({_wide_literal(raw)}, {_wide_literal(visible)})"
        )
        statements.append(f"case {index}: return {expression} == {'true' if expected else 'false'} ? 0 : 1;")
    source.write_text(
        '#include "kirikiri_runtime_text.h"\n#include <cstdlib>\n'
        'int main(int argc, char **argv) { if (argc != 2) return 2; switch(std::atoi(argv[1])) {\n'
        + "\n".join(statements) + "\n default: return 3; } }\n",
        encoding="ascii",
    )
    executable = tmp / ("probe.exe" if os.name == "nt" else "probe")
    result = subprocess.run(
        [str(compiler), "-std=c++17", "-static-libgcc", "-static-libstdc++",
         "-I", str(ROOT / "native/kirikiri_runtime"), str(source), "-o", str(executable)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return executable


@pytest.mark.parametrize("index", range(len(CASES)))
def test_native_runtime_text_provenance(native_text_probe, index):
    result = subprocess.run([str(native_text_probe), str(index)], timeout=10)
    assert result.returncode == 0, CASES[index]
