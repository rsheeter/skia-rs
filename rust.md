# Contemplating a Migration of Skia to Rust

> Grounded in the codebase measurements and Chrome/Android usage analysis in [`first_pass.md`](first_pass.md) (Skia `ca229e3195`, 2026-11-24: **568.3K production SLOC**, of which **445.2K SLOC [78.3%]** is shared by Chrome and Android, **142.7K SLOC [25.1%]** is transitional legacy Ganesh, and **70.7K SLOC [12.4%]** is used by neither).

---

## Executive Verdict

- **Can we do it at all?** **Yes** — if scoped to the post-Ganesh Chrome + Android footprint (**~300–350K SLOC**, not 568K or 1.5M) — *provided* we distinguish between replacing Skia's leaf subsystems in place (already proven!) versus rewriting `src/core/` + Graphite behind Skia's C++ OOP ABI (possible, but blocked on severing Android `libhwui`'s private-header coupling and solving multi-ISA SIMD + C++ virtual hierarchy bridging).
- **Can we do it incrementally?** **Yes through the outer ~25–35% of the codebase** (codecs, fonts, metadata, `skcms`, `PathOps`, `SkSL`, `src/pdf/`, `src/sfnt/`, sparse-strips path tiling), where boundaries are coarse-grained and pure. **No — not cheaply — inside `src/core/` + Graphite**, where per-draw-call C++↔Rust chatter hits an architectural cliff unless consumer boundaries (`libhwui` private includes, `SkCanvas` virtuals) are sealed first.
- **Is it worth doing?** **Unambiguously yes for untrusted input parsers and compilers** (finish Tier 0 codecs/fonts/metadata; seriously target `SkSL`, `SkReadBuffer`/deserialization, `PathOps`, `skcms`, and sparse-strips rasterization). **Not yet for a 1:1 rewrite of `src/core/` + Graphite on security ROI alone**: the security payoff follows a steep 90/20 curve, GPU drivers and Dawn remain a massive `unsafe` floor beneath Rust, and the ~50–150+ engineer-year cost competes directly with retiring Ganesh and shipping Graphite.
- **Comparison to FreeType → Skrifa and HarfBuzz → HarfRust:** Skrifa and HarfRust succeeded because they had **five favorable properties** — pure data transforms, frozen binary specs, deterministic exact oracles, narrow pre-existing FFI seams, and maximal security ROI per SLOC. Skia's *edges* share all five properties; Skia's *core + GPU engine* have **none of them**.

---

## 1. Can We Do It at All?

**Yes, technically — but "migrating Skia to Rust" actually refers to three very different engineering projects with wildly different feasibility:**

| Strategy | Scope (SLOC) | Feasibility | Key Blocker |
|---|---:|---|---|
| **A. Strangler-fig leaves inside C++ Skia** *(already underway)* | ~40–130K | **High (proven)** | Maintaining clean `cxx` seams; avoiding dual-codec binary bloat during transitions |
| **B. Full in-place Rust rewrite behind Skia's existing C++ ABI** | ~300–350K | **Hard, multi-year** | C++ OOP virtual hierarchies, `libhwui` private `src/*` `#include`s, Chrome `SkUserConfig.h` + `raw_ptr`, multi-ISA SIMD |
| **C. Clean-room Rust 2D engine** *(e.g. Vello / sparse-strips + Naga)* replacing Skia at higher consumer boundaries (`cc::PaintOpBuffer`, `Canvas`/`RenderEngine`) | ~100–250K | **Architecturally cleaner, huge compat tail** | 15+ years of Chrome WPT pixel expectations, Android `android.graphics.*` + AGSL app compat, PDF/XPS output |

### Five Hard Technical Problems Any Full Migration Must Solve

1. **C++ OOP polymorphism and intrusive ownership (`include/core/`, 9.4K SLOC; 358 public headers total):**
   Skia's API is deeply C++-idiomatic: virtual subclassing of `SkCanvas` (used directly by Chrome's `BenchmarkingCanvas`, `OpacityFilterCanvas`, and Android's `SkiaRecordingCanvas`), `SkDrawable`, `SkFlattenable` deserialization registries, composable immutable DAGs (`SkShader`, `SkColorFilter`, `SkImageFilter`, `SkPathEffect`, `SkBlender`), and intrusive ref-counting (`SkRefCnt` / `sk_sp<T>` with Chromium mixin headers). Rust has no class inheritance, and `cxx` cannot natively bridge virtual C++ class hierarchies without manual handle/vtable wrappers (Crubit may eventually narrow this gap, but is not yet a turnkey solution for Skia's templates and virtual hierarchies).
