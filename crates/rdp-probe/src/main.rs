//! Loopback-only M2 test server, optionally bound to one same-user GUI agent.
macro_rules! println {($($arg:tt)*) => {crate::log::line(&format!($($arg)*),false)};}
macro_rules! eprintln {($($arg:tt)*) => {crate::log::line(&format!($($arg)*),true)};}
mod log;
mod audio;
mod bridge;
mod codecs;
mod multi;
mod local_auth;
mod native_desktop;
mod relay;
use anyhow::{Context, Result, ensure};
use ironrdp_server::{
    BitmapUpdate, ConnectionHandler, CredentialDecision, CredentialValidationError,
    CredentialValidator, Credentials, DesktopSize, DisplayUpdate, KeyboardEvent, MouseEvent,
    PixelFormat, PostConnectionAction, RdpServer, RdpServerDisplay, RdpServerDisplayUpdates,
    RdpServerInputHandler,
    tokio_rustls::{TlsAcceptor, rustls},
};
use serde::Deserialize;
use std::{
    collections::BTreeSet,
    fs,
    io::{Cursor, Read},
    net::SocketAddr,
    num::{NonZeroU16, NonZeroUsize},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use zeroize::{Zeroize, ZeroizeOnDrop};

const WIDTH: u16 = 1024;
const HEIGHT: u16 = 768;

#[derive(Deserialize, Zeroize, ZeroizeOnDrop)]
#[serde(deny_unknown_fields)]
struct Secret {
    username: String,
    password: String,
}

fn private_file(path: &Path) -> Result<Vec<u8>> {
    private_file_owned(path, unsafe { libc::geteuid() })
}

fn private_file_owned(path: &Path, owner: u32) -> Result<Vec<u8>> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let meta = file.metadata()?;
    ensure!(
        meta.is_file() && meta.uid() == owner && meta.permissions().mode() & 0o077 == 0,
        "private file must be regular, owned by this user, without group/other access"
    );
    ensure!(meta.len() <= 65536, "private file too large");
    let mut bytes = Vec::new();
    file.take(65537).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 65536, "private file grew too large");
    Ok(bytes)
}

// Avoid prefix-dependent password comparison. Username is not secret.
fn equal_secret(a: &[u8], b: &[u8]) -> bool {
    let mut difference = a.len() ^ b.len();
    for i in 0..a.len().max(b.len()) {
        difference |= usize::from(a.get(i).copied().unwrap_or(0) ^ b.get(i).copied().unwrap_or(0));
    }
    difference == 0
}

struct Auth {
    secret: Secret,
    authenticated: Arc<AtomicBool>,
}
#[async_trait::async_trait]
impl CredentialValidator for Auth {
    async fn validate(
        &self,
        creds: &Credentials,
    ) -> Result<CredentialDecision, CredentialValidationError> {
        let ok = creds.username == self.secret.username
            && creds.password.len() <= 1024
            && equal_secret(creds.password.as_bytes(), self.secret.password.as_bytes());
        self.authenticated.store(ok, Ordering::Release);
        println!("{{\"event\":\"authentication\",\"accepted\":{ok}}}");
        Ok(if ok {
            CredentialDecision::Accept
        } else {
            CredentialDecision::Reject
        })
    }
}

