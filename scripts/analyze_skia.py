#!/usr/bin/env python3
"""Analyze the Skia codebase: LoC/SLOC breakdown, subsystems, and Chrome/Android usage.

Zero external dependencies (Python 3 stdlib only).

Usage:
    python3 scripts/analyze_skia.py --skia-dir ../skia
    python3 scripts/analyze_skia.py --skia-dir ../skia --section summary
    python3 scripts/analyze_skia.py --skia-dir ../skia --section subsystems
    python3 scripts/analyze_skia.py --skia-dir ../skia --section gni
    python3 scripts/analyze_skia.py --skia-dir ../skia --section matrix --fetch-upstream
    python3 scripts/analyze_skia.py --skia-dir ../skia --section matrix \
        --chromium-build-gn path/to/skia_BUILD.gn --aosp-android-bp path/to/Android.bp
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

CHROMIUM_BUILD_GN_URL = (
    "https://chromium.googlesource.com/chromium/src/+/refs/heads/main/skia/BUILD.gn?format=TEXT"
)
AOSP_ANDROID_BP_URL = (
    "https://android.googlesource.com/platform/external/skia/+/refs/heads/main/Android.bp?format=TEXT"
)

CODE_EXTS: dict[str, str] = {
    ".c": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".cxx": "C/C++",
    ".h": "C/C++",
    ".hh": "C/C++",
    ".hpp": "C/C++",
    ".hxx": "C/C++",
    ".inc": "C/C++",
    ".m": "ObjC/ObjC++",
    ".mm": "ObjC/ObjC++",
    ".rs": "Rust",
    ".sksl": "SkSL",
    ".rts": "SkSL",
    ".rtb": "SkSL",
    ".rtcf": "SkSL",
    ".rta": "SkSL",
    ".lex": "SkSL",
    ".vert": "Shader",
    ".frag": "Shader",
    ".comp": "Shader",
    ".glsl": "Shader",
    ".wgsl": "Shader",
    ".metal": "Shader",
    ".hlsl": "Shader",
    ".S": "Asm",
    ".s": "Asm",
    ".asm": "Asm",
    ".java": "Java/Kotlin",
    ".kt": "Java/Kotlin",
    ".js": "JS/TS",
    ".mjs": "JS/TS",
    ".ts": "JS/TS",
    ".jsx": "JS/TS",
    ".tsx": "JS/TS",
    ".swift": "Swift",
}

BUILD_AND_SCRIPT_EXTS: dict[str, str] = {
    ".py": "Python",
    ".go": "Go",
    ".sh": "Shell",
    ".gn": "Build",
    ".gni": "Build",
    ".bazel": "Build",
    ".bzl": "Build",
    ".cmake": "Build",
    ".bp": "Build",
}

BUILD_FILENAMES = {"BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel"}
C_LIKE_LANGS = {"C/C++", "ObjC/ObjC++", "Rust", "SkSL", "Shader", "JS/TS", "Go", "Java/Kotlin", "Swift"}

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def count_lines(path: Path, lang: str) -> tuple[int, int]:
    """Return (total_lines, sloc) where sloc strips blank lines and comments."""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    total = data.count("\n") + (1 if data and not data.endswith("\n") else 0)
    if lang in C_LIKE_LANGS:
        no_block = BLOCK_COMMENT_RE.sub("", data)
        sloc = sum(1 for line in no_block.splitlines() if LINE_COMMENT_RE.sub("", line).strip())
        return total, sloc
    if lang in {"Python", "Build", "Shell"}:
        sloc = sum(1 for line in data.splitlines() if line.strip() and not line.strip().startswith("#"))
        return total, sloc
    sloc = sum(1 for line in data.splitlines() if line.strip())
    return total, sloc


def get_git_info(skia_dir: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(skia_dir), "log", "-1", "--format=%h (%ai) %s"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "(git metadata unavailable)"


def scan_repo(skia_dir: Path) -> dict[str, tuple[str, int, int]]:
    """Return mapping rel_posix_path -> (lang, total_lines, sloc)."""
    records: dict[str, tuple[str, int, int]] = {}
    for root, dirs, files in os.walk(skia_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            p = Path(root) / f
            ext = p.suffix
            if f in BUILD_FILENAMES:
                lang = "Build"
            elif ext in CODE_EXTS:
                lang = CODE_EXTS[ext]
            elif ext in BUILD_AND_SCRIPT_EXTS:
                lang = BUILD_AND_SCRIPT_EXTS[ext]
            else:
                continue
            rel = p.relative_to(skia_dir).as_posix()
            total, sloc = count_lines(p, lang)
            records[rel] = (lang, total, sloc)
    return records


def is_code_lang(lang: str) -> bool:
    return lang not in {"Build", "Python", "Go", "Shell"}


def is_module_nonprod(rel: str) -> bool:
    parts = rel.split("/")
    sub = parts[2] if len(parts) > 3 else ""
    if sub in {"tests", "gm", "bench", "fuzz", "slides", "utils", "samples"}:
        return True
    if "test" in Path(rel).name.lower():
        return True
    return False


def classify_top_bucket(rel: str, lang: str) -> tuple[str, str]:
    """Classify every code-language file into ('prod'|'vendored'|'nonprod', bucket_name)."""
    parts = rel.split("/")
    top = parts[0]
    if top == "include" and len(parts) > 1 and parts[1] == "third_party":
        return "vendored", "include/third_party (vendored Vulkan headers)"
    if top == "src":
        sub = parts[1]
        if sub == "gpu":
            if len(parts) > 2 and parts[2] == "ganesh":
                g3 = parts[3] if len(parts) > 4 else "(core)"
                if g3 in {"gl", "vk", "mtl", "d3d", "mock"}:
                    return "prod", f"src/gpu/ganesh/{g3}"
                return "prod", "src/gpu/ganesh/(core+ops+geom+effects+etc)"
            if len(parts) > 2 and parts[2] == "graphite":
                g3 = parts[3] if len(parts) > 4 else "(core)"
                if g3 in {"vk", "dawn", "mtl", "sparse_strips", "precompile"}:
                    return "prod", f"src/gpu/graphite/{g3}"
                return "prod", "src/gpu/graphite/(core+render+geom+draw+task+etc)"
            return "prod", "src/gpu/(shared: vk/mtl/tessellate/common)"
        return "prod", f"src/{sub}"
    if top == "include":
        sub = parts[1]
        if sub == "gpu":
            if len(parts) > 2 and parts[2] in {"ganesh", "graphite"}:
                return "prod", f"include/gpu/{parts[2]}"
            return "prod", "include/gpu/(shared)"
        return "prod", f"include/{sub}"
    if top == "rust":
        return "prod", f"rust/{parts[1]}" if len(parts) > 2 else "rust/(root)"
    if top == "client_utils":
        return "prod", "client_utils/android"
    if top == "modules":
        mod = parts[1]
        if is_module_nonprod(rel):
            return "nonprod", "modules/*/tests+gm+slides"
        return "prod", f"modules/{mod}"
    if top == "tests" and lang in {"Shader", "SkSL"}:
        return "nonprod", "tests/sksl (golden shader outputs)"
    if top == "experimental":
        if len(parts) > 2 and parts[1] in {"rust_ico", "rust_jpeg"}:
            return "nonprod", f"experimental/{parts[1]} (built by Chrome)"
        return "nonprod", "experimental/ (other)"
    return "nonprod", top


def categorize_core(fname: str) -> str:
    if "RasterPipeline" in fname:
        return "RasterPipeline (CPU stage VM)"
    if any(k in fname for k in ("Blit", "Blitter", "Sprite", "Swizzle", "Memset", "Opts", "BitmapProcState")):
        return "Blitters, Swizzlers & CPU Opts"
    if any(k in fname for k in ("Scan", "Edge", "AAClip", "AlphaRuns", "Analytic", "Clipper")):
        return "Scan Converters & Edge/Clip"
    if "Mask" in fname:
        return "Masks & MaskFilter/Blur"
    if "Draw" in fname:
        return "Draw / Shadow / Text / Atlas Dispatch"
    if any(k in fname for k in ("Canvas", "Device", "Overdraw", "ClipStack", "Surface")):
        return "Canvas, Device & ClipStack"
    if any(
        k in fname
        for k in (
            "Path", "Stroke", "Geometry", "Matrix", "M44", "Point", "Rect", "RRect",
            "Region", "RSXform", "Quad", "Cubic", "Contour", "Mesh", "Vertices", "Scalar", "Math",
        )
    ):
        return "Path, Geometry, Stroke & Math"
    if any(
        k in fname
        for k in (
            "Picture", "Record", "Flat", "ReadBuffer", "WriteBuffer",
            "Flattenable", "Serial", "BBH", "RTree", "BigPicture",
        )
    ):
        return "SkPicture, Recording & Serialization"
    if any(k in fname for k in ("Font", "Glyph", "Strike", "Scaler", "Typeface", "TextBlob", "Descriptor")):
        return "Font, Glyph, Strike & TextBlob"
    if any(k in fname for k in ("ImageFilter", "Blur", "Gauss")):
        return "ImageFilters & Blur Core"
    if any(
        k in fname
        for k in (
            "Image", "Bitmap", "Pixmap", "PixelRef", "Mipmap", "YUVA", "YUV",
            "Compressed", "SpecialImage", "ReadPixels", "WritePixels",
            "ConvertPixels", "UnPreMultiply", "Texture",
        )
    ):
        return "Bitmap, Pixmap, Image, Mipmap & YUV"
    if any(k in fname for k in ("Color", "Blend", "Runtime", "Shader", "SLType", "Paint", "Capabilities")):
        return "Paint, ColorSpace, Blend & RuntimeEffect"
    if any(
        k in fname
        for k in (
            "Stream", "Data", "String", "Buffer", "Cache", "ResourceCache",
            "TaskGroup", "Executor", "Checksum", "MD5", "Trace", "ATrace",
            "Annotation", "Document", "Graphics", "Lattice", "DistanceField",
            "GlobalInit", "IDChange", "KnownRuntime", "FontMgr",
        )
    ):
        return "Infra, Caches, Streams & Misc"
    return "Base Utilities (Arena/Containers/Sync/etc.)"


def categorize_codec(rel: str) -> str:
    n = Path(rel).name
    if rel.startswith("rust/"):
        return f"rust/{rel.split('/')[1]}"
    if rel.startswith("experimental/rust_"):
        return f"experimental/{rel.split('/')[1]}"
    if "PngRust" in n:
        return "PNG (Rust bridge C++)"
    if "Png" in n:
        return "PNG (libpng / shared base)"
    if "BmpRust" in n:
        return "BMP (Rust bridge C++)"
    if "Bmp" in n:
        return "BMP (C++)"
    if "Ico" in n:
        return "ICO (C++)"
    if "Jpegxl" in n:
        return "JPEG XL (libjxl)"
    if "Jpeg" in n or "JPEG" in n:
        return "JPEG & Gainmap (libjpeg-turbo)"
    if "Webp" in n:
        return "WebP (libwebp)"
    if "Wuffs" in n:
        return "GIF (Wuffs)"
    if "CrabbyAvif" in n or "Avif" in n:
        return "AVIF (CrabbyAvif / Rust)"
    if "Raw" in n or "Tiff" in n:
        return "RAW / DNG / TIFF"
    if "Wbmp" in n:
        return "WBMP"
    if "Hdr" in n or "Gainmap" in n:
        return "HDR / AGTM / Gainmap metadata"
    if "Xmp" in n:
        return "XMP metadata"
    if "Exif" in n:
        return "EXIF (C++)"
    if "ColorProfileRust" in n:
        return "ICC ColorProfile (Rust bridge C++)"
    if "ICC" in n or "ColorProfile" in n:
        return "ICC ColorProfile (C++)"
    if "AndroidCodec" in n:
        return "AndroidCodec wrapper"
    if any(k in n for k in ("Swizzler", "Sampler", "Sampled", "Scaling")):
        return "Swizzler & Sampling infra"
    return "Core Codec / Encoder base"


def categorize_port(rel: str) -> str:
    low = rel.lower()
    if "fontations" in low:
        return "Fontations (Rust + C++ bridge)"
    if "freetype" in low:
        return "FreeType"
    if "_win_dw" in rel:
        return "Windows DirectWrite"
    if "_win." in rel or "_win_" in rel or "WIC" in rel:
        return "Windows GDI / WIC / OS"
    if "_mac_" in rel or "_ios" in rel or "CG." in rel:
        return "Apple CoreText / CoreGraphics / iOS"
    if "fontconfig" in low:
        return "Linux FontConfig / FCI"
    if "android" in low or "NDK" in rel:
        return "Android / NDK FontMgr & ImageGen"
    if "fuchsia" in low:
        return "Fuchsia FontMgr"
    if "custom" in low or "proxy" in low:
        return "Custom / Empty / Proxy FontMgr"
    return "OS / Memory / Log / Init"


def parse_gni_files(skia_dir: Path) -> dict[str, list[str]]:
    """Parse Skia .gni files into variable_name -> list of repo-relative paths."""
    gni_paths = (
        list((skia_dir / "gn").glob("*.gni"))
        + list((skia_dir / "modules").glob("*/*.gni"))
        + list((skia_dir / "rust").glob("*/*.gni"))
        + list((skia_dir / "experimental").glob("*/*.gni"))
    )
    vars_map: dict[str, list[str]] = {}
    raw_exprs: dict[str, list[str]] = {}

    for gp in gni_paths:
        txt = gp.read_text(encoding="utf-8", errors="replace")
        clean = "\n".join(re.sub(r"#.*$", "", line) for line in txt.splitlines())

        local_strs = {
            "_src": "src",
            "_include": "include",
            "_modules": "modules",
            "_tests": "tests",
            "_rust": "rust",
            "_experimental": "experimental",
            "_bench": "bench",
            "_gm": "gm",
            "_tools": "tools",
        }
        for m in re.finditer(r'(_[a-zA-Z0-9_]+)\s*=\s*get_path_info\(\s*"([^"]+)"', clean):
            vname, relp = m.group(1), m.group(2)
            resolved = (gp.parent / relp).resolve()
            try:
                local_strs[vname] = resolved.relative_to(skia_dir.resolve()).as_posix()
            except ValueError:
                pass

        def subst_path(s: str) -> str:
            for k, v in local_strs.items():
                s = s.replace("${" + k + "}", v).replace("$" + k, v)
            s = s.lstrip("/")
            if s.startswith("third_party/skia/"):
                s = s[len("third_party/skia/"):]
            if not (skia_dir / s).exists() and (gp.parent / s).exists():
                try:
                    s = (gp.parent / s).resolve().relative_to(skia_dir.resolve()).as_posix()
                except ValueError:
                    pass
            return s

        for m in re.finditer(r'^\s*([a-zA-Z0-9_]+)\s*=\s*"([^"]+)"', clean, re.MULTILINE):
            vars_map[m.group(1)] = [subst_path(m.group(2))]

        for m in re.finditer(r"^\s*([a-zA-Z0-9_]+)\s*(\+?=)\s*\[(.*?)\]", clean, re.MULTILINE | re.DOTALL):
            vname, op, body = m.group(1), m.group(2), m.group(3)
            items = [subst_path(p) for p in re.findall(r'"([^"]+)"', body)]
            if op == "+=" and vname in vars_map:
                vars_map[vname].extend(items)
            else:
                vars_map[vname] = list(items)

        for m in re.finditer(
            r"^\s*([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z0-9_]+(?:\s*\+\s*[a-zA-Z0-9_]+)+)\s*$",
            clean,
            re.MULTILINE,
        ):
            raw_exprs[m.group(1)] = [x.strip() for x in m.group(2).split("+")]

        for m in re.finditer(r"^\s*([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z][a-zA-Z0-9_]+)\s*$", clean, re.MULTILINE):
            lhs, rhs = m.group(1), m.group(2)
            if rhs not in {"true", "false"}:
                raw_exprs[lhs] = [rhs]

    for _ in range(6):
        for lhs, parts in raw_exprs.items():
            if all(p in vars_map for p in parts):
                combined: list[str] = []
                for p in parts:
                    combined.extend(vars_map[p])
                vars_map[lhs] = combined

    return vars_map


def fetch_gitiles_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        b64 = resp.read().decode("utf-8")
    return base64.b64decode(re.sub(r"\s+", "", b64)).decode("utf-8", errors="replace")


def expand_paired_headers(selected: set[str], all_code_files: set[str]) -> set[str]:
    """Include same-stem headers in same dir or sibling include/ dir for selected sources."""
    expanded = set(selected)
    by_dir_stem: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_mod_stem: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rel in all_code_files:
        p = Path(rel)
        by_dir_stem[(p.parent.as_posix(), p.stem)].append(rel)
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] in {"src", "include"}:
            by_mod_stem[(parts[1], p.stem)].append(rel)
        elif len(parts) >= 4 and parts[0] == "modules":
            by_mod_stem[(f"modules/{parts[1]}", p.stem)].append(rel)

    for rel in list(selected):
        p = Path(rel)
        for peer in by_dir_stem.get((p.parent.as_posix(), p.stem), []):
            if Path(peer).suffix in {".h", ".hh", ".hpp", ".inc"}:
                expanded.add(peer)
        parts = rel.split("/")
        mkey = None
        if len(parts) >= 3 and parts[0] == "src":
            mkey = (parts[1], p.stem)
        elif len(parts) >= 4 and parts[0] == "modules":
            mkey = (f"modules/{parts[1]}", p.stem)
        if mkey:
            for peer in by_mod_stem.get(mkey, []):
                if Path(peer).suffix in {".h", ".hh", ".hpp"} and "/include/" in f"/{peer}":
                    expanded.add(peer)
    return expanded


def add_sksl_builtin_modules(selected: set[str], code_files: set[str]) -> None:
    """SkSL compiles built-in .sksl files into generated/*.minified.sksl included by SkSLModuleDataDefault.cpp."""
    if "src/sksl/SkSLModuleDataDefault.cpp" in selected or "src/sksl/SkSLCompiler.cpp" in selected:
        for rel in code_files:
            if rel.startswith("src/sksl/generated/") or (
                rel.startswith("src/sksl/") and Path(rel).suffix in {".sksl", ".rts", ".rtb"}
            ):
                selected.add(rel)
            if rel in {"src/sksl/GLSL.std.450.h", "src/sksl/spirv.h"}:
                selected.add(rel)


def derive_android_files_from_gni(
    gni: dict[str, list[str]], code_files: set[str]
) -> set[str]:
    """Derive Android libskia + renderengine file set from gn/gn_to_bp.py's active GN flags.

    Matches gn/gn_to_bp.py (HWUI + LayoutLib host + RenderEngine):
    - Core, Effects, ImageFilters, PathOps, Utils, Opts, SKCMS, Android Utils, client_utils/android
    - GPU: Ganesh (core + GL/EGL + Vulkan) + Graphite (core + Vulkan + Precompile) + Shared GPU/VK
      (Excludes Metal, Dawn, D3D, Sparse Strips, Vello)
    - SkSL: core + pipeline + codegen (runtime uses GLSL + SPIR-V + RasterPipeline; Metal/WGSL compiled via group)
    - Codecs: shared + PNG (libpng + Rust PNG) + JPEG + Gainmaps + XMP + WebP + Wuffs GIF +
      BMP (C++) + ICO (C++) + WBMP + RAW/DNG + CrabbyAvif + JPEG XL + AndroidCodec +
      host CG/WIC image generators (for macOS/Windows LayoutLib)
    - Encoders: PNG (libpng + Rust PNG), JPEG (+ Gainmap), WebP, ICC
    - Documents: PDF (+ HarfBuzz subsetter), SVG export (SkSVGCanvas/Device) + XML/DOM
      (Excludes XPS)
    - Fonts/Ports: FreeType + Fontations (Rust+C++) + Custom/Empty FontMgr + OS/Log/Memory
      (Excludes Windows DirectWrite/GDI, Apple CoreText, Linux FontConfig/FCI, Android/NDK FontMgrs, Fuchsia)
    - Modules: skcms ONLY (skottie, sksg, skparagraph, skshaper, skunicode, svg, canvaskit, etc. excluded from libskia)
    """
    vars_android = [
        # Core / effects / pathops / utils / pdf / svg / xml
        "skia_core_public", "skia_core_sources",
        "skia_effects_public", "skia_effects_sources",
        "skia_colorfilters_sources",
        "skia_effects_imagefilter_public", "skia_effects_imagefilter_sources",
        "skia_pathops_public", "skia_pathops_sources",
        "skia_utils_public", "skia_utils_private",
        "skia_pdf_public", "skia_pdf_sources",
        "skia_svg_public", "skia_svg_sources", "skia_svg_writer_public", "skia_svg_writer_sources",
        "skia_xml_sources",
        # GPU: Ganesh (GL + VK) + Graphite (VK + Precompile) + shared
        "skia_gpu_public", "skia_shared_gpu_sources",
        "skia_ganesh_private", "skia_null_gpu_sources",
        "skia_gpu_gl_public", "skia_gpu_gl_private",
        "skia_gpu_vk_public", "skia_gpu_vk_private", "skia_shared_vk_sources",
        "skia_graphite_public", "skia_graphite_sources",
        "skia_graphite_precompile_public", "skia_graphite_precompile_sources",
        "skia_graphite_vk_public", "skia_graphite_vk_sources",
        # SkSL (BUILD.gn compiles skia_sksl_codegen_sources; runtime uses GLSL + SPIR-V + RP)
        "skia_sksl_core_sources", "skia_sksl_core_module_sources",
        "skia_sksl_graphite_modules_sources", "skia_sksl_pipeline_sources",
        "skia_sksl_codegen_sources",
        # Codecs & Encoders
        "skia_codec_public", "skia_codec_shared", "skia_codec_android",
        "skia_codec_png_base", "skia_codec_libpng", "skia_codec_rust_png", "skia_codec_rust_png_public",
        "skia_codec_rust_png_ffi_rs_srcs", "skia_codec_rust_png_ffi_cpp_hdrs",
        "skia_rust_common_ffi_rs_srcs", "skia_rust_common_ffi_cpp_srcs", "skia_rust_common_ffi_cpp_hdrs",
        "skia_codec_jpeg_decode", "skia_codec_jpeg_xmp", "skia_codec_xmp",
        "skia_codec_webp_decode", "skia_codec_wuffs",
        "skia_codec_bmp", "skia_codec_ico", "skia_codec_wbmp", "skia_codec_raw",
        "skia_codec_crabbyavif", "skia_codec_jpegxl",
        "skia_encode_public", "skia_encode_srcs",
        "skia_encode_png_base", "skia_encode_libpng_srcs", "skia_encode_libpng_public",
        "skia_encode_rust_png_srcs", "skia_encode_rust_png_public",
        "skia_encode_jpeg_srcs", "skia_encode_jpeg_public",
        "skia_encode_webp_srcs", "skia_encode_webp_public",
        # Ports (FreeType + Fontations + Custom Empty + OS + LayoutLib host image generators)
        "skia_ports_sources", "skia_ports_freetype_sources",
        "skia_ports_fontmgr_custom_sources", "skia_ports_fontmgr_empty_sources", "skia_ports_fontmgr_empty_public",
        "skia_ports_typeface_fontations_sources", "skia_ports_fontations_bridge_rust_side_sources",
        "skia_fontations_path_bridge_sources",
        "skia_ports_windows_sources",
    ]
    selected: set[str] = set()
    for v in vars_android:
        selected.update(gni.get(v, []))
    for rel in code_files:
        if rel.startswith((
            "src/android/", "include/android/", "client_utils/android/",
            "src/opts/", "modules/skcms/", "src/lazy/", "src/sfnt/",
            "src/gpu/android/", "src/svg/", "include/svg/",
            "src/gpu/ganesh/gl/egl/",
        )):
            selected.add(rel)
        if rel in {
            "src/ports/SkOSFile_posix.cpp", "src/ports/SkOSFile_stdio.cpp", "src/ports/SkOSFile_win.cpp",
            "src/ports/SkLog_android.cpp", "src/ports/SkLog_stdio.cpp", "src/ports/SkLog_win.cpp",
            "src/ports/SkMemory_malloc.cpp", "src/ports/SkDiscardableMemory_none.cpp",
            "src/ports/SkGlobalInitialization_default.cpp",
            "src/ports/SkImageGeneratorCG.cpp", "src/ports/SkImageGeneratorWIC.cpp",
            "src/gpu/ganesh/surface/SkSurface_AndroidFactories.cpp",
            "src/gpu/ganesh/image/SkImage_GaneshFactories_Android.cpp",
            "src/gpu/ganesh/GrAHardwareBufferImageGenerator.cpp",
            "src/gpu/ganesh/GrAHardwareBufferUtils.cpp",
            "src/gpu/ganesh/gl/AHardwareBufferGL.cpp",
            "src/gpu/ganesh/vk/AHardwareBufferVk.cpp",
            "src/codec/SkCrabbyAvifCodec.cpp", "src/codec/SkCrabbyAvifCodec.h",
            "src/codec/SkJpegxlCodec.cpp", "src/codec/SkJpegxlCodec.h",
            "src/codec/SkRawCodec.cpp", "src/codec/SkRawCodec.h",
            "src/codec/SkTiffUtility.cpp", "src/codec/SkTiffUtility.h",
            "src/codec/SkWbmpCodec.cpp", "src/codec/SkWbmpCodec.h",
            "src/codec/SkIcoCodec.cpp", "src/codec/SkIcoCodec.h",
            "src/codec/SkBmpCodec.cpp", "src/codec/SkBmpBaseCodec.cpp",
            "src/codec/SkBmpMaskCodec.cpp", "src/codec/SkBmpRLECodec.cpp",
            "src/codec/SkBmpStandardCodec.cpp",
            "src/codec/SkPngCodec.cpp", "src/codec/SkPngCodec.h",
            "src/codec/SkJpegCodec.cpp", "src/codec/SkJpegDecoderMgr.cpp",
            "src/codec/SkJpegMetadataDecoderImpl.cpp", "src/codec/SkJpegMultiPicture.cpp",
            "src/codec/SkJpegSegmentScan.cpp", "src/codec/SkJpegSourceMgr.cpp",
            "src/codec/SkJpegUtility.cpp", "src/codec/SkWebpCodec.cpp",
            "src/codec/SkWuffsCodec.cpp",
            "src/codec/SkAndroidCodec.cpp", "src/codec/SkAndroidCodecAdapter.cpp",
            "src/codec/SkSampledCodec.cpp", "src/codec/SkExif.cpp",
            "src/codec/SkCodecColorProfile.cpp",
            "src/encode/SkJpegGainmapEncoder.cpp", "src/encode/SkPngEncoderImpl.cpp",
        }:
            selected.add(rel)
    add_sksl_builtin_modules(selected, code_files)
    return expand_paired_headers(selected & code_files, code_files)


def derive_chrome_files(
    gni: dict[str, list[str]], code_files: set[str], chr_gn_text: str
) -> set[str]:
    """Resolve Chromium skia/BUILD.gn variable & direct file references against Skia .gni."""
    chr_vars = set(re.findall(r"\b(skia_[a-zA-Z0-9_]+|skcms_[a-zA-Z0-9_]+)\b", chr_gn_text))
    selected: set[str] = set()
    for v in chr_vars:
        selected.update(gni.get(v, []))
    for p in re.findall(r'"//third_party/skia/([^"]+)"', chr_gn_text):
        selected.add(p)
    for rel in code_files:
        if rel.startswith(("modules/skcms/", "src/opts/")):
            selected.add(rel)
    add_sksl_builtin_modules(selected, code_files)
    return expand_paired_headers(selected & code_files, code_files)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Analyze Skia LoC, subsystems, and Chrome/Android usage.")
    parser.add_argument(
        "--skia-dir",
        type=Path,
        default=repo_root.parent / "skia",
        help="Path to local Skia checkout (default: ../skia).",
    )
    parser.add_argument(
        "--section",
        choices=["all", "summary", "subsystems", "gni", "matrix"],
        default="all",
        help="Which section(s) of the report to print (default: all).",
    )
    parser.add_argument(
        "--chromium-build-gn",
        type=Path,
        default=None,
        help="Optional local path to Chromium skia/BUILD.gn for Chrome cross-referencing.",
    )
    parser.add_argument(
        "--aosp-android-bp",
        type=Path,
        default=None,
        help="Optional local path to AOSP external/skia/Android.bp for AOSP verification.",
    )
    parser.add_argument(
        "--fetch-upstream",
        action="store_true",
        help="Fetch live Chromium skia/BUILD.gn and AOSP external/skia/Android.bp from googlesource.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write report to file instead of stdout.")
    parser.add_argument("--json", type=Path, default=None, help="Optionally write summary metrics as JSON.")
    args = parser.parse_args()

    skia_dir = args.skia_dir.resolve()
    if not skia_dir.is_dir():
        print(f"Error: Skia directory not found: {skia_dir}", file=sys.stderr)
        return 1

    records = scan_repo(skia_dir)
    code_records = {k: v for k, v in records.items() if is_code_lang(v[0])}
    gni = parse_gni_files(skia_dir)

    want_summary = args.section in {"all", "summary"}
    want_subsystems = args.section in {"all", "subsystems"}
    want_gni = args.section in {"all", "gni"}
    want_matrix = args.section in {"all", "matrix"}

    lines: list[str] = []

    def log(s: str = "") -> None:
        lines.append(s)

    prod_buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    vendored_buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    nonprod_buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])

    for rel, (lang, tot, sloc) in code_records.items():
        kind, bucket = classify_top_bucket(rel, lang)
        target = prod_buckets if kind == "prod" else (vendored_buckets if kind == "vendored" else nonprod_buckets)
        target[bucket][0] += 1
        target[bucket][1] += tot
        target[bucket][2] += sloc

    def sum_buckets(bmap: dict[str, list[int]]) -> tuple[int, int, int]:
        return (
            sum(v[0] for v in bmap.values()),
            sum(v[1] for v in bmap.values()),
            sum(v[2] for v in bmap.values()),
        )

    pf, pt, ps = sum_buckets(prod_buckets)
    vf, vt, vs = sum_buckets(vendored_buckets)
    nf, nt, ns = sum_buckets(nonprod_buckets)

    if want_summary:
        log("=== SKIA REPOSITORY SNAPSHOT ===")
        log(f"Path:   {skia_dir}")
        log(f"Commit: {get_git_info(skia_dir)}")
        log(f"Files:  {len(records)} tracked code/build files ({len(code_records)} code-language files)")

        log("\n=== HIGH-LEVEL REPOSITORY SPLIT (Code Languages) ===")
        log(f"  Production Library (src + include[excl. 3p] + rust + modules[prod] + client_utils): {pf:>5} files  {pt:>9} total  {ps:>9} SLOC")
        log(f"  Vendored Headers (include/third_party/vulkan):                                      {vf:>5} files  {vt:>9} total  {vs:>9} SLOC")
        log(f"  Non-Production (tests, gm, bench, tools, dm, fuzz, docs, experimental, mod tests):  {nf:>5} files  {nt:>9} total  {ns:>9} SLOC")
        log(f"  GRAND TOTAL (Code Languages):                                                       {pf+vf+nf:>5} files  {pt+vt+nt:>9} total  {ps+vs+ns:>9} SLOC")

        log("\n=== PRODUCTION LIBRARY BREAKDOWN ===")
        log(f"  {'Chunk':<50} {'Files':>5} {'Total':>9} {'SLOC':>9} {'%SLOC':>7}")
        for cat, (f, t, s) in sorted(prod_buckets.items(), key=lambda x: -x[1][2]):
            log(f"  {cat:<50} {f:>5} {t:>9} {s:>9} {100.0*s/ps:>6.1f}%")
        log(f"  {'TOTAL PRODUCTION':<50} {pf:>5} {pt:>9} {ps:>9} {'100.0%':>7}")

        log("\n=== NON-PRODUCTION & VENDORED BREAKDOWN ===")
        for cat, (f, t, s) in sorted({**vendored_buckets, **nonprod_buckets}.items(), key=lambda x: -x[1][2]):
            log(f"  {cat:<50} {f:>5} {t:>9} {s:>9}")

    gpu_rollup: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    gpu_detail: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    core_buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    cg_targets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    codec_buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    port_buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    mod_prod: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    mod_test: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])

    for rel, (lang, tot, sloc) in code_records.items():
        if rel.startswith(("src/gpu/", "include/gpu/", "include/private/gpu/")):
            rkey = "Ganesh (legacy GPU)" if "/ganesh/" in rel else ("Graphite (next-gen GPU)" if "/graphite/" in rel else "GPU Shared")
            gpu_rollup[rkey][0] += 1; gpu_rollup[rkey][1] += tot; gpu_rollup[rkey][2] += sloc
            parts = rel.split("/")
            if parts[0] == "src":
                if len(parts) == 3:
                    dkey = "src/gpu/(shared root)"
                elif parts[2] in {"ganesh", "graphite"}:
                    dkey = f"src/gpu/{parts[2]}/(core)" if len(parts) == 4 else f"src/gpu/{parts[2]}/{parts[3]}"
                else:
                    dkey = f"src/gpu/{parts[2]}"
            elif parts[1] == "private":
                dkey = f"include/private/gpu/{parts[3]}" if len(parts) > 4 else "include/private/gpu/(root)"
            else:
                dkey = f"include/gpu/{parts[2]}" if len(parts) > 3 else "include/gpu/(root)"
            gpu_detail[dkey][0] += 1; gpu_detail[dkey][1] += tot; gpu_detail[dkey][2] += sloc
        if rel.startswith("src/core/"):
            b = categorize_core(Path(rel).name)
            core_buckets[b][0] += 1; core_buckets[b][1] += tot; core_buckets[b][2] += sloc
        if rel.startswith("src/sksl/codegen/"):
            n = Path(rel).name
            if "SPIRVtoHLSL" in n or "HLSL" in n: t = "HLSL / SPIRVtoHLSL (stubs)"
            elif "SPIRV" in n: t = "SPIR-V (Vulkan)"
            elif "WGSL" in n: t = "WGSL (Dawn / WebGPU)"
            elif "Metal" in n: t = "MSL (Metal)"
            elif "GLSL" in n: t = "GLSL (OpenGL / ES)"
            elif "RasterPipeline" in n: t = "RasterPipeline (CPU RuntimeEffect)"
            elif "PipelineStage" in n: t = "PipelineStage (Ganesh FP/SkSL)"
            else: t = "Shared CodeGen base"
            cg_targets[t][0] += 1; cg_targets[t][1] += tot; cg_targets[t][2] += sloc
        if rel.startswith(("src/codec/", "src/encode/", "rust/", "experimental/rust_ico/", "experimental/rust_jpeg/")):
            b = categorize_codec(rel)
            codec_buckets[b][0] += 1; codec_buckets[b][1] += tot; codec_buckets[b][2] += sloc
        if rel.startswith("src/ports/"):
            b = categorize_port(rel)
            port_buckets[b][0] += 1; port_buckets[b][1] += tot; port_buckets[b][2] += sloc
        if rel.startswith("modules/"):
            m = rel.split("/")[1]
            dst = mod_test if is_module_nonprod(rel) else mod_prod
            dst[m][0] += 1; dst[m][1] += tot; dst[m][2] += sloc

    if want_subsystems:
        log("=== GPU BREAKDOWN (src/gpu + include/gpu + include/private/gpu) ===")
        for k, (f, t, s) in sorted(gpu_rollup.items(), key=lambda x: -x[1][2]):
            log(f"  [ROLLUP] {k:<34} {f:>5} files  {t:>8} total  {s:>8} SLOC")
        for k, (f, t, s) in sorted(gpu_detail.items(), key=lambda x: -x[1][2]):
            log(f"  {k:<40} {f:>5} files  {t:>8} total  {s:>8} SLOC")

        log("\n=== src/core/ FUNCTIONAL BREAKDOWN ===")
        for b, (f, t, s) in sorted(core_buckets.items(), key=lambda x: -x[1][2]):
            log(f"  {b:<44} {f:>4} files  {t:>7} total  {s:>7} SLOC")

        log("\n=== src/sksl/codegen/ PER-TARGET BREAKDOWN ===")
        for k, (f, t, s) in sorted(cg_targets.items(), key=lambda x: -x[1][2]):
            log(f"  {k:<40} {f:>3} files  {t:>7} total  {s:>7} SLOC")

        log("\n=== CODECS, ENCODERS & RUST FFI ===")
        for b, (f, t, s) in sorted(codec_buckets.items(), key=lambda x: -x[1][2]):
            log(f"  {b:<40} {f:>4} files  {t:>7} total  {s:>7} SLOC")

        log("\n=== src/ports/ BY BACKEND / PLATFORM ===")
        for b, (f, t, s) in sorted(port_buckets.items(), key=lambda x: -x[1][2]):
            log(f"  {b:<40} {f:>4} files  {t:>7} total  {s:>7} SLOC")

        log("\n=== modules/ BREAKDOWN (Production vs Tests/GMs/Slides) ===")
        all_mods = sorted(set(mod_prod) | set(mod_test), key=lambda m: -mod_prod[m][2])
        log(f"  {'Module':<22} {'ProdF':>5} {'ProdTot':>8} {'ProdSLOC':>9} | {'TestF':>5} {'TestSLOC':>9}")
        for m in all_mods:
            f1, t1, s1 = mod_prod[m]
            f2, _, s2 = mod_test[m]
            log(f"  {m:<22} {f1:>5} {t1:>8} {s1:>9} | {f2:>5} {s2:>9}")

    if want_gni:
        log("=== SKIA .gni KEY FILEGROUPS ===")
        for key in [
            "skia_sksl_codegen_sources",
            "skia_sksl_glsl_sources",
            "skia_sksl_spirv_sources",
            "skia_sksl_metal_sources",
            "skia_sksl_wgsl_sources",
            "skia_sksl_hlsl_sources",
            "skia_ports_windows_fonts_sources",
            "skia_ports_windows_gdi_sources",
            "skia_ports_windows_sources",
            "skia_ports_fci_sources",
            "skia_ports_fontconfig_sources",
            "skia_ports_fontmgr_coretext_sources",
            "skia_ports_freetype_sources",
            "skia_ports_typeface_fontations_sources",
            "skia_codec_shared",
            "skia_codec_rust_png",
            "skia_codec_rust_bmp",
            "skia_codec_rust_ico",
            "skia_codec_rust_jpeg",
            "skia_graphite_mtl_sources",
            "skia_graphite_vello_sources",
            "skia_graphite_sparse_strips_sources",
        ]:
            if key in gni:
                vals = gni[key]
                sloc_sum = sum(code_records[v][2] for v in vals if v in code_records)
                log(f"  {key:<42} {len(vals):>3} files  {sloc_sum:>6} SLOC  -> {', '.join(Path(v).name for v in vals[:6])}")

    if want_matrix:
        chr_text = None
        aosp_text = None
        if args.chromium_build_gn:
            chr_text = args.chromium_build_gn.read_text(encoding="utf-8", errors="replace")
        elif args.fetch_upstream:
            chr_text = fetch_gitiles_text(CHROMIUM_BUILD_GN_URL)

        if args.aosp_android_bp:
            aosp_text = args.aosp_android_bp.read_text(encoding="utf-8", errors="replace")
        elif args.fetch_upstream:
            aosp_text = fetch_gitiles_text(AOSP_ANDROID_BP_URL)

        prod_files = {
            rel for rel, (lang, _, _) in code_records.items() if classify_top_bucket(rel, lang)[0] == "prod"
        }
        android_set = derive_android_files_from_gni(gni, set(code_records))
        log("=== CHROME vs. ANDROID USAGE MATRIX (Production Library) ===")
        log(f"  Android (derived from gn_to_bp.py + .gni): {len(android_set & prod_files)} prod files, {sum(code_records[r][2] for r in android_set & prod_files)} SLOC")

        if chr_text is not None:
            chrome_set = derive_chrome_files(gni, set(code_records), chr_text)
            chrome_prod = chrome_set & prod_files
            chrome_exp = chrome_set - prod_files
            android_prod = android_set & prod_files
            both = chrome_prod & android_prod
            chrome_only = chrome_prod - android_prod
            android_only = android_prod - chrome_prod
            neither = prod_files - chrome_prod - android_prod

            def fmt_set(s: set[str]) -> str:
                return f"{len(s):>5} files  {sum(code_records[r][2] for r in s):>8} SLOC ({100.0*sum(code_records[r][2] for r in s)/ps:5.1f}%)"

            log(f"  Chrome (from skia/BUILD.gn + .gni):        {fmt_set(chrome_prod)} (+ {len(chrome_exp)} experimental/rust_* files, {sum(code_records[r][2] for r in chrome_exp)} SLOC)")
            log(f"  Both Chrome & Android:                     {fmt_set(both)}")
            log(f"  Chrome ONLY (not Android):                 {fmt_set(chrome_only)}")
            log(f"  Android ONLY (not Chrome):                 {fmt_set(android_only)}")
            log(f"  NEITHER Chrome nor Android:                {fmt_set(neither)}")

            for title, subset in [
                ("CHROME ONLY (by bucket)", chrome_only),
                ("ANDROID ONLY (by bucket)", android_only),
                ("NEITHER CHROME NOR ANDROID (by bucket)", neither),
            ]:
                log(f"\n  --- {title} ---")
                bmap: dict[str, list[int]] = defaultdict(lambda: [0, 0])
                samples: dict[str, list[str]] = defaultdict(list)
                for r in sorted(subset):
                    _, b = classify_top_bucket(r, code_records[r][0])
                    bmap[b][0] += 1
                    bmap[b][1] += code_records[r][2]
                    if len(samples[b]) < 4:
                        samples[b].append(Path(r).name)
                for b, (f, s) in sorted(bmap.items(), key=lambda x: -x[1][1]):
                    if s >= 50:
                        log(f"    {b:<46} {f:>4} files  {s:>7} SLOC  e.g. {', '.join(samples[b])}")
            log(f"\n  --- SUMMARY TOTALS (Production Library = {ps} SLOC across {pf} files) ---")
            log(f"    Chrome total (prod):        {fmt_set(chrome_prod)} (+ {len(chrome_exp)} experimental/rust_* files, {sum(code_records[r][2] for r in chrome_exp)} SLOC)")
            log(f"    Android total (prod):       {fmt_set(android_prod)}")
            log(f"    Both Chrome & Android:      {fmt_set(both)}")
            log(f"    Chrome ONLY:                {fmt_set(chrome_only)}")
            log(f"    Android ONLY:               {fmt_set(android_only)}")
            log(f"    NEITHER Chrome nor Android: {fmt_set(neither)}")
        else:
            log("  (Pass --chromium-build-gn <path> or --fetch-upstream to compute the full 4-way Chrome/Android/Neither matrix.)")

        if aosp_text is not None:
            target_re = re.compile(r"^(\w+)\s*\{\s*\n(.*?)^\}", re.MULTILINE | re.DOTALL)
            aosp_libskia: set[str] = set()
            aosp_re: set[str] = set()
            for m in target_re.finditer(aosp_text):
                body = m.group(2)
                nm = re.search(r'name:\s*"([^"]+)"', body)
                if not nm:
                    continue
                tname = nm.group(1)
                srcs = re.findall(r'"((?:src|modules|rust|client_utils)/[^"]+)"', body)
                if tname in {"libskia", "skia_arch_defaults"}:
                    aosp_libskia.update(s for s in srcs if not s.endswith("/"))
                elif tname == "libskia_renderengine":
                    aosp_re.update(s for s in srcs if not s.endswith("/"))
            log(f"\n  AOSP Android.bp snapshot check: libskia={len(aosp_libskia)} src entries, libskia_renderengine={len(aosp_re)} src entries")

    report_text = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(report_text, encoding="utf-8")
    else:
        sys.stdout.write(report_text)

    if args.json:
        summary = {
            "skia_dir": str(skia_dir),
            "git_commit": get_git_info(skia_dir),
            "production": {"files": pf, "total_lines": pt, "sloc": ps, "buckets": dict(prod_buckets)},
            "vendored": {"files": vf, "total_lines": vt, "sloc": vs, "buckets": dict(vendored_buckets)},
            "non_production": {"files": nf, "total_lines": nt, "sloc": ns, "buckets": dict(nonprod_buckets)},
            "gpu_rollup": dict(gpu_rollup),
            "gpu_detail": dict(gpu_detail),
            "core_buckets": dict(core_buckets),
            "sksl_codegen": dict(cg_targets),
            "codecs": dict(codec_buckets),
            "ports": dict(port_buckets),
            "modules_prod": dict(mod_prod),
            "modules_test": dict(mod_test),
        }
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
