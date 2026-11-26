# Analysis Scripts

This directory contains reproducible analysis scripts used to generate the codebase measurements and usage breakdowns in [`../first_pass.md`](../first_pass.md).

## `analyze_skia.py`

Zero-dependency Python 3 script that scans a local Skia checkout (default `../skia`) and reports:

1. **Repository & Production LoC / SLOC breakdown** (`--section summary`)
   - Separates **Production Library** code (`src/`, `include/` excluding vendored `include/third_party/`, `rust/`, `client_utils/`, and production files in `modules/`) from **Vendored Third-Party Headers** (`include/third_party/vulkan/`, ~430K lines) and **Non-Production** code (`tests/`, golden shader outputs in `tests/sksl/`, `gm/`, `bench/`, `dm/`, `fuzz/`, `tools/`, `docs/`, `experimental/`, and `modules/*/tests+gm+slides`).
   - Reports both **Total lines** and **SLOC** (non-blank, non-comment lines, stripping C/C++/Rust/SkSL `//` and `/* ... */` comments and `#` comments in build/script files).
2. **Subsystem deep dives** (`--section subsystems`)
   - **GPU**: Ganesh vs. Graphite vs. shared GPU code, broken down by API backend (GL, Vulkan, Metal, D3D, Dawn) and internal components (ops, render, sparse_strips, precompile, geometry, effects, compute, etc.).
   - **`src/core/`**: Functional breakdown (paths/geometry, scan converters, canvas/device, text/strikes, blitters/opts, SkPicture/serialization, RasterPipeline, color/blend, image filters, base utilities).
   - **`src/sksl/`**: Front-end, IR, analysis, transforms, builtin modules, and per-backend `codegen/` (SPIR-V, WGSL, MSL, GLSL, RasterPipeline, PipelineStage, HLSL).
   - **Codecs, Encoders & Rust FFI**: `src/codec/`, `src/encode/`, `rust/`, and `experimental/rust_*` grouped by format and C++ vs. Rust.
   - **Ports**: `src/ports/` grouped by font & platform backend (DirectWrite, Fontations, FreeType, Android/NDK, CoreText, GDI/WIC, FontConfig, Fuchsia, Custom).
   - **Modules**: Production SLOC vs. test/GM/slide SLOC for every module under `modules/`.
3. **Skia `.gni` filegroup inspection** (`--section gni`)
   - Parses `gn/*.gni`, `modules/*/*.gni`, `rust/*/*.gni`, and `experimental/*/*.gni` to show exact file counts and SLOC for key build filegroups (e.g., SkSL codegen targets, platform font ports, Rust codecs, Graphite Metal/Vello/sparse strips).
4. **Chrome vs. Android vs. Neither usage matrix** (`--section matrix`)
   - Derives Android's active Skia file set from Skia's own `gn/gn_to_bp.py` flags (HWUI + LayoutLib host + SurfaceFlinger RenderEngine) resolved against `gn/*.gni`.
   - When given Chromium's `skia/BUILD.gn` (via `--chromium-build-gn <path>` or `--fetch-upstream`), resolves every `skia_*` / `skcms_*` filegroup imported by Chromium and computes the 4-way partition of Skia's production library: **Both Chrome & Android**, **Chrome only**, **Android only**, and **Neither**.
   - Optionally cross-checks against AOSP's `external/skia/Android.bp` (via `--aosp-android-bp <path>` or `--fetch-upstream`).

### How to run

```bash
# Full report (all sections; matrix uses offline Android analysis unless Chrome BUILD.gn is provided)
python3 scripts/analyze_skia.py --skia-dir ../skia

# Individual sections
python3 scripts/analyze_skia.py --skia-dir ../skia --section summary
python3 scripts/analyze_skia.py --skia-dir ../skia --section subsystems
python3 scripts/analyze_skia.py --skia-dir ../skia --section gni

# Full Chrome vs. Android vs. Neither matrix (fetches live Chromium skia/BUILD.gn and AOSP Android.bp)
python3 scripts/analyze_skia.py --skia-dir ../skia --section matrix --fetch-upstream

# Full matrix using local copies of Chromium skia/BUILD.gn and AOSP Android.bp (offline)
python3 scripts/analyze_skia.py --skia-dir ../skia --section matrix \
  --chromium-build-gn /path/to/chromium/skia/BUILD.gn \
  --aosp-android-bp /path/to/aosp/external/skia/Android.bp

# Save report and/or structured JSON summary
python3 scripts/analyze_skia.py --skia-dir ../skia --out report.txt --json summary.json
```
