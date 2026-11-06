# Local integration patches

Base: [`ironrdp-server` 0.13.0 on crates.io](https://crates.io/crates/ironrdp-server/0.13.0), from [Devolutions/IronRDP](https://github.com/Devolutions/IronRDP). The downloaded `.crate` SHA-256 is:

```text
1d3922b23ad9f262b932d42fa37df87f7e45c2d12f45433c0cc084f8762fb09a
```

The published `Cargo.toml`, `src/`, README and both upstream license files are retained. The workspace uses a `[patch.crates-io]` override; it does not depend on an unpublished upstream branch. Upstream remains licensed under MIT OR Apache-2.0.

## Change

Windows App can send an absolute mouse button PDU without an earlier MOVE PDU. Converting that packet into one `MouseEvent` dropped its coordinates, so the GUI bridge clicked its previous position (initially 0,0). Fast-path and slow-path absolute mouse handlers now dispatch the packet's position before its button transitions. Extended mouse buttons map to buttons 4/5. Wheel packets do not use their coordinate fields.

The original coordinate change affects `src/handler.rs` and `src/server.rs`. Additional display integration changes below also affect `src/encoder/mod.rs`. Relative mouse and the optional `ainput` channel are unchanged; this patch does not assert coverage of those paths.

The coordinate and wheel behavior follows Microsoft's [MS-RDPBCGR pointer event specification](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/2c1ced34-340a-46cd-be6e-fc8cab7c3b17). Three regression tests cover a click without MOVE, wheel coordinates, and extended buttons:

```sh
cargo test -p ironrdp-server --no-default-features --features helper coordinate_tests --locked
```

VM03 validation with Windows App 11.4.4 (3132) confirmed cross-window clicks and drag selection after this patch. See the repository's M2 report. Remove the override once an upstream release with equivalent behavior has passed the same tests.

## Display diagnostics and incomplete repaint work

Follow-up implementation and scoped VM03 results are recorded in [2026-11-05 regression report](../../docs/reports/REFACTOR_VM03_2026-11-05.md). `BitmapRefresh` explicitly invalidates the differencing cache before a complete snapshot; negotiated FrameMarker Begin/End commands bracket all strips from that snapshot, using one frame ID. Unchanged images emit no empty marker pair; legacy clients receive no unadvertised markers. See [Microsoft Frame Marker command](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/7a4d7c0c-06e3-4802-aaa8-92c8f1d86f6e). Frame markers alone did not fix the observed resize failure; fixed remote geometry with client-side scaling passed the measured regression. Dynamic Display Control remains unsupported.

The encoder now reports the selected codec once per connection. Setting `ZMACRDS_PERF_LOG` additionally reports display-update delivery counts and elapsed milliseconds; these are not client presentation FPS. No pixels, passwords or TLS secrets are logged.

`RdpServerOptions.desktop_resize_supported` defaults to true to preserve the upstream behavior. The builder can disable it; both ZMacRDS server entry points set it to false because their source has fixed geometry. A VM03 counter-test still reproduced black regions when Windows App forced dynamic resolution despite this capability change. Clients must use fixed remote geometry plus local scaling until Display Control and its full surface lifecycle are implemented and verified.

RefreshRectangle and SuppressOutput resume requests invalidate the encoder's difference cache before the next sample. A regression test proves unchanged pixels are then selected for repaint. This is **not a complete resize fix**: the user still reproduced black blocks after this change. See the [architecture review](../../docs/reports/ARCHITECTURE_REVIEW_2026-11-04.md). The remaining surface/geometry/cache lifecycle needs joint validation before this patch can be considered complete.

## Revocable audio events

`ServerEvent::RdpsndGuarded` carries a connection-local generation, deadline and semaphore permit. Immediately before RDPSND encoding, the server drops events whose generation was invalidated by pause, disconnect or takeover, or whose PCM age exceeded 200 ms. The permit bounds pending waves to four for this source. Ordinary upstream `Rdpsnd` events keep their existing behavior. Protocol trace logs report an audio event without formatting its PCM payload. A regression test covers expiration and generation revocation; the application separately tests UID-local queues, capture lease and native generation. See [audio/service results](../../docs/reports/AUDIO_SERVICE_2026-11-05.md).

## Immediate client refresh

`RefreshRectangle` and resumed `SuppressOutput` now wake the display loop via a bounded/coalescing `Notify`, rather than waiting for a future capture. The optional `RdpServerDisplayUpdates::request_refresh` hook lets the fixed-UID bridge immediately republish its latest valid complete frame. Paused, revoked, or closed captures cannot replay the old baseline. Other backends retain a no-op default. Tests cover immediate refresh without advancing time, paused and closed sources, and recovery to a new valid frame.

## Receiver reassembly bounds

Outbound fragmentation now uses the smaller of the server limit and the client's negotiated MultiFragmentUpdate limit. Windows App advertises 1 MiB in this environment; the server's own 8 MiB advertisement is not permission to send larger reassembled updates. Missing client capability falls back to 16,374 bytes; impossibly small limits are rejected. Encoding reserves 64 bytes for framing, and NSCodec jobs are additionally capped at 256 KiB. Tests cover capability selection and every row of a multi-strip encoded image. This is a protocol-boundary fix, not proof that every observed resize defect was caused by fragmentation.