#[derive(Default, Clone)]
struct Pattern {
    frames: u64,
    key_down: u64,
    key_up: u64,
    unicode_units: u64,
    mouse_events: u64,
    x: u16,
    y: u16,
    buttons: u8,
    held: BTreeSet<(u8, bool)>,
    connections: u64,
}
#[derive(Clone)]
struct Probe {
    state: Arc<Mutex<Pattern>>,
    authenticated: Arc<AtomicBool>,
    bridge: Option<bridge::Config>,
    bridge_input: Arc<Mutex<bridge::Input>>,
}
impl Probe {
    fn clear_control(&self) {
        self.authenticated.store(false, Ordering::Release);
        self.bridge_input.lock().unwrap().close();
        let mut s = self.state.lock().unwrap();
        s.held.clear();
        s.buttons = 0;
    }
}
impl RdpServerInputHandler for Probe {
    fn keyboard(&mut self, event: KeyboardEvent) {
        if !self.authenticated.load(Ordering::Acquire) {
            return;
        }
        if self.bridge.is_some() {
            self.bridge_input.lock().unwrap().keyboard(event);
            return;
        }
        let mut s = self.state.lock().unwrap();
        match event {
            KeyboardEvent::Pressed { code, extended } => {
                s.held.insert((code, extended));
                s.key_down += 1;
            }
            KeyboardEvent::Released { code, extended } => {
                s.held.remove(&(code, extended));
                s.key_up += 1;
            }
            KeyboardEvent::UnicodePressed(_) => {
                s.unicode_units += 1;
                s.key_down += 1;
            }
            KeyboardEvent::UnicodeReleased(_) => {
                s.key_up += 1;
            }
            KeyboardEvent::Synchronize(_) => {}
        }
    }
    fn mouse(&mut self, event: MouseEvent) {
        if !self.authenticated.load(Ordering::Acquire) {
            return;
        }
        if self.bridge.is_some() {
            self.bridge_input.lock().unwrap().mouse(event);
            return;
        }
        let mut s = self.state.lock().unwrap();
        s.mouse_events += 1;
        match event {
            MouseEvent::Move { x, y } => {
                s.x = x.min(WIDTH - 1);
                s.y = y.min(HEIGHT - 1);
            }
            MouseEvent::LeftPressed => s.buttons |= 1,
            MouseEvent::LeftReleased => s.buttons &= !1,
            MouseEvent::RightPressed => s.buttons |= 2,
            MouseEvent::RightReleased => s.buttons &= !2,
            MouseEvent::MiddlePressed => s.buttons |= 4,
            MouseEvent::MiddleReleased => s.buttons &= !4,
            _ => {}
        }
    }
}
impl ConnectionHandler for Probe {
    fn on_accept(&mut self, peer: SocketAddr) -> bool {
        self.clear_control();
        if !peer.ip().is_loopback() {
            return false;
        }
        self.state.lock().unwrap().connections += 1;
        println!("{{\"event\":\"connection_accepted\"}}");
        true
    }
    fn on_disconnected(
        &mut self,
        _: SocketAddr,
        duration: Duration,
        error: Option<&anyhow::Error>,
    ) -> PostConnectionAction {
        self.clear_control();
        let s = self.state.lock().unwrap();
        println!(
            "{{\"event\":\"disconnected\",\"duration_ms\":{},\"error\":{},\"frames\":{},\"key_down\":{},\"key_up\":{},\"unicode_units\":{},\"mouse_events\":{}}}",
            duration.as_millis(),
            error.is_some(),
            s.frames,
            s.key_down,
            s.key_up,
            s.unicode_units,
            s.mouse_events
        );
        PostConnectionAction::Continue
    }
}

struct Updates {
    probe: Probe,
    timer: tokio::time::Interval,
}
#[async_trait::async_trait]
impl RdpServerDisplayUpdates for Updates {
    async fn next_update(&mut self) -> Result<Option<DisplayUpdate>> {
        self.timer.tick().await;
        ensure!(
            self.probe.authenticated.load(Ordering::Acquire),
            "display requires authentication"
        );
        let snapshot = {
            let mut s = self.probe.state.lock().unwrap();
            s.frames += 1;
            s.clone()
        };
        Ok(Some(DisplayUpdate::Bitmap(BitmapUpdate {
            x: 0,
            y: 0,
            width: NonZeroU16::new(WIDTH).unwrap(),
            height: NonZeroU16::new(HEIGHT).unwrap(),
            format: PixelFormat::BgrA32,
            data: draw(&snapshot).into(),
            stride: NonZeroUsize::new(usize::from(WIDTH) * 4).unwrap(),
        })))
    }
}
#[async_trait::async_trait]
impl RdpServerDisplay for Probe {
    async fn size(&mut self) -> DesktopSize {
        if let Some(config) = &self.bridge {
            return config.size();
        }
        DesktopSize {
            width: WIDTH,
            height: HEIGHT,
        }
    }
    async fn updates(&mut self) -> Result<Box<dyn RdpServerDisplayUpdates>> {
        // IronRDP's optional validator can be skipped if ClientInfo credentials
        // are absent. Never let that condition expose even this synthetic image.
        ensure!(
            self.authenticated.load(Ordering::Acquire),
            "missing authenticated identity"
        );
        if let Some(config) = &self.bridge {
            let updates = bridge::connect(config.clone(), self.bridge_input.clone()).await?;
            println!("{{\"event\":\"bridge_display_started\"}}");
            return Ok(Box::new(updates));
        }
        let mut timer = tokio::time::interval(Duration::from_millis(200));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        println!("{{\"event\":\"display_started\",\"width\":{WIDTH},\"height\":{HEIGHT}}}");
        Ok(Box::new(Updates {
            probe: self.clone(),
            timer,
        }))
    }
}

