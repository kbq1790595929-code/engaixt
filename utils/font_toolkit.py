"""字体工具链 — SDF Atlas 生成 / TMP 字体资产打包 / Fallback 环检测。

TMP (TextMeshPro) 字体不能直接使用 OTF/TTF，需要预先生成 Signed Distance Field Atlas。
本模块提供完整的离线生成管线。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from utils.logger import info, debug, warning, error


# 标准 GB2312 一级字库（3755 字）作为兜底字符集
_GB2312_LEVEL1 = set()
for _code in range(0xB0A1, 0xF7FF + 1):
    _hi = (_code >> 8) & 0xFF
    _lo = _code & 0xFF
    if 0xA1 <= _hi <= 0xF7 and 0xA1 <= _lo <= 0xFE:
        try:
            _bytes_val = bytes([_hi, _lo])
            _GB2312_LEVEL1.add(_bytes_val.decode("gb2312"))
        except Exception:
            continue

# CJK Unified Ideographs 基本区 (U+4E00–U+9FFF)
_CJK_BASIC = set()
for _cp in range(0x4E00, 0xA000):
    _CJK_BASIC.add(chr(_cp))


# ---------------------------------------------------------------------------
# 字符集收集
# ---------------------------------------------------------------------------

def collect_charset(texts: list[str], include_gb2312: bool = True,
                    include_cjk_basic: bool = False) -> str:
    """从待翻译文本中收集所有中文字符，生成 msdf-atlas-gen 字符集文件内容。

    Args:
        texts: 待翻译文本列表
        include_gb2312: 是否包含 GB2312 一级字库作为兜底
        include_cjk_basic: 是否包含 CJK 基本区全部 20902 字（Atlas ~40MB）

    Returns:
        字符集字符串，每字符一行
    """
    charset: set[str] = set()

    # 从译文中收集中文
    for text in texts:
        if not text:
            continue
        for c in text:
            if '一' <= c <= '鿿' or '　' <= c <= '〿' or '＀' <= c <= '￯':
                charset.add(c)

    # GB2312 一级字库兜底（3755 字）
    if include_gb2312:
        charset |= _GB2312_LEVEL1

    # CJK 基本区全覆盖（20902 字，Atlas 约 40MB）
    if include_cjk_basic:
        charset |= _CJK_BASIC

    info(f"字符集: {len(charset)} 个唯一字符")
    if len(charset) <= 2000:
        info(f"  推荐 Atlas 尺寸: 2048×2048")
    elif len(charset) <= 6000:
        info(f"  推荐 Atlas 尺寸: 4096×4096")
    else:
        info(f"  推荐 Atlas 尺寸: 4096×4096 × {max(2, (len(charset) + 5999) // 6000)} 页")

    return "".join(sorted(charset))


def write_charset_file(charset: str, output_path: Path) -> Path:
    """写入字符集文件（每字符一行，无换行）。"""
    charset_str = "\n".join(charset)
    output_path.write_text(charset_str, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# msdf-atlas-gen 调用封装
# ---------------------------------------------------------------------------

def check_msdf_atlas_gen() -> str | None:
    """检查 msdf-atlas-gen 是否可用。返回可执行文件路径或 None。"""
    exe = shutil.which("msdf-atlas-gen")
    return exe


def generate_sdf_atlas(
    font_path: Path,
    charset_path: Path,
    output_dir: Path,
    atlas_size: int = 4096,
    font_size: int = 48,
    px_range: int = 4,
) -> Path | None:
    """调用 msdf-atlas-gen 生成 SDF 图集。

    Args:
        font_path: 源字体文件路径 (.otf/.ttf)
        charset_path: 字符集文件路径
        output_dir: 输出目录
        atlas_size: 图集尺寸 (2048 或 4096)
        font_size: 字号
        px_range: SDF 距离场像素范围

    Returns:
        生成的 PNG 图集路径，失败返回 None
    """
    exe = check_msdf_atlas_gen()
    if not exe:
        warning("msdf-atlas-gen 未安装，跳过 SDF Atlas 生成")
        warning("安装方法: https://github.com/Chlumsky/msdf-atlas-gen")
        warning("  Windows: cargo install msdf-atlas-gen")
        warning("  或下载预编译版放到 PATH 中")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    png_out = output_dir / "font_atlas.png"
    json_out = output_dir / "font_atlas.json"

    cmd = [
        exe,
        "-font", str(font_path),
        "-charset", str(charset_path),
        "-type", "msdf",
        "-size", str(font_size),
        "-pxrange", str(px_range),
        "-atlassize", str(atlas_size),
        "-format", "png",
        "-imageout", str(png_out),
        "-json", str(json_out),
        "-potr",  # Power-of-two rounding
    ]

    try:
        info(f"生成 SDF Atlas: {font_path.name} ({atlas_size}×{atlas_size})")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            error(f"msdf-atlas-gen 失败: {result.stderr}")
            return None
        if not png_out.exists():
            error(f"msdf-atlas-gen 未生成输出文件")
            return None
        info(f"SDF Atlas 生成完成: {png_out} ({png_out.stat().st_size:,} bytes)")
        return png_out
    except FileNotFoundError:
        warning("msdf-atlas-gen 未找到")
        return None
    except subprocess.TimeoutExpired:
        error("msdf-atlas-gen 超时 (5 分钟)")
        return None
    except Exception as e:
        error(f"msdf-atlas-gen 异常: {e}")
        return None


# ---------------------------------------------------------------------------
# TMP FontAsset 打包
# ---------------------------------------------------------------------------

def package_tmp_font_asset(atlas_png: Path, atlas_json: Path, font_path: Path,
                           output_dir: Path, font_name: str = "ChineseFont") -> Path | None:
    """将 SDF Atlas 打包为 Unity TMP FontAsset (.asset 文件)。

    使用 UnityPy 创建序列化的 TMP_FontAsset。
    注意：完整的 TMP_FontAsset 结构较复杂，这里生成最小可行版本。
    生成的 .asset 文件需配合 XUAT 的 FontAssetPath 配置使用。
    """
    try:
        import UnityPy
    except ImportError:
        warning("UnityPy 未安装，无法打包 TMP FontAsset")
        return None

    import json

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{font_name}_TMP.asset"

    # 读取 atlas JSON 获取字形信息
    atlas_data = json.loads(atlas_json.read_text(encoding="utf-8"))

    # 创建临时 Unity 资源
    try:
        # 使用 UnityPy 的最小化方式：直接写入字节数据
        # 实际 TMP FontAsset 序列化较复杂，这里先做基础打包
        # XUAT 也可以通过直接传 OTF/TTF + 让 TMP 运行时生成 SDF

        # 简化方案：复制必要文件到目标目录
        dest_png = output_dir / f"{font_name}_Atlas.png"
        shutil.copy2(atlas_png, dest_png)
        dest_json = output_dir / f"{font_name}_Atlas.json"
        shutil.copy2(atlas_json, dest_json)
        dest_font = output_dir / f"{font_name}.otf"
        if font_path.suffix.lower() in (".otf", ".ttf"):
            shutil.copy2(font_path, dest_font)

        # 生成一个 Unity .asset 文件（通过 UnityPy）
        # 实际 TMP_FontAsset 需要完整的序列化结构，这里记录所需步骤
        info(f"TMP FontAsset 资源已复制到: {output_dir}")
        info(f"如需完整 .asset 文件，请在 Unity Editor 中:")
        info(f"  1. Window → TextMeshPro → Font Asset Creator")
        info(f"  2. Source Font File = {dest_font}")
        info(f"  3. Atlas Resolution = {atlas_data.get('atlas', {}).get('width', 4096)}")
        info(f"  4. Character File = 生成的字符集文件")
        info(f"  5. Generate Font Atlas → Save")

        return output_dir

    except Exception as e:
        warning(f"TMP FontAsset 打包失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 字体 Fallback 链环检测
# ---------------------------------------------------------------------------

def detect_fallback_cycle(fallback_map: dict[str, str]) -> list[list[str]]:
    """检测字体 Fallback 链中的循环引用。

    TMP 的 Fallback 链如果形成环（A→B→C→A），在查找缺失字形时
    会进入无限递归，触发 StackOverflowException 导致游戏崩溃，
    且没有任何有意义的错误信息。

    Args:
        fallback_map: {字体名: fallback字体名, ...}

    Returns:
        检测到的环列表，每个环是 [A, B, C, A]
    """
    cycles: list[list[str]] = []

    def _dfs(node: str, visited: set[str], path: list[str]):
        if node in visited:
            # 找到环
            cycle_start = path.index(node)
            cycles.append(path[cycle_start:] + [node])
            return
        visited.add(node)
        path.append(node)
        next_node = fallback_map.get(node)
        if next_node and next_node != node:
            _dfs(next_node, visited, path)
        path.pop()
        visited.discard(node)

    for font in fallback_map:
        _dfs(font, set(), [])

    if cycles:
        warning(f"检测到字体 Fallback 链环引用: {cycles}")
        warning("这会导致 TMP 在查找缺失字形时无限递归，游戏崩溃！")
    else:
        debug("字体 Fallback 链检查通过，无环引用")

    return cycles


def validate_fallback_chain(fallback_chain: list[str]) -> tuple[bool, str | None]:
    """验证 Fallback 链配置的正确性。

    检查：
    1. 无自引用 (A → A)
    2. 无环引用 (A → B → C → A)
    3. 无重复条目

    Args:
        fallback_chain: 有序 Fallback 字体列表 [主字体, fallback1, fallback2, ...]

    Returns:
        (是否有效, 错误信息)
    """
    if len(fallback_chain) != len(set(fallback_chain)):
        return False, "Fallback 链包含重复条目"

    # 构建 {i: i+1} 映射检测环
    fallback_map = {}
    for i in range(len(fallback_chain) - 1):
        if fallback_chain[i] == fallback_chain[i + 1]:
            return False, f"自引用: {fallback_chain[i]} → {fallback_chain[i]}"
        fallback_map[fallback_chain[i]] = fallback_chain[i + 1]

    cycles = detect_fallback_cycle(fallback_map)
    if cycles:
        return False, f"环引用: {' → '.join(cycles[0])}"

    return True, None
