//! Bounded, loopback-only RDP integration client for the synthetic VM editors.
//! Validates a locally supplied certificate (including name/expiry), then pins
//! its exact DER bytes. No system trust changes, no screenshots, no key logging.
mod audio;
use anyhow::{Context as _, Result, bail, ensure};
use ironrdp_connector::{ClientConnector, Credentials, DesktopSize};
use ironrdp_core::decode;
use ironrdp_graphics::image_processing::PixelFormat;
use ironrdp_pdu::{
    gcc::KeyboardType,
    input::{
        MousePdu,
        fast_path::{FastPathInputEvent as Event, KeyboardFlags},
        mouse::PointerFlags,
    },
    mcs::SendDataIndication,
    rdp::{
        capability_sets::{BitmapCodecs, MajorPlatformType},
        server_error_info::{ErrorInfo, ProtocolIndependentCode, ServerSetErrorInfoPdu},
    },
    x224::X224,
};
use ironrdp_session::{ActiveStageBuilder, ActiveStageOutput, image::DecodedImage};
use ironrdp_tokio::{FramedWrite, TokioFramed, connect_begin, connect_finalize, mark_as_upgraded};
use serde::Deserialize;
use serde_json::json;
use std::{
    fs,
    io::{Cursor, Read},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::PathBuf,
    sync::{Arc,Mutex},
    time::{Duration, Instant},
};
use tokio::net::TcpStream;
use tokio_rustls::{TlsConnector, rustls};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    #[serde(default = "default_port")]
    port: u16,
    #[serde(default)]
    motion_probe: Option<[u16; 2]>,
    #[serde(default)]
    audio_probe: Option<[u16;2]>,
    #[serde(default)]
    service_probe:bool,
    #[serde(default)]
    frame_summary:bool,
    #[serde(default)]
    measure_text_latency: bool,
    certificate: PathBuf,
    username: String,
    password: String,
    uid: u32,
    nonce: u32,
    seconds: u64,
    #[serde(default)]
    expect_denied: bool,
    #[serde(default)]
    actions: Vec<Action>,
}
fn default_port() -> u16 {
    3390
}
impl Drop for Config {
    fn drop(&mut self) {
        use zeroize::Zeroize;
        self.password.zeroize();
    }
}
#[derive(Deserialize)]
struct Action {
    at_ms: u64,
    #[serde(flatten)]
    kind: Kind,
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Kind {
    Text {
        text: String,
    },
    Click {
        x: u16,
        y: u16,
    },
    Drag {
        x: u16,
        y: u16,
        to_x: u16,
        to_y: u16,
    },
    Key {
        code: u8,
        #[serde(default)]
        extended: bool,
        down: bool,
    },
}
struct NoNetwork;
impl ironrdp_tokio::NetworkClient for NoNetwork {
    async fn send(
        &mut self,
        _: &ironrdp_connector::sspi::generator::NetworkRequest,
    ) -> ironrdp_connector::ConnectorResult<Vec<u8>> {
        Err(ironrdp_connector::general_err!(
            "CredSSP/external network is disabled in this test client"
        ))
    }
}

fn read_config(path: &str) -> Result<Config> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let meta = file.metadata()?;
    ensure!(
        meta.is_file()
            && meta.uid() == unsafe { libc::geteuid() }
            && meta.permissions().mode() & 0o077 == 0
            && meta.len() <= 65536,
        "config must be owned, private, regular and bounded"
    );
    let mut bytes = zeroize::Zeroizing::new(Vec::new());
    file.take(65537).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 65536, "config grew too large");
    let config: Config = serde_json::from_slice(&bytes)?;
    ensure!(
        config.port >= 1024 && (1..=7200).contains(&config.seconds) && config.actions.len() <= 1000,
        "invalid test bounds"
    );
    ensure!(config.audio_probe.is_none_or(|f| f[0]!=f[1] && f.into_iter().all(|h|(200..=2000).contains(&h))) && (config.audio_probe.is_none() || config.actions.is_empty()),"audio probe uses two distinct synthetic tones and no input");
    ensure!(!config.service_probe || (config.actions.is_empty() && config.motion_probe.is_none() && config.audio_probe.is_none()),"passive service probe cannot send input or measure synthetic content");
    ensure!(!config.frame_summary || config.service_probe,"frame summary requires a read-only service probe");
    ensure!(
        !config.expect_denied || config.actions.is_empty(),
        "negative tests never send input"
    );
    ensure!(
        config.actions.windows(2).all(|s| s[0].at_ms <= s[1].at_ms),
        "actions must be ordered"
    );
    ensure!(!config.measure_text_latency || (config.motion_probe.is_some()
        && !config.actions.is_empty() && !config.expect_denied
        && config.actions.iter().all(|a| matches!(&a.kind, Kind::Text { text } if !text.is_empty()))),
        "text latency requires a visible synthetic editor marker and text-only actions");
    Ok(config)
}
fn connector_config(config: &Config) -> ironrdp_connector::Config {
    ironrdp_connector::Config {
        desktop_size: DesktopSize {
            width: 1920,
            height: 1200,
        },
        desktop_scale_factor: 100,
        enable_tls: true,
        enable_credssp: false,
        credentials: Credentials::UsernamePassword {
            username: config.username.clone(),
            password: config.password.clone(),
        },
        domain: None,
        client_build: 1,
        client_name: "ZMacRDSCheck".into(),
        keyboard_type: KeyboardType::IbmEnhanced,
        keyboard_subtype: 0,
        keyboard_functional_keys_count: 12,
        keyboard_layout: 0x409,
        ime_file_name: String::new(),
        bitmap: Some(ironrdp_connector::BitmapConfig {
            lossy_compression: false,
            color_depth: 32,
            codecs: BitmapCodecs(Vec::new()),
        }),
        dig_product_id: String::new(),
        client_dir: String::new(),
        alternate_shell: String::new(),
        work_dir: String::new(),
        platform: MajorPlatformType::MACINTOSH,
        hardware_id: None,
        request_data: None,
        autologon: true,
        enable_audio_playback: config.audio_probe.is_some(),
        performance_flags: Default::default(),
        license_cache: None,
        timezone_info: Default::default(),
        compression_type: None,
        enable_server_pointer: false,
        pointer_software_rendering: false,
        multitransport_flags: None,
    }
}