fn rect(pixels: &mut [u8], x: usize, y: usize, w: usize, h: usize, color: [u8; 4]) {
    for py in y..(y + h).min(usize::from(HEIGHT)) {
        for px in x..(x + w).min(usize::from(WIDTH)) {
            let i = (py * usize::from(WIDTH) + px) * 4;
            // BGRA also matches the uncompressed RDP surface wire layout.
            pixels[i..i + 4].copy_from_slice(&[color[2], color[1], color[0], color[3]]);
        }
    }
}
fn glyph(c: char) -> [u8; 7] {
    match c {
        '0' => [14, 17, 19, 21, 25, 17, 14],
        '1' => [4, 12, 4, 4, 4, 4, 14],
        '2' => [14, 17, 1, 2, 4, 8, 31],
        '3' => [30, 1, 1, 14, 1, 1, 30],
        '4' => [2, 6, 10, 18, 31, 2, 2],
        '5' => [31, 16, 16, 30, 1, 1, 30],
        '6' => [14, 16, 16, 30, 17, 17, 14],
        '7' => [31, 1, 2, 4, 8, 8, 8],
        '8' => [14, 17, 17, 14, 17, 17, 14],
        '9' => [14, 17, 17, 15, 1, 1, 14],
        'A' => [14, 17, 17, 31, 17, 17, 17],
        'B' => [30, 17, 17, 30, 17, 17, 30],
        'C' => [14, 17, 16, 16, 16, 17, 14],
        'D' => [30, 17, 17, 17, 17, 17, 30],
        'E' => [31, 16, 16, 30, 16, 16, 31],
        'F' => [31, 16, 16, 30, 16, 16, 16],
        'H' => [17, 17, 17, 31, 17, 17, 17],
        'I' => [14, 4, 4, 4, 4, 4, 14],
        'K' => [17, 18, 20, 24, 20, 18, 17],
        'L' => [16, 16, 16, 16, 16, 16, 31],
        'M' => [17, 27, 21, 21, 17, 17, 17],
        'N' => [17, 25, 21, 19, 17, 17, 17],
        'O' => [14, 17, 17, 17, 17, 17, 14],
        'P' => [30, 17, 17, 30, 16, 16, 16],
        'R' => [30, 17, 17, 30, 20, 18, 17],
        'S' => [15, 16, 16, 14, 1, 1, 30],
        'T' => [31, 4, 4, 4, 4, 4, 4],
        'U' => [17, 17, 17, 17, 17, 17, 14],
        'W' => [17, 17, 17, 21, 21, 27, 17],
        'X' => [17, 17, 10, 4, 10, 17, 17],
        'Y' => [17, 17, 10, 4, 4, 4, 4],
        'Z' => [31, 1, 2, 4, 8, 16, 31],
        ':' => [0, 4, 4, 0, 4, 4, 0],
        _ => [0; 7],
    }
}
fn label(pixels: &mut [u8], x: usize, y: usize, text: &str) {
    for (i, c) in text.chars().enumerate() {
        for (row, bits) in glyph(c).into_iter().enumerate() {
            for col in 0..5 {
                if bits & (1 << (4 - col)) != 0 {
                    rect(
                        pixels,
                        x + i * 24 + col * 4,
                        y + row * 4,
                        4,
                        4,
                        [220, 235, 245, 255],
                    );
                }
            }
        }
    }
}
fn draw(s: &Pattern) -> Vec<u8> {
    let mut p = vec![0; usize::from(WIDTH) * usize::from(HEIGHT) * 4];
    rect(&mut p, 0, 0, WIDTH.into(), HEIGHT.into(), [18, 33, 52, 255]);
    label(&mut p, 40, 40, "ZMACRDS RDP PROBE");
    for (i, line) in [
        format!("FRAME: {}", s.frames),
        format!("KEY DOWN: {}", s.key_down),
        format!("KEY UP: {}", s.key_up),
        format!("UNICODE: {}", s.unicode_units),
        format!("MOUSE: {}", s.mouse_events),
        format!("CONNECTION: {}", s.connections),
    ]
    .iter()
    .enumerate()
    {
        label(&mut p, 40, 130 + i * 64, line);
    }
    rect(
        &mut p,
        40,
        580,
        (s.frames as usize * 7) % 944,
        24,
        [55, 200, 160, 255],
    );
    rect(
        &mut p,
        usize::from(s.x),
        usize::from(s.y),
        18,
        18,
        if s.buttons > 0 {
            [250, 130, 50, 255]
        } else {
            [70, 180, 255, 255]
        },
    );
    p
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len()==8 && args[1]=="--native-desktop-holder" {
        return native_desktop::run(args[2].to_str().context("descriptor")?.parse()?,args[3].to_str().context("UID")?.parse()?,
            args[4].to_str().context("ready descriptor")?.parse()?,args[5].to_str().context("width")?.parse()?,args[6].to_str().context("height")?.parse()?,args[7].to_str().context("generation label")?).await;
    }
    if args.len() == 3 && args[1] == "--multi" {
        let runtime = multi::bootstrap(Path::new(&args[2]))?;
        return runtime.run().await;
    }
    if args.len() == 4 && args[1] == "--user-relay" {
        let broker_uid = args[3].to_str().context("invalid broker UID")?.parse()?;
        return relay::run(Path::new(&args[2]), broker_uid).await;
    }
    if args.len() == 3 && args[1] == "--check-bridge" {
        let config = serde_json::from_slice(&private_file(Path::new(&args[2]))?)?;
        return bridge::check(config).await;
    }
    ensure!(
        args.len() == 4 || args.len() == 5,
        "Usage: zmacrds-rdp-probe CERT.pem KEY.pem CREDENTIALS.json [BRIDGE.json] (127.0.0.1:3390 only)"
    );
    let mut bytes = private_file(Path::new(&args[3]))?;
    let secret: Secret = serde_json::from_slice(&bytes).context("invalid credentials file")?;
    bytes.zeroize();
    ensure!(
        !secret.username.is_empty()
            && secret.username.len() <= 64
            && secret.password.len() >= 16
            && secret.password.len() <= 1024,
        "invalid test credential lengths"
    );
    let tls = load_tls(Path::new(&args[1]), Path::new(&args[2]))?;
    let bridge = if let Some(path) = args.get(4) {
        let config: bridge::Config = serde_json::from_slice(&private_file(Path::new(path))?)?;
        config.validate()?;
        Some(config)
    } else {
        None
    };
    let desktop_bridge = bridge.is_some();
    let probe = Probe {
        state: Arc::new(Mutex::new(Pattern::default())),
        authenticated: Arc::new(AtomicBool::new(false)),
        bridge,
        bridge_input: Arc::default(),
    };
    let auth = Auth {
        secret,
        authenticated: probe.authenticated.clone(),
    };
    let mut server = RdpServer::builder()
        .with_addr(([127, 0, 0, 1], 3390))
        .with_tls(tls)
        .with_input_handler(probe.clone())
        .with_display_handler(probe.clone())
        .with_bitmap_codecs(codecs::capabilities())
        .with_credential_validator(Some(Arc::new(auth)))
        .with_connection_handler(Some(Box::new(probe)))
        .with_max_request_size(1024 * 1024)
        .with_desktop_resize_support(false)
        .build();
    println!(
        "{{\"event\":\"starting\",\"address\":\"127.0.0.1:3390\",\"security\":\"TLS\",\"desktop_bridge\":{desktop_bridge}}}"
    );
    tokio::task::LocalSet::new()
        .run_until(async {
            tokio::select! {result=server.run()=>result, _=tokio::signal::ctrl_c()=>Ok(())}
        })
        .await
}

