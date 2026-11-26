# skia-rs

Technical notes and reproducible codebase analysis exploring what a migration of [Skia](https://skia.org/) to Rust would look like — grounded in Skia's actual source tree, Chromium and Android build configurations, and lessons from the FreeType → Skrifa and HarfBuzz → HarfRust migrations.

## Documents

- **[`first_pass.md`](first_pass.md)** — **Codebase & Usage Breakdown**:
  - Measures Skia (`ca229e3195`, 2026-11-24) at **568.3K production SLOC** (799.5K total lines), cleanly separated from **355.7K SLOC** of vendored Vulkan headers (`include/third_party/vulkan/`) and **607.4K SLOC** of tests, golden SkSL shaders, GMs, benchmarks, tools, and experiments.
  - Breaks down production code by subsystem: GPU backends (**39.0%** — legacy Ganesh 142.7K vs. next-gen Graphite 68.6K), Core/Raster/Effects/Opts (**21.4%**), optional Modules (**10.7%**), SkSL compiler (**10.4%**), Ports/Text/SFNT (**5.9%**), Codecs/Encoders/Rust FFI (**4.4%**), PathOps (**3.6%**), Documents/XML (**2.3%**), and Utils/Android glue (**2.2%**).
  - Cross-references live Chromium (`skia/BUILD.gn`, `skia/features.gni`, `SkUserConfig.h`) and Android (`gn/gn_to_bp.py`, AOSP `external/skia`, `libs/hwui`, `libs/renderengine` `Android.bp`) builds: **78.3%** shared by both, **7.0%** Chrome-only, **2.2%** Android-only, and **12.4% (70.7K SLOC)** used by neither.
- **[`rust.md`](rust.md)** — **Rust Migration Assessment**:
  - Evaluates feasibility, incremental migration tiers (Tier 0 untrusted parsers → Tier 1 `SkSL`/`PathOps`/`skcms`/`sfnt`/PDF → Tier 2 sparse-strips/raster pipeline → Tier 3 `src/core/` + Graphite), security & engineering ROI, and major risks (dual/triple-engine maintenance tax, Android `libhwui`'s private `src/*` includes, multi-ISA SIMD, binary size, and GPU driver `unsafe` floor).
  - Contrasts migrating Skia against **FreeType → Skrifa (Fontations)** and **HarfBuzz → HarfRust**, explaining why Skia's untrusted-input edges and self-contained compiler/math islands match the Skrifa/HarfRust playbook closely while `src/core/` + Graphite do not.

## Analysis Scripts

- **[`scripts/analyze_skia.py`](scripts/analyze_skia.py)** (documented in [`scripts/README.md`](scripts/README.md)) — zero-dependency Python 3 script reproducing all LoC/SLOC counts, subsystem breakdowns, `.gni` inspections, and the Chrome/Android/Neither usage matrix against a local Skia checkout (default `../skia`).