fn crc(bytes: &[u8]) -> u16 {
    let mut value = 0xffffu16;
    for b in bytes {
        value ^= u16::from(*b) << 8;
        for _ in 0..8 {
            value = (value << 1) ^ if value & 0x8000 != 0 { 0x1021 } else { 0 };
        }
    }
    value
}
fn marker(image: &DecodedImage) -> Option<(u32, u32, u32)> {
    marker_bgra(
        image.data(),
        usize::from(image.width()),
        usize::from(image.height()),
    )
}
fn motion_marker(image: &DecodedImage, [x, y]: [u16; 2]) -> Option<(u32, u32, u32)> {
    let (w, h) = (usize::from(image.width()), usize::from(image.height()));
    let (x, y) = (usize::from(x), usize::from(y));
    if x + 524 > w || y + 4 > h {
        return None;
    }
    let mut strip = [0; 524 * 4 * 4];
    let start = ((y + 2) * w + x) * 4;
    strip[524 * 8..524 * 12].copy_from_slice(image.data().get(start..start + 524 * 4)?);
    marker_bgra(&strip, 524, 4)
}
fn marker_bgra(raw: &[u8], w: usize, h: usize) -> Option<(u32, u32, u32)> {
    if w < 524 || w.checked_mul(h)?.checked_mul(4)? != raw.len() {
        return None;
    }
    for y in (2..h).step_by(4) {
        for x in 0..=w - 524 {
            let at = (y * w + x) * 4;
            let magenta = |i| raw[i] > 180 && raw[i + 1] < 80 && raw[i + 2] > 180;
            if !magenta(at)
                || !(raw[at + 16] > 180 && raw[at + 17] > 180 && raw[at + 18] < 80)
                || !magenta(at + 32)
            {
                continue;
            }
            let mut bytes = [0u8; 16];
            let mut valid = true;
            for bit in 0..128 {
                let i = at + (bit + 3) * 16;
                let white = raw[i] > 180 && raw[i + 1] > 180 && raw[i + 2] > 180;
                let black = raw[i] < 80 && raw[i + 1] < 80 && raw[i + 2] < 80;
                if !white && !black {
                    valid = false;
                    break;
                }
                bytes[bit / 8] |= u8::from(white) << (7 - bit % 8);
            }
            if valid
                && bytes[..2] == [0xa5, 0x3c]
                && crc(&bytes[..14]) == u16::from_be_bytes([bytes[14], bytes[15]])
            {
                return Some((
                    u32::from_be_bytes(bytes[2..6].try_into().ok()?),
                    u32::from_be_bytes(bytes[6..10].try_into().ok()?),
                    u32::from_be_bytes(bytes[10..14].try_into().ok()?),
                ));
            }
        }
    }
    None
}
fn mouse(x: u16, y: u16, flags: PointerFlags) -> Event {
    Event::MouseEvent(MousePdu {
        flags,
        x_position: x,
        y_position: y,
        number_of_wheel_rotation_units: 0,
    })
}
fn action_events(action: &Action, size: (u16, u16)) -> Result<Vec<Event>> {
    let position = |x, y| {
        ensure!(x < size.0 && y < size.1, "input outside test desktop");
        Ok::<(), anyhow::Error>(())
    };
    Ok(match &action.kind {
        Kind::Text { text } => {
            ensure!(text.encode_utf16().count() <= 64, "text action too long");
            text.encode_utf16()
                .flat_map(|u| {
                    [
                        Event::UnicodeKeyboardEvent(KeyboardFlags::empty(), u),
                        Event::UnicodeKeyboardEvent(KeyboardFlags::RELEASE, u),
                    ]
                })
                .collect()
        }
        Kind::Key {
            code,
            extended,
            down,
        } => vec![Event::KeyboardEvent(
            (if *extended {
                KeyboardFlags::EXTENDED
            } else {
                KeyboardFlags::empty()
            }) | (if *down {
                KeyboardFlags::empty()
            } else {
                KeyboardFlags::RELEASE
            }),
            *code,
        )],
        Kind::Click { x, y } => {
            position(*x, *y)?;
            vec![
                mouse(*x, *y, PointerFlags::LEFT_BUTTON | PointerFlags::DOWN),
                mouse(*x, *y, PointerFlags::LEFT_BUTTON),
            ]
        }
        Kind::Drag { x, y, to_x, to_y } => {
            position(*x, *y)?;
            position(*to_x, *to_y)?;
            vec![
                mouse(*x, *y, PointerFlags::LEFT_BUTTON | PointerFlags::DOWN),
                mouse(*to_x, *to_y, PointerFlags::MOVE),
                mouse(*to_x, *to_y, PointerFlags::LEFT_BUTTON),
            ]
        }
    })
}
fn denied_packet(frame: &[u8]) -> bool {
    let Ok(X224(pdu)) = decode::<X224<SendDataIndication<'_>>>(frame) else {
        return false;
    };
    matches!(
        decode::<ServerSetErrorInfoPdu>(&pdu.user_data),
        Ok(ServerSetErrorInfoPdu(ErrorInfo::ProtocolIndependentCode(
            ProtocolIndependentCode::ServerDeniedConnection
        )))
    )
}

