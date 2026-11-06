# Local NSCodec correction

Base: published `ironrdp-nscodec` 0.2.0 from crates.io, authored by Devolutions / Teleport; original MIT and Apache-2.0 license files retained. The workspace `[patch.crates-io]` selects this copy; do not edit the global Cargo cache.

The original encoder always emitted its RLE result. Pair runs can expand a plane, or produce equal length with changed bytes. NSCodec decoders select compressed versus raw using the encoded plane byte count, so only a strictly shorter RLE result is valid as compressed data. Keep the original plane when RLE is equal or larger, matching the [FreeRDP reference encoder](https://pub.freerdp.com/api/nsc__encode_8c_source.html) and [reference decoder](https://pub.freerdp.com/api/nsc_8c_source.html).

Changes: raw fallback in `rle_encode`; correction of an existing test that expected an expanded invalid plane; regressions for pair-run expansion, mixed/noisy/constant planes using an independent size-aware reader, and a multi-row grayscale frame including opaque alpha and bottom-up order. Opaque desktop alpha uses the protocol's zero-length alpha plane (constant 255), avoiding its allocation and transmission. The reference reader covers the final-five-byte literal rule. No changes to color loss, input routing, capture, authentication, or audio.

A pair-run fixture failed against the original encoder before applying the fix. These encoder regressions do not prove Windows App resize acceptance. The production compatibility policy leaves NSCodec behind `ZMACRDS_EXPERIMENTAL_NSCODEC=1`; default fallback uses raw SurfaceBits when Windows App does not select RemoteFX. Observed limitations and build identity are recorded in `docs/reports/BACKGROUND_MANAGER_2026-11-05.md`.
