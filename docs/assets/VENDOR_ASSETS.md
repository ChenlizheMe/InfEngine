# Website vendor assets

These files are committed so the GitHub Pages experience does not depend on third-party font or icon CDNs at runtime.

| Local file | Upstream | Version/source | SHA-256 |
|---|---|---|---|
| `fonts/inter-latin.woff2` | Google Fonts / Inter | `fonts.gstatic.com/s/inter/v20` | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |
| `fonts/jetbrains-mono-latin.woff2` | Google Fonts / JetBrains Mono | `fonts.gstatic.com/s/jetbrainsmono/v24` | `83c005d49d8a6a50474c73a5a36ac0468076e9c4a29da7bdb14995d80560a5be` |
| `fonts/space-grotesk-latin.woff2` | Google Fonts / Space Grotesk | `fonts.gstatic.com/s/spacegrotesk/v22` | `0640890476fc1198ab4de571fb658de443c4d85b66466ec09534a8737ab1ce9d` |
| `fonts/fa-solid-subset-900.woff2` | Font Awesome Free | `6.4.0`; 31-glyph site subset | `1ab0dea7613a56456bd30de51fee7d0fccb6def013fe1f46862e2eb204fba343` |
| `fonts/fa-brands-subset-400.woff2` | Font Awesome Free | `6.4.0`; GitHub + Python subset | `7d7c0b8449df96bbfdc8b4e6c6740ce2337af2c90363a5213713977df3e7ae76` |

Font license texts are preserved in `vendor-licenses/`. When any asset changes, update its version/source, checksum, license, and the static-site verifier in the same change.

## Project-authored visual assets

`infernux-social-card-0.3.4.jpg` is the reviewed 1200×630 Open Graph/X card for release 0.3.4. It is a center-cropped derivative of the real `demo.png` editor capture rather than separate promotional artwork. Its SHA-256 is `c1bb18887d484776433a14f43bf2d77dde6a9fe4f8eabc077e6fbb541c273159`. The site verifier locks its format, dimensions, release-scoped filename, and reviewed content hash.

The repository keeps `demo.png` as the canonical 1920×1032 review source used by both README files. The GitHub Pages homepage does not reference or deliver that PNG: it offers the release-scoped AVIF first and uses a high-quality WebP as the `<img>` fallback and structured-data screenshot. This explicitly targets current Chrome, Edge, Firefox, and Safari while keeping the larger PNG outside the website delivery budget.

| Local file | Encoding and review evidence | Bytes | SHA-256 |
|---|---|---:|---|
| `demo.png` | Original 1920×1032 repository-owned editor capture | 1,121,375 | `4be6e30abfd71f3e4a31593ce6e44817cffdb83ec170cee489a3b40b162d3d91` |
| `demo-0.3.4.webp` | Pillow 12.2.0 WebP, quality 88; high-quality browser fallback | 151,802 | `10a141e9c795829ded555363d0866c1508403e19fb4fdc14401e1532f313384c` |
| `demo-0.3.4.avif` | Pillow 12.2.0 AVIF, quality 80, 4:4:4; visually reviewed against the PNG | 136,520 | `4cbe016a9eedfefebb8d7a2bbd107e829ca706f045de2aa4d5c381c456efa9f5` |

AVIF is preferred and high-quality WebP is the final website fallback. The image gate locks all three reviewed files by content hash; it also verifies that both README files retain the PNG while the homepage contains no PNG reference. The performance budget counts the largest browser-delivered representation and separately excludes the repository-only review source, so neither hidden fallback weight nor unused evidence files distort the site budget.

### Install and touch icons

The install icons are deterministic, project-authored derivatives of the repository-owned `logo.png`; they do not introduce an external artwork source or license. Pillow 12.2.0 in the repository `infernux` environment resized the source with Lanczos sampling, composited it over the site background `#0a0c11`, and wrote optimized 256-color opaque PNGs. The standalone maskable asset keeps the complete emblem inside the Web App Manifest safe-zone circle (radius 40% of the canvas); it is intentionally more padded than the ordinary launcher icons.

| Local file | Role and geometry | Bytes | SHA-256 |
|---|---|---:|---|
| `infernux-icon-192.png` | Chromium install icon; 192×192, opaque | 10,064 | `edfa0d3e709db4ac3100978575147579d4ccdb63c695c3d551e78bc7891c0f4a` |
| `infernux-icon-512.png` | Chromium install/splash icon; 512×512, opaque | 49,570 | `9f73c451f95f09decaf95702971099c1a6237a8e454c293f201dddfc7473e280` |
| `infernux-icon-maskable-512.png` | Adaptive launcher icon; 512×512, opaque, safe-zone padded | 25,603 | `54c43fee25612ce3d2d0fa4f14cff5191149201faa2d97d0b834860bfd3fbcc1` |
| `infernux-apple-touch-icon.png` | Apple home-screen icon; 180×180, opaque | 9,163 | `a4e54a3d319ab3badace561328c94233c07fc3181b116ca137033a68a31de7f5` |

`docs/tools/check-pwa-assets.mjs` locks each file's PNG signature, dimensions, opacity, reviewed hash, manifest role, HTML link, provenance, and Service Worker precache entry. Regenerate and review the complete set together if the emblem or background color changes.

## Font Awesome subsetting

The two Font Awesome files are generated from the official 6.4.0 `webfonts` files with the `pyftsubset` executable provided by the repository's `infernux` environment. The solid subset contains the code points declared by the first `unicode-range` in `css/fontawesome-subset.css`; the brand subset contains `U+F09B` (GitHub) and `U+F3E2` (Python). When a new icon class is introduced, regenerate the matching WOFF2, update its `unicode-range` and checksum, then run the website verifier. The verifier rejects missing CSS mappings, unexpected font hashes, and Font Awesome files large enough to indicate that a complete upstream font was restored.