fn load_tls(cert: &Path, key: &Path) -> Result<TlsAcceptor> {
    let key_bytes = zeroize::Zeroizing::new(private_file(key)?);
    let key = rustls_pemfile::private_key(&mut Cursor::new(key_bytes.as_slice()))?
        .context("missing TLS private key")?;
    let cert_bytes = fs::read(cert)?;
    let certs =
        rustls_pemfile::certs(&mut Cursor::new(cert_bytes)).collect::<std::io::Result<Vec<_>>>()?;
    let tls = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    Ok(TlsAcceptor::from(Arc::new(tls)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pattern_uses_bgra_wire_order() {
        let pixels = draw(&Pattern::default());
        let i = (usize::from(HEIGHT) - 1) * usize::from(WIDTH) * 4;
        assert_eq!(&pixels[i..i + 4], &[52, 33, 18, 255]);
    }
    #[tokio::test]
    async fn failed_login_clears_any_prior_authentication() {
        let gate = Arc::new(AtomicBool::new(false));
        let auth = Auth {
            secret: Secret {
                username: "probe".into(),
                password: "a-test-password-with-entropy".into(),
            },
            authenticated: gate.clone(),
        };
        let mut credentials = Credentials {
            username: "probe".into(),
            password: "a-test-password-with-entropy".into(),
            domain: None,
        };
        assert_eq!(
            auth.validate(&credentials).await.unwrap(),
            CredentialDecision::Accept
        );
        assert!(gate.load(Ordering::Acquire));
        credentials.password.push('x');
        assert_eq!(
            auth.validate(&credentials).await.unwrap(),
            CredentialDecision::Reject
        );
        assert!(!gate.load(Ordering::Acquire));
        let mut probe = Probe {
            state: Arc::default(),
            authenticated: gate.clone(),
            bridge: None,
            bridge_input: Arc::default(),
        };
        gate.store(true, Ordering::Release);
        assert!(probe.on_accept("127.0.0.1:10000".parse().unwrap()));
        assert!(!gate.load(Ordering::Acquire));
        assert!(!probe.on_accept("192.0.2.1:10000".parse().unwrap()));
    }
    #[tokio::test]
    async fn unauthenticated_or_disconnected_clients_cannot_control_or_observe() {
        let mut p = Probe {
            state: Arc::default(),
            authenticated: Arc::new(AtomicBool::new(false)),
            bridge: None,
            bridge_input: Arc::default(),
        };
        p.keyboard(KeyboardEvent::UnicodePressed(65));
        p.mouse(MouseEvent::LeftPressed);
        assert_eq!(p.state.lock().unwrap().key_down, 0);
        assert!(p.updates().await.is_err());
        p.authenticated.store(true, Ordering::Release);
        p.keyboard(KeyboardEvent::Pressed {
            code: 30,
            extended: false,
        });
        p.mouse(MouseEvent::LeftPressed);
        assert_eq!(p.state.lock().unwrap().key_down, 1);
        p.clear_control();
        {
            let s = p.state.lock().unwrap();
            assert!(s.held.is_empty());
            assert_eq!(s.buttons, 0);
        }
        assert!(p.updates().await.is_err());
    }
    #[test]
    fn passwords_require_exact_bytes() {
        assert!(equal_secret(b"test-secret", b"test-secret"));
        for wrong in [
            b"test-secre".as_slice(),
            b"test-secretX",
            b"Test-secret",
            b"",
        ] {
            assert!(!equal_secret(b"test-secret", wrong));
        }
    }
}