2. **Android `libhwui` private-header coupling:**
   As documented in [`first_pass.md`](first_pass.md#31-three-distinct-consumers-in-aosp), `frameworks/base/libs/hwui/Android.bp` adds `include/private`, `src/core`, `src/utils`, `src/image`, `src/gpu`, `src/shaders`, and `src/codec` directly to its `include_dirs`. You literally cannot replace `src/core/` or `src/gpu/` internals in Rust without first refactoring `libhwui` onto a narrow, stable boundary (or generating massive C++ shim headers mimicking Skia's internal layout).
3. **Portable multi-ISA CPU SIMD (`src/opts/`, 8.6K SLOC):**
   `SkRasterPipeline_opts.h` alone is **6,107 SLOC** (7,641 total lines) of clang vector-extension kernels compiled multiple times for scalar, SSE2, AVX2/Haswell (ML3), AVX-512 (ML4), ARM NEON, LoongArch LASX, and WASM SIMD128 with runtime dispatch (`SkOpts`). Stable Rust still lacks `std::simd`, requiring either `std::arch` + `#[target_feature]` dispatch or crates like `fearless_simd` / `pulp` / `wide`. This is solvable (Vello's `vello_cpu` proves Rust SIMD can match or beat Skia on complex paths!), but reproducing every Skia raster stage without throughput or binary-size regressions is serious low-level work.
4. **The `unsafe` GPU & OS interop floor:**
   Unlike font parsing or shaping, a GPU 2D engine must talk to Vulkan, Dawn's C/C++ API, EGL/GLES, `AHardwareBuffer`, sync fences, and memory-mapped GPU staging rings. Rust's ownership model genuinely helps manage CPU-side command/resource lifetimes and prevent UAFs in caches and draw lists, *but* every GPU submission still crosses an `unsafe` FFI boundary into C/C++ graphics drivers.
5. **SkSL and Android's public AGSL contract (`src/sksl/`, 59.1K SLOC):**
   Android exposes **AGSL** (`android.graphics.RuntimeShader` / `RuntimeXfermode`) as a public SDK API — shipped inside third-party APKs in the Play Store. Chrome, HWUI, and SurfaceFlinger RenderEngine also rely on `SkRuntimeEffect` and Graphite's `ShaderCodeDictionary`. A Rust Skia cannot drop SkSL/AGSL; it must either port SkSL's front-end/IR/verifier to Rust (potentially lowering to **Naga IR** to replace Skia's custom SPIR-V/WGSL/MSL/GLSL emitters) or retain C++ SkSL as a compiled-in compiler.

*Note on toolchains:* Build-system feasibility is **no longer a blocker**. Both Chromium (GN + `gnrt` + `cxx`) and AOSP (Soong) already compile and ship Rust code *inside Skia* today (`Fontations`, `rust/png`, plus `rust/bmp`, `rust/icc`, `rust/exif`, `experimental/rust_ico`, `experimental/rust_jpeg` in Chrome, and `CrabbyAvif` in Android).

---

## 2. Can We Do It Incrementally?

**Yes — up to a clear architectural cliff.** Skia decomposes into four migration tiers ordered by seam cleanliness, FFI call frequency, and coupling to `src/core/`:

### Tier 0: Untrusted Binary Input Edge *(Already In Flight — ~31K SLOC in Skia + huge third-party C libs)*
- **What:** Fontations (`src/ports/fontations/`, replacing FreeType), Rust PNG (`rust/png`), Rust BMP (`rust/bmp`), Rust ICO (`experimental/rust_ico`), Rust JPEG (`experimental/rust_jpeg`), Rust ICC (`rust/icc` / `moxcms`), Rust EXIF (`rust/exif`), CrabbyAvif (`SkCrabbyAvifCodec`).
- **Remaining Tier 0 gaps:** WebP (currently C `libwebp`), JPEG XL (currently C++ `libjxl` on Android; `jxl-rs` emerging), RAW/DNG (`dng_sdk`/`piex` on Android), **XMP & XML parsing** (`src/xml/` + `SkXmp.cpp` — currently wraps C `expat`), and **HDR/AGTM metadata parsing** (`SkHdrAgtmParse.cpp`, 1.7K SLOC of new C++ binary parsing!).
- **Why incremental works effortlessly:** Coarse-grained FFI (one call per image/scanline or per glyph), zero shared mutable object graphs, trivial behind-a-flag rollout (`SK_CODEC_DECODES_*_WITH_RUST`, `rusty_jpeg_feature.cc`).

### Tier 1: Self-Contained Algorithmic, Compiler & Document Islands *(~99K SLOC — Clean Seams)*
| Subsystem | SLOC | FFI Seam Shape | Notes |
|---|---:|---|---|
| **`modules/skcms`** | 4.3K | Flat C ABI (`skcms_Transform`, ICC parse) | Chrome already uses `moxcms` for ICC parsing (`rust/icc`); pixel transforms are a small SIMD kernel set. |
| **`src/sfnt/`** | 5.1K | OpenType table parsing | Largely redundant with Fontations' `read-fonts` — can be retired rather than rewritten! |
| **`src/pathops/`** | 20.6K | Pure geometry: `(path, path, op) → path` | Zero GPU/canvas state; Skia already has `modules/bentleyottmann` exploring a replacement. |
| **`src/sksl/`** | 59.1K | Compiler: `(SkSL text, caps) → SPIR-V / WGSL / GLSL / RP bytecode` | Coarse-grained (compile once, cache); Rust `enum`/`match` is vastly cleaner than 107 C++ IR files; opportunity to target **Naga** for backend codegen (~14K SLOC saved). |
| **`src/pdf/` & `src/xps/`** | 11.0K | Document serialization (`SkDevice` → byte stream) | Coarse output boundary; HarfBuzz subsetting could eventually pair with Fontations' `write-fonts` / `skrifa`! |
| **`src/text/` (Slug/RemoteGlyphCache)** | 4.9K | IPC serialization boundary | Deserializes cross-process glyph/strike data in Chrome's GPU process (`SkChromeRemoteGlyphCache`). |

### Tier 2: CPU Rasterization & Shading Kernels *(~20–30K SLOC — Medium-Hard Seam)*
- **Path rasterization (`SkScan_*`, `SkEdge*`, `SkAAClip`, 8.1K SLOC) & Graphite's `sparse_strips` (3.7K SLOC):** Skia is *already* prototyping Vello-style sparse strips inside Graphite (`src/gpu/graphite/sparse_strips/`). A Rust sparse-strips crate (co-developed with `vello_cpu`/`vello_hybrid`) could potentially serve *both* Graphite's path atlas rasterization and Skia's CPU path renderer behind a tile/strip boundary.
- **`SkRasterPipeline` + `SkRasterPipeline_opts.h` (8.0K SLOC):** Self-contained bytecode VM + SIMD stages, called per scanline/rect span (coarse enough for FFI *if* the pipeline bytecode and pixel spans are passed in bulk, though tight tuning is required).

### Tier 3: The Tangled Core & GPU Engine *(~155K SLOC: `src/core/` + Graphite — The Incremental Cliff)*
- **`src/core/` (84.4K SLOC)** and **Graphite (60K SLOC excl. Metal/experiments)** cannot be migrated file-by-file via FFI without severe friction:
  - Every `drawRect` / `drawPath` / `drawTextBlob` passes `SkPaint`, `SkMatrix`, `SkPath`, `SkImage`, `SkShader`, and clip state across `SkCanvas` → `SkDevice` → Graphite `DrawContext` / `PaintParams` → `ShaderCodeDictionary`. Putting FFI *inside* that hot loop destroys inlining, multiplies lifetime complexity, and requires mirroring core data structures on both sides.
  - **How you *would* cross the cliff if you chose to:** Don't migrate `src/core/` class-by-class. First **finish deleting Ganesh** (-142.7K SLOC), **sever `libhwui`'s private `src/*` includes**, and establish a **coarse command-buffer / display-list boundary** (which Chrome *already has* in `cc::PaintOpBuffer`, and Android *partially has* in `RenderNode` / `SkiaDisplayList` and `RenderEngine::drawLayers`). Behind a display-list boundary, you can swap an entire Rust renderer (Graphite-in-Rust or Vello-hybrid) incrementally per platform/surface — exactly the way Chrome and Android are rolling out Ganesh → Graphite today.

---

## 3. Is It Worth Doing?

**It depends sharply on *which tier* you are talking about.** Security ROI in Skia is **not linear with SLOC** — it follows a steep front-loaded curve.

### 3.1 Where the Real Value Lives (and Where It Flattens Out)

| Tier | Approx. SLOC | Security ROI (Rule of 2) | Maintenance / Perf ROI | Verdict |
|---|---:|---|---|---|
| **Tier 0** (Fonts, Codecs, ICC, EXIF, XMP, AGTM) | ~30K (+ huge 3P C libs) | **Extreme.** Untrusted web/app bytes parsed directly; historically the #1 source of Skia/browser RCEs. | **High.** Reuses battle-tested crates (`skrifa`, `png`, `image`, `zune-jpeg`, `moxcms`, `crabbyavif`); shrinks third-party C CVE load. | **Unambiguously worth it** (keep finishing!). |
| **Tier 1** (`SkSL`, `PathOps`, `skcms`, `SkReadBuffer` deserialization, `sfnt` retirement) | ~90K | **High.** `SkSL` is an attacker-reachable shader compiler (AGSL/RuntimeEffect); `SkReadBuffer` deserializes IPC/pictures in Chrome's GPU process; `PathOps` has subtle geometric/memory edge cases. | **Medium-High.** `SkSL` in Rust (+ Naga?) and `sfnt`→`read-fonts` delete real C++ complexity. | **Strongly worth evaluating** — best ROI-per-engineer-year after Tier 0. |
| **Tier 2** (Sparse-strips path rasterizer, `SkRasterPipeline`) | ~16–20K | **Medium-High.** Scan converters / blitters suffer OOB writes and integer overflows on adversarial paths/dimensions (e.g., CVE-2026-3909 / 3931 class). | **High *if* algorithmic upgrade** (Vello sparse strips unlocks multi-threaded CPU + hybrid GPU gains). | **Worth it if paired with sparse-strips** rather than a literal port of legacy `SkScan_*`. |
| **Tier 3** (`src/core/` + Graphite 1:1 rewrite) | ~150K+ | **Moderate & diminishing.** Eliminates CPU-side UAFs in Graphite/caches — *especially* valuable on **Android** (where `skia_use_partition_alloc = false`, unlike Chrome which has MiraclePtr `raw_ptr`!). **However**, GPU drivers & Dawn remain C/C++ `unsafe` underneath. | **Negative in short/medium term.** Literal port gives ~0% speedup while costing 50–150+ engineer-years and stalling Graphite/product work. | **Not worth a 1:1 rewrite today.** Revisit only post-Ganesh deletion, post-HWUI decoupling, or as part of a next-gen engine. |

### 3.2 Two Subtle Strategic Observations
1. **Android has a *stronger* security case for Rust deeper in Skia than Chrome does.** Chrome isolates GPU Skia inside a sandboxed GPU process and wraps internal Skia pointers with PartitionAlloc's **MiraclePtr (`raw_ptr`/`raw_ref`)** to neutralize UAFs. Android sets `skia_use_partition_alloc = false` in `gn/gn_to_bp.py` and links `libskia` directly into `libhwui` (inside every app process and `system_server`) and `SurfaceFlinger` (`libskia_renderengine`). That makes Skia UAFs/OOBs in `SkSL`, `SkPath`/scan conversion, and Graphite caches disproportionately impactful on Android.
2. **Retiring Ganesh is a bigger near-term win than rewriting core Skia in Rust.** Deleting Ganesh eliminates **142.7K SLOC (25.1%)** of aging, GL-coupled C++ state-machine code — *more* unsafe C++ deleted than rewriting all of Tier 0 + Tier 1 combined, while simultaneously shipping real user performance gains (MotionMark, INP, 120Hz Android frames).

---

## 4. What Are the Risks?

1. **The "Three-Engine Tax" (Forever Half-Migrated Trap):**
   Skia is *already* paying a heavy tax maintaining Ganesh (142.7K SLOC) + Graphite (68.6K SLOC) simultaneously across Chrome and Android. Starting a Rust rewrite of core/GPU before Ganesh is retired creates a three-way maintenance matrix (Ganesh C++ + Graphite C++ + Rust), burning out the Skia team and risking an abandoned half-migration that is worse than pure C++.
2. **Chasing a Moving Target:**
   Graphite, precompile, sparse strips, and HDR/AGTM are evolving every week. Rewriting an actively shifting C++ engine in Rust means paying continuous double-implementation tax for every new feature or bugfix unless C++ freezes (which Chrome and Android cannot afford for multi-year spans).
3. **Visual, Pixel, and App Compatibility Regressions:**
   Chrome's Web Platform Tests / Blink pixel baselines and millions of Android apps depend on subtle Skia edge cases (AA coverage rounding, degenerate conics/strokes, image filter crop rects, premultiplied alpha math, AGSL quirks). Unlike fonts (exact glyph curves), small floating-point or rasterization differences can silently break thousands of tests or subtly glitch production UIs.
4. **Performance & Binary-Size Regressions:**
   - Matching `SkRasterPipeline_opts.h` across AVX2/AVX-512/NEON/LASX/WASM SIMD128 in stable Rust is non-trivial.
   - Chatty FFI across `SkCanvas` or `SkPath` kills inlining unless Cross-Language LTO works seamlessly across both Chromium GN (PGO/AFDO) and AOSP Soong (ThinLTO).
   - During rollout, shipping *both* C++ and Rust paths inflates APK/binary size and RAM (a hard constraint for Android Chrome and low-RAM Android devices), compounded by Rust monomorphization if generics aren't carefully disciplined.
5. **Consumer Coupling & Hidden ABI Breakage:**
   `libhwui`'s direct `#include`s into Skia's `src/*` directories, Android Studio's host LayoutLib builds (Linux/Mac/Win), CTS `SkQP`, and Chromium's `SkUserConfig.h` staging flags mean internal C++ refactors frequently break downstream builds. Wrapping those internals across an FFI boundary magnifies churn.
6. **Boundary Safety Erosion (`unsafe` Leakage):**
   A poorly placed FFI boundary (passing raw `SkPixmap` pointers, C++ `SkStream*` callbacks, or unpinned GPU buffer handles back and forth) can *introduce* subtle aliasing/lifetime UB right at the seam — eroding the safety benefit you migrated for.
7. **Ecosystem Fragmentation:**
   Beyond Chrome and Android, Skia powers Flutter (CanvasKit/fallback), Jetpack Compose Multiplatform (Skiko), PDFium, and C++ desktop embedders. Breaking C++ embedders or forcing complex Rust/Cargo/Bazel/GN/Soong/Wasm integration onto downstream users carries real adoption and maintenance costs.

---

## 5. Compare & Contrast: FreeType → Skrifa & HarfBuzz → HarfRust vs. Skia → Rust

The Google Fonts *Oxidize* migrations (**FreeType → Skrifa/Fontations** and **HarfBuzz → HarfRust**) are the gold-standard reference points for migrating foundational C/C++ graphics/text libraries to Rust in Chrome and Android. Comparing them directly against Skia clarifies *exactly* where the playbook transfers and where it breaks down.

### 5.1 Side-by-Side Comparison

| Dimension | FreeType → Skrifa (`Fontations`) | HarfBuzz → `HarfRust` | Skia Tier 0–1 *(Codecs, SkSL, PathOps, skcms)* | Skia Tier 3 *(Core + Graphite GPU)* |
|---|---|---|---|---|
| **Scale (target SLOC)** | **~28K SLOC** (`skrifa`) + `read-fonts`/`font-types` (~45K total) | **~77K lines** (`harfrust`, sharing `read-fonts`) | **4K–59K SLOC** per independent island | **~300–350K SLOC** (post-Ganesh Chrome+Android) — **5×–10× larger** |
| **Computational shape** | **Pure function**: `(font bytes, glyph_id, size, coords) → outline / metrics / bitmap`. Zero I/O, zero hardware, zero global state. | **Pure function**: `(font bytes, UTF-8 run, script/features) → positioned glyphs`. Zero I/O, zero OS deps. | **Pure / coarse transforms**: bytes → pixels, SkSL text → shader IR/bytecode, paths → path. | **Stateful, imperative, hardware-coupled engine**: mutable canvas stack, async GPU command buffers, texture/pipeline/glyph caches, fences, OS surface interop. |
| **Spec & target stability** | **Stable binary specs** (OpenType, TrueType, CFF/CFF2, COLR, hinting VM). | **Stable specs** (OpenType GSUB/GPOS, USE, script shapers; HarfBuzz is the reference). | **Mostly stable specs** (PNG/JPEG/ICC/EXIF/PDF/AGSL/SPIR-V/WGSL). | **Rapidly moving internal target**: Graphite, precompile, sparse strips, and HDR/AGTM evolve weekly; no external spec defines Skia rendering. |
| **Verification oracle** | **Exact, deterministic differential oracle**: glyph-by-glyph outline, metric, and pixel diffs against FreeType across huge font corpora! | **Exact, deterministic differential oracle**: exact `(glyph_id, advance, offset, cluster)` tuple equality against C++ HarfBuzz! | **Strong deterministic oracles**: exact codec pixels, `skcms` ULP bounds, `PathOps` geometry, 2,385 existing `tests/sksl/` golden shaders. | **Fuzzy, driver-dependent oracle**: GPU pixels vary across Adreno/Mali/Apple/Intel/NVIDIA drivers; relies on visual GMs, SkQP, WPT, and app compat. |
| **FFI & API surface** | **Narrow pre-existing seam**: `SkTypeface` / `SkScalerContext` already abstracted FreeType (~4.2K SLOC total bridge in `src/ports/fontations/`). | **Narrow flat C ABI**: `hb_shape()` + font/buffer setup (`harfrust_capi`). | **Narrow coarse-grained seams**: `SkCodec`, `SkSL::Compiler`, `Op(path, path)`, `skcms_Transform`. | **Huge porous C++ OOP surface**: 358 public headers (22.9K SLOC), virtual inheritance, intrusive refcounts, + Android `libhwui` `#include`ing 7 private `src/*` dirs. |
| **Rust ergonomic & `unsafe` fit** | **Ideal**: `read-fonts` parses binary tables zero-copy via `&[u8]` slices with **~0 `unsafe`**. | **Ideal**: Safe slice lookups (reusing `read-fonts`) + script state machines with **~0 `unsafe`**. | **Very good**: `SkSL` AST/IR loves `enum`/`match`; codecs & `read-fonts` love safe slices; `PathOps` is pure value math. | **Friction-heavy**: Multi-ISA SIMD (`SkRasterPipeline_opts.h`), shared/cached DAGs, and raw Vulkan/Dawn/EGL/AHB driver calls impose an unavoidable `unsafe` floor. |
| **Incremental rollout** | **Surgical**: Chrome rolled out Fontations per font format (COLR/CFF2 in M128 → system fonts in M133+) with instant FreeType fallback. | **Surgical**: Feature-flag swap in Blink/Minikin with shadow differential testing. | **Surgical**: Per-format flags (`SK_CODEC_*_WITH_RUST`, `rusty_jpeg_feature.cc`) or per-compiler-pass verification. | **Coarse & expensive**: Requires dual-pipeline shipping (like Ganesh + Graphite) until every platform/driver/app passes. |
| **Security ROI per SLOC** | **Maximal**: Eliminates decades of RCE-class binary parser flaws in untrusted fonts. | **Very high**: Eliminates C++ OOB/UAF bugs on untrusted web fonts and shaping tables. | **High**: Eliminates untrusted binary/XML/shader parser bugs under Rule of 2. | **Diminishing**: Lower bug density in draw dispatch; Chrome already applies `raw_ptr` + GPU sandbox; GPU drivers remain C/C++. |
| **Broader Rust ecosystem leverage** | **Unified the ecosystem**: `font-types` / `read-fonts` / `skrifa` shared across Chrome, Android, HarfRust, Parley, Typst, Servo. | **Unified the ecosystem**: `harfrust` consolidates shaping on `read-fonts`, replacing `rustybuzz` + `ttf-parser`. | **Immediate crates.io leverage**: `png`, `image`, `zune-jpeg`, `moxcms`, `crabbyavif`, `jxl-rs`, `naga` already exist! | **Low for a Skia-ABI clone**; high *only* if converging with clean-room Rust 2D work (e.g., Linebender's **Vello** sparse strips). |

### 5.2 What Skrifa and HarfRust Actually Teach Us About Skia

1. **Skrifa and HarfRust didn't rewrite FreeType's or HarfBuzz's *entire* scope — they ruthlessly shed legacy and OS coupling.**
   HarfRust intentionally dropped C-HarfBuzz's FreeType/CoreText/ICU/GLib integrations and scaling, working cleanly in `UnitsPerEm` over `read-fonts`. Skrifa focused on OpenType/TrueType/CFF/COLR outlines & metrics rather than every obscure 1990s FreeType bitmap format. **For Skia, the exact equivalent discipline is:** never port Ganesh (-143K SLOC), never port unused modules/backends (-71K SLOC), and don't replicate legacy C++ OOP inheritance where a clean functional boundary exists.
2. **Sharing a foundational safe parser (`read-fonts`) multiplied value across *both* Skrifa and HarfRust.**
   Notice the same pattern inside Skia: `rust/common` already unifies `cxx` stream traits across `rust/png`, `rust/bmp`, `rust/icc`, `rust/exif`, `experimental/rust_ico`, and `experimental/rust_jpeg`; `read-fonts` can retire `src/sfnt/`; and adopting **Naga IR** (or a clean Rust SkSL IR) could unify shader validation/codegen across Graphite's SPIR-V and WGSL targets.
3. **Differential testing was the secret weapon — so pick Skia seams that *have* an exact oracle first.**
   Skrifa could prove parity glyph-by-glyph against FreeType; HarfRust could prove parity tuple-by-tuple against HarfBuzz. Inside Skia, **codecs, `skcms`, `PathOps`, `SkSL` (2,385 golden shader tests!), and CPU `sparse_strips` rasterization** all admit tight, automated differential testing against C++ Skia. Full GPU Graphite does not — making it the *last* piece to consider, not the first.
4. **The architecture-first rule:**
   Fontations only dropped cleanly into Skia because `SkTypeface` / `SkScalerContext` already existed as a clean abstraction seam. Before anyone seriously contemplates rewriting Skia's `src/core/` or Graphite in Rust, the prerequisite isn't writing Rust — it's **finishing the Ganesh retirement** and **creating an `SkScalerContext`-grade seam in Android's `libhwui`**.

---

## 6. Recommended Roadmap (If Pursuing Rust in Skia)

1. **Complete Tier 0 (Now — highest ROI, proven pattern):** Finish rolling out Fontations (retire FreeType in Chrome & Android), Rust PNG/BMP/ICO/JPEG, `rust/icc` (`moxcms`), and `rust/exif`; add Rust replacements for the remaining untrusted-input parsers (`SkXmp.cpp` / `src/xml/` off Expat, `SkHdrAgtmParse.cpp`, WebP/JXL as crates mature); retire `src/sfnt/` via `read-fonts`.
2. **Target High-ROI Tier 1 Islands (Next 1–2 years):**
   - **SkSL → Rust** (starting with front-end/parser/IR/verifier for AGSL & `SkRuntimeEffect`, evaluating Naga for SPIR-V/WGSL codegen) — huge security & maintainability win on 59K SLOC.
   - **Hardened Rust `SkPicture` / `Slug` / IPC deserialization** (`SkReadBuffer`) — closes cross-process deserialization risk in Chrome's GPU process.
   - **`PathOps` (20.6K SLOC)** and **`skcms` transforms (4.3K SLOC)** — clean pure-math islands with exact oracles.
3. **Co-evolve Sparse Strips (Tier 2 — architectural leverage):** Watch/invest in Graphite's `sparse_strips` experiment (`src/gpu/graphite/sparse_strips/`). Because the algorithm originates in Rust (`vello_cpu` / `vello_hybrid`), landing the production implementation in Rust instead of C++ gives both memory safety *and* a real algorithmic/performance leap.
4. **Defer Tier 3 (`src/core/` + Graphite) Until Prerequisites Are Met:** Do not attempt a full core/GPU rewrite while Ganesh is still alive (-143K SLOC to delete first) or while Android `libhwui` reaches directly into Skia's `src/*` headers. First delete Ganesh and seal HWUI's boundary — then reassess whether Rust-in-place or a clean-room sparse-strips/Graphite successor makes sense.