async fn run(config: Config) -> Result<()> {
    let certs = rustls_pemfile::certs(&mut Cursor::new(fs::read(&config.certificate)?))
        .collect::<std::io::Result<Vec<_>>>()?;
    ensure!(
        certs.len() == 1,
        "test requires one explicitly pinned certificate"
    );
    let expected = certs[0].clone();
    let mut roots = rustls::RootCertStore::empty();
    roots.add(expected.clone())?;
    let tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let tcp = TcpStream::connect(("127.0.0.1", config.port)).await?;
    let mut connector = ClientConnector::new(connector_config(&config), tcp.local_addr()?);
    let audio_stats=Arc::new(Mutex::new(audio::Stats::default()));
    if let Some(tones)=config.audio_probe {connector.attach_static_channel(ironrdp_rdpsnd::client::Rdpsnd::new(Box::new(audio::Backend::new(audio_stats.clone(),tones))));}
    let mut wire = TokioFramed::new(tcp);
    let upgrade = connect_begin(&mut wire, &mut connector).await?;
    let (tcp, leftover) = wire.into_inner();
    ensure!(leftover.is_empty(), "unexpected bytes before TLS");
    let tls = TlsConnector::from(Arc::new(tls))
        .connect("localhost".try_into()?, tcp)
        .await?;
    ensure!(
        tls.get_ref().1.peer_certificates().and_then(|v| v.first()) == Some(&expected),
        "certificate differs from exact pin"
    );
    println!("{}", json!({"event":"tls_verified","uid":config.uid}));
    let mut wire = TokioFramed::new(tls);
    let upgraded = mark_as_upgraded(upgrade, &mut connector);
    let result = connect_finalize(
        upgraded,
        connector,
        &mut wire,
        &mut NoNetwork,
        "localhost".into(),
        Vec::new(),
        None,
    )
    .await?;
    let size = (result.desktop_size.width, result.desktop_size.height);
    ensure!(
        size.0 > 0 && size.0 <= 4096 && size.1 > 0 && size.1 <= 2160,
        "unexpected geometry"
    );
    let mut image = DecodedImage::new(PixelFormat::BgrA32, size.0, size.1);
    let mut stage = ActiveStageBuilder {
        static_channels: result.static_channels,
        user_channel_id: result.user_channel_id,
        io_channel_id: result.io_channel_id,
        message_channel_id: result.message_channel_id,
        share_id: result.share_id,
        compression_type: result.compression_type,
        enable_server_pointer: false,
        pointer_software_rendering: false,
    }
    .build();
    let start = Instant::now();
    let scan_ms = if config.motion_probe.is_some() {
        5
    } else {
        400
    };
    let mut timer = tokio::time::interval(Duration::from_millis(scan_ms.min(100)));
    timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut graphics = 0u64;
    let mut unique_ticks = 0u64;
    let mut last_tick = None;
    let mut last_fresh = Instant::now();
    let mut scan_at = Instant::now();
    let mut action = 0;
    let mut latency_pending: Option<(u32, Instant)> = None;
    let mut latency_samples = Vec::new();
    let mut last_report=Instant::now();
    loop {
        tokio::select! {
            read=wire.read_pdu()=>{
                let (kind,frame)=read?;
                if denied_packet(&frame) {
                    ensure!(config.expect_denied && graphics==0,"server denied a required session");
                    ensure!(audio_stats.lock().unwrap().received==0,"unauthenticated audio received");
                    println!("{}",json!({"event":"check_complete","result":"PASS","access_denied":true,"graphics":0,"audio_packets":0}));return Ok(());
                }
                for output in stage.process(&mut image,kind,&frame)? {
                    match output {
                        ActiveStageOutput::ResponseFrame(bytes)=>{if !bytes.is_empty(){wire.write_all(&bytes).await?;}},
                        ActiveStageOutput::GraphicsUpdate(_)=>{graphics+=1;if config.service_probe {last_fresh=Instant::now();}ensure!(!config.expect_denied,"unauthorized client received graphics");},
                        ActiveStageOutput::Terminate(reason)=>bail!("unexpected server termination: {reason}"),
                        ActiveStageOutput::DeactivateAll=>bail!("unexpected desktop reactivation"),
                        _=>{},
                    }
                }
            },
            _=timer.tick()=>{
                if last_report.elapsed()>=Duration::from_secs(60) {
                    last_report=Instant::now();let stats=audio_stats.lock().unwrap();
                    println!("{}",json!({"event":"check_progress","uid":config.uid,"elapsed_ms":start.elapsed().as_millis(),"graphics":graphics,"audio_frames":stats.frames,"audio_waves":stats.waves}));
                }
                if graphics>0 && config.audio_probe.is_none() && !config.service_probe && scan_at.elapsed()>=Duration::from_millis(scan_ms) {
                    scan_at=Instant::now();
                    let decoded = match config.motion_probe { Some(point) => motion_marker(&image, point), None => marker(&image) };
                    if let Some((uid,nonce,tick))=decoded {
                        ensure!(uid==config.uid && nonce==config.nonce,"WRONG USER OR AGENT FRAME");
                        if let Some((expected,sent))=latency_pending {
                            ensure!(tick<=expected,"unexpected document length during latency check");
                            if tick==expected {
                                let elapsed=sent.elapsed().as_micros() as u64;
                                latency_samples.push(elapsed);
                                latency_pending=None;
                                println!("{}",json!({"event":"input_visible","uid":uid,"microseconds":elapsed,"tick":tick}));
                            }
                        }
                        if last_tick!=Some(tick){last_tick=Some(tick);unique_ticks+=1;last_fresh=Instant::now();
                            println!("{}",json!({"event":"marker","uid":uid,"nonce":nonce,"tick":tick,"elapsed_ms":start.elapsed().as_millis()}));}
                    }
                }
                if !config.expect_denied && config.audio_probe.is_none() && !config.service_probe {ensure!(last_fresh.elapsed()<Duration::from_secs(6),"desktop marker stopped refreshing");}
                if let Some(current_tick)=last_tick
                    && action<config.actions.len() && start.elapsed().as_millis()>=u128::from(config.actions[action].at_ms) {
                    if config.measure_text_latency {
                        ensure!(latency_pending.is_none(),"previous text has not become visible before next action");
                        if let Kind::Text{text}=&config.actions[action].kind {
                            let expected=current_tick.checked_add(u32::try_from(text.encode_utf16().count())?).context("document length overflow")?;
                            latency_pending=Some((expected,Instant::now()));
                        }
                    }
                    // Pace mouse transitions so AppKit can activate a window
                    // and observe the drag instead of coalescing one burst.
                    let mouse_action=matches!(config.actions[action].kind,Kind::Click{..}|Kind::Drag{..});
                    let batch=if mouse_action {1} else {16};
                    for events in action_events(&config.actions[action],size)?.chunks(batch) {
                        for output in stage.process_fastpath_input(&mut image,events)? {
                            if let ActiveStageOutput::ResponseFrame(bytes)=output {wire.write_all(&bytes).await?;}
                        }
                        tokio::time::sleep(Duration::from_millis(if mouse_action {80} else {10})).await;
                    }
                    action+=1;println!("{}",json!({"event":"action_sent","uid":config.uid,"index":action}));
                }
                if start.elapsed()>=Duration::from_secs(config.seconds) {
                    ensure!(!config.expect_denied,"server did not explicitly deny the connection");
                    if config.audio_probe.is_some() {
                        let stats=audio_stats.lock().unwrap();
                        let ratio=stats.own/stats.foreign.max(1e-20);
                        println!("{}",json!({"event":"audio_measurement","uid":config.uid,"waves":stats.waves,"frames":stats.frames,
                            "own_power":stats.own,"foreign_power":stats.foreign,"isolation_db":10.*ratio.log10()}));
                        ensure!(stats.frames>config.seconds*48000*7/10,"audio delivery below 70 percent of realtime");
                        ensure!(!stats.invalid && stats.waves>=20 && stats.own/stats.waves.max(1) as f64>0.000001 && ratio>=10000.,"missing audio or foreign tone leaked");
                    } else if config.service_probe {
                        ensure!(graphics>0,"missing initial desktop; static desktops are otherwise valid");
                        if config.frame_summary {
                            let (w,h)=(usize::from(image.width()),usize::from(image.height()));let mut rows=Vec::new();
                            for y in [h/4,h/2,h*3/5,h*7/10,h*4/5,h*9/10] {
                                let mut counts=std::collections::HashMap::new();
                                for pixel in image.data()[y*w*4..(y+1)*w*4].as_chunks::<4>().0{*counts.entry([pixel[2],pixel[1],pixel[0]]).or_insert(0usize)+=1;}
                                if let Some((color,count))=counts.into_iter().max_by_key(|(_,n)|*n){rows.push(json!({"y":y,"dominant_rgb":color,"dominant_fraction":count as f64/w as f64}));}
                            }
                            println!("{}",json!({"event":"frame_summary","uid":config.uid,"width":w,"height":h,"rows":rows}));
                        }
                    } else {ensure!(unique_ticks>=3 && action==config.actions.len(),"incomplete frame/input check");}
                    if config.measure_text_latency {
                        ensure!(latency_pending.is_none() && latency_samples.len()==config.actions.len(),"missing visible input acknowledgements");
                        latency_samples.sort_unstable();
                        let percentile=|p:usize|latency_samples[(latency_samples.len()*p).div_ceil(100)-1];
                        println!("{}",json!({"event":"input_latency","uid":config.uid,"samples":latency_samples.len(),
                            "p50_us":percentile(50),"p95_us":percentile(95),"max_us":latency_samples.last(),
                            "scope":"decoded synthetic document pixels; raw RDP test client"}));
                    }
                    for output in stage.graceful_shutdown()?{if let ActiveStageOutput::ResponseFrame(bytes)=output{wire.write_all(&bytes).await?;}}
                    println!("{}",json!({"event":"check_complete","result":"PASS","uid":config.uid,"graphics":graphics,"unique_ticks":unique_ticks,"actions":action,"elapsed_ms":start.elapsed().as_millis()}));return Ok(());
                }
            }
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    ensure!(
        args.len() == 2,
        "Usage: zmacrds-rdp-check PRIVATE_CONFIG.json (loopback 3390 only)"
    );
    let config = read_config(&args[1])?;
    let seconds = config.seconds;
    tokio::time::timeout(Duration::from_secs(seconds + 20), run(config)).await??;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn passive_service_probe_rejects_input_and_synthetic_measurements() {
        use std::io::Write;
        let path=std::env::temp_dir().join(format!("zmacrds-passive-check-{}.json",std::process::id()));
        let mut file=fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path).unwrap();
        let base=json!({"certificate":"/unused-test-certificate","username":"fixture","password":"fixture",
            "uid":505,"nonce":1,"seconds":1,"service_probe":true,"actions":[]});
        for (field,value,valid) in [
            ("actions",json!([]),true),
            ("actions",json!([{"at_ms":0,"type":"text","text":"fixture"}]),false),
            ("motion_probe",json!([505,506]),false),
            ("audio_probe",json!([375,750]),false),
        ] {
            let mut config=base.clone();config[field]=value;
            file.set_len(0).unwrap();use std::io::Seek;file.rewind().unwrap();
            file.write_all(&serde_json::to_vec(&config).unwrap()).unwrap();file.flush().unwrap();
            assert_eq!(read_config(path.to_str().unwrap()).is_ok(),valid,"{field}");
        }
        drop(file);fs::remove_file(path).unwrap();
    }
    #[test]
    fn decodes_known_marker_and_rejects_corruption() {
        // Fixed fixture: M1 wire layout, CRC independently generated with
        // Python binascii.crc_hqx(payload, 0xffff), not this decoder's crc().
        let packet = [
            0xa5, 0x3c, 0, 0, 1, 0xf6, 7, 0x4b, 0x67, 0xfd, 0, 0, 3, 0xe8, 0, 0x4b,
        ];
        let mut raw = vec![0; 524 * 4 * 4];
        let row = 524 * 2 * 4;
        for (cell, color) in [[255, 0, 255, 255], [255, 255, 0, 255], [255, 0, 255, 255]]
            .iter()
            .enumerate()
        {
            raw[row + cell * 16..row + cell * 16 + 4].copy_from_slice(color);
        }
        for bit in 0..128 {
            if packet[bit / 8] & (1 << (7 - bit % 8)) != 0 {
                let at = row + (bit + 3) * 16;
                raw[at..at + 4].fill(255);
            }
        }
        assert_eq!(marker_bgra(&raw, 524, 4), Some((502, 122382333, 1000)));
        raw[row + 3 * 16..row + 3 * 16 + 4].fill(0);
        assert_eq!(marker_bgra(&raw, 524, 4), None);
        assert_eq!(marker_bgra(&raw[..100], 524, 4), None);
    }
}
