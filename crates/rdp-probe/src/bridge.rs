//! Same-user IPC with a bounded latest complete image and explicit repaint.
use anyhow::{Context, Result, bail, ensure};
use ironrdp_server::{
    BitmapUpdate, DesktopSize, DisplayUpdate, KeyboardEvent, MouseEvent, PixelFormat,
    RdpServerDisplayUpdates,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    num::{NonZeroU16, NonZeroUsize},
    path::Path,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixStream,
    sync::{mpsc, watch},
    task::JoinHandle,
};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Identity {
    uid: u32,
    audit_session_id: u32,
    nonce: u32,
    epoch: u64,
    boot_id: String,
}
#[derive(Clone, Deserialize)]
pub struct Config {
    #[serde(flatten)]
    identity: Identity,
    socket: String,
    width: u16,
    height: u16,
    schema: u32,
}
impl Config {
    pub fn service(uid:u32,width:u16,height:u16)->Self {
        Self {identity:Identity {uid,audit_session_id:1,nonce:1,epoch:1,boot_id:"service-discovered-by-fixed-uid-relay".into()},
            socket:"/tmp/zmacrds-m2-service/agent.sock".into(),width,height,schema:1}
    }

    pub fn validate(&self) -> Result<()> {
        self.validate_for_uid(unsafe { libc::geteuid() })
    }
    pub fn validate_for_uid(&self, uid: u32) -> Result<()> {
        ensure!(
            self.schema == 1 && self.identity.uid == uid && self.identity.uid != 0,
            "bridge must belong to this ordinary user"
        );
        ensure!(
            self.width > 0
                && self.width <= 4096
                && self.height > 0
                && self.height <= 2160
                && self.width.is_multiple_of(4),
            "unsupported desktop geometry"
        );
        ensure!(
            self.socket.starts_with("/tmp/zmacrds-m2-") && self.socket.len() < 104,
            "invalid bridge socket path"
        );
        Ok(())
    }
    pub fn uid(&self) -> u32 {
        self.identity.uid
    }
    pub async fn open_agent(&self) -> Result<UnixStream> {
        self.validate()?;
        let stream = UnixStream::connect(&self.socket).await?;
        ensure!(
            stream.peer_cred()?.uid() == self.identity.uid,
            "unexpected agent UID"
        );
        Ok(stream)
    }
    pub fn size(&self) -> DesktopSize {
        DesktopSize {
            width: self.width,
            height: self.height,
        }
    }
}
#[derive(Deserialize)]
struct Hello {
    #[serde(flatten)]
    identity: Identity,
    width: u16,
    height: u16,
    lease: u32,
    schema: u32,
}
#[derive(Default)]
pub struct Input {
    pub audio: Arc<super::audio::Audio>,
    sender: Option<mpsc::Sender<Value>>,
    high_surrogate: Option<u16>,
    suspended: bool,
    capture_generation: u64,
}
impl Input {
    pub fn close(&mut self) {
        self.audio.invalidate();
        self.sender = None;
        self.high_surrogate = None;
        self.suspended=true;self.capture_generation=0;
    }
    fn send(&mut self, mut event: Value) {
        if self.suspended {self.high_surrogate=None;return;}
        event["capture_generation"]=json!(self.capture_generation);
        if let Some(sender) = &self.sender {
            // A full input queue closes the lease instead of losing a key-up.
            if sender.try_send(event).is_err() {
                self.close();
            }
        }
    }
    pub fn keyboard(&mut self, event: KeyboardEvent) {
        match event {
            KeyboardEvent::Pressed { code, extended }
            | KeyboardEvent::Released { code, extended } => {
                if let Some(mac) = mac_key(code, extended) {
                    self.send(json!({"type":"key","code":mac,"down":matches!(event,KeyboardEvent::Pressed{..})}));
                }
            }
            KeyboardEvent::UnicodePressed(unit) => {
                if (0xd800..=0xdbff).contains(&unit) {
                    self.high_surrogate = Some(unit);
                    return;
                }
                let text = if (0xdc00..=0xdfff).contains(&unit) {
                    self.high_surrogate
                        .take()
                        .and_then(|high| String::from_utf16(&[high, unit]).ok())
                } else {
                    self.high_surrogate = None;
                    String::from_utf16(&[unit]).ok()
                };
                if let Some(text) = text {
                    self.send(json!({"type":"text","text":text}));
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        let value = match event {
            MouseEvent::Move { x, y } => json!({"type":"move","x":x,"y":y}),
            MouseEvent::LeftPressed => json!({"type":"button","button":0,"down":true}),
            MouseEvent::LeftReleased => json!({"type":"button","button":0,"down":false}),
            MouseEvent::RightPressed => json!({"type":"button","button":1,"down":true}),
            MouseEvent::RightReleased => json!({"type":"button","button":1,"down":false}),
            MouseEvent::MiddlePressed => json!({"type":"button","button":2,"down":true}),
            MouseEvent::MiddleReleased => json!({"type":"button","button":2,"down":false}),
            MouseEvent::VerticalScroll { value } => json!({"type":"scroll","x":0,"y":value}),
            MouseEvent::Scroll { x, y } => json!({"type":"scroll","x":x,"y":y}),
            _ => return,
        };
        self.send(value);
    }
}

// RDP set-1 scancodes -> macOS virtual keycodes. Windows maps to Command;
// Alt maps to Option. Unsupported keys are ignored, never guessed.
fn mac_key(code: u8, extended: bool) -> Option<u16> {
    if extended {
        return Some(match code {
            0x1c => 76,
            0x1d => 62,
            0x35 => 75,
            0x38 => 61,
            0x47 => 115,
            0x48 => 126,
            0x49 => 116,
            0x4b => 123,
            0x4d => 124,
            0x4f => 119,
            0x50 => 125,
            0x51 => 121,
            0x53 => 117,
            0x5b => 55,
            0x5c => 54,
            _ => return None,
        });
    }
    Some(match code {
        0x01 => 53,
        0x02 => 18,
        0x03 => 19,
        0x04 => 20,
        0x05 => 21,
        0x06 => 23,
        0x07 => 22,
        0x08 => 26,
        0x09 => 28,
        0x0a => 25,
        0x0b => 29,
        0x0c => 27,
        0x0d => 24,
        0x0e => 51,
        0x0f => 48,
        0x10 => 12,
        0x11 => 13,
        0x12 => 14,
        0x13 => 15,
        0x14 => 17,
        0x15 => 16,
        0x16 => 32,
        0x17 => 34,
        0x18 => 31,
        0x19 => 35,
        0x1a => 33,
        0x1b => 30,
        0x1c => 36,
        0x1d => 59,
        0x1e => 0,
        0x1f => 1,
        0x20 => 2,
        0x21 => 3,
        0x22 => 5,
        0x23 => 4,
        0x24 => 38,
        0x25 => 40,
        0x26 => 37,
        0x27 => 41,
        0x28 => 39,
        0x29 => 50,
        0x2a => 56,
        0x2b => 42,
        0x2c => 6,
        0x2d => 7,
        0x2e => 8,
        0x2f => 9,
        0x30 => 11,
        0x31 => 45,
        0x32 => 46,
        0x33 => 43,
        0x34 => 47,
        0x35 => 44,
        0x36 => 60,
        0x37 => 67,
        0x38 => 58,
        0x39 => 49,
        0x3b => 122,
        0x3c => 120,
        0x3d => 99,
        0x3e => 118,
        0x3f => 96,
        0x40 => 97,
        0x41 => 98,
        0x42 => 100,
        0x43 => 101,
        0x44 => 109,
        0x47 => 89,
        0x48 => 91,
        0x49 => 92,
        0x4a => 78,
        0x4b => 86,
        0x4c => 87,
        0x4d => 88,
        0x4e => 69,
        0x4f => 83,
        0x50 => 84,
        0x51 => 85,
        0x52 => 82,
        0x53 => 65,
        0x57 => 103,
        0x58 => 111,
        _ => return None,
    })
}

async fn read_packet(
    reader: &mut (impl AsyncReadExt + Unpin),
    max: usize,
) -> Result<(u8, Vec<u8>)> {
    let len = reader.read_u32().await? as usize;
    ensure!(len > 0 && len <= max, "oversized IPC packet");
    let kind = reader.read_u8().await?;
    let mut payload = vec![0; len - 1];
    reader.read_exact(&mut payload).await?;
    Ok((kind, payload))
}
async fn read_packet_recovering(
    reader: &mut (impl AsyncReadExt + Unpin), max: usize,
    mut on_stall: impl FnMut()->Result<()>,
) -> Result<(u8,Vec<u8>)> {
    // Never cancel and restart a partially read packet at the soft deadline.
    let packet=read_packet(reader,max);
    tokio::pin!(packet);
    let soft=tokio::time::sleep(Duration::from_secs(3));
    let hard=tokio::time::sleep(Duration::from_secs(30));
    tokio::pin!(soft,hard);
    let mut warned=false;
    loop {tokio::select! {
        result=&mut packet=>return result,
        _=&mut soft,if !warned=>{on_stall()?;warned=true;},
        _=&mut hard=>bail!("GUI bridge unresponsive for 30 seconds"),
    }}
}
fn paused_bitmap(config:&Config)->BitmapUpdate {
    let w=usize::from(config.width);let h=usize::from(config.height);
    let mut data=[36u8,25,20,255].repeat(w*h);
    let text=format!("ZMACRDS UID {}\nDESKTOP CAPTURE PAUSED\nWAIT FOR THE MAC SESSION\nINPUT DISABLED",config.uid());
    for (line,text) in text.lines().enumerate() {
        for (column,ch) in text.chars().enumerate() {
            for (row,bits) in super::glyph(ch).into_iter().enumerate() {
                for bit in 0..5 {if bits&(1<<(4-bit))!=0 {
                    for dy in 0..3 {for dx in 0..3 {
                        let x=40+column*18+bit*3+dx;let y=40+line*32+row*3+dy;
                        if x<w && y<h {data[(y*w+x)*4..(y*w+x)*4+4].copy_from_slice(&[245,235,220,255]);}
                    }}
                }}
            }
        }
    }
    BitmapUpdate {x:0,y:0,width:NonZeroU16::new(config.width).unwrap(),height:NonZeroU16::new(config.height).unwrap(),
        stride:NonZeroUsize::new(w*4).unwrap(),format:PixelFormat::BgrA32,data:data.into()}
}
fn decode_bitmap(config: &Config, data: Vec<u8>, patch: bool) -> Result<BitmapUpdate> {
    let header = if patch { 20 } else { 12 };
    ensure!(data.len() >= header, "missing pixel header");
    let field = |at| u32::from_be_bytes(data[at..at + 4].try_into().unwrap());
    let (x, y, at) = if patch {
        (field(0), field(4), 8)
    } else {
        (0, 0, 0)
    };
    let (w, h, stride) = (field(at), field(at + 4), field(at + 8));
    let (dw, dh) = (u32::from(config.width), u32::from(config.height));
    ensure!(
        w > 0 && h > 0 && w <= dw && h <= dh && x <= dw - w && y <= dh - h,
        "pixel region outside desktop"
    );
    ensure!(patch || (w, h) == (dw, dh), "frame geometry changed");
    ensure!(
        stride == w * 4 && data.len() == header + (stride * h) as usize,
        "invalid pixel payload"
    );
    let bytes: bytes::Bytes = data.into();
    Ok(BitmapUpdate {
        x: x as u16,
        y: y as u16,
        width: NonZeroU16::new(w as u16).unwrap(),
        height: NonZeroU16::new(h as u16).unwrap(),
        format: PixelFormat::BgrA32,
        data: bytes.slice(header..),
        stride: NonZeroUsize::new(stride as usize).unwrap(),
    })
}
fn reconstruct(previous: &mut Option<BitmapUpdate>, bitmap: BitmapUpdate) -> Result<BitmapUpdate> {
    if let Some(full) = previous.as_ref()
        && (bitmap.x != 0 || bitmap.y != 0 || bitmap.width != full.width || bitmap.height != full.height) {
            let mut data = full.data.to_vec();
            let row_bytes = usize::from(bitmap.width.get()) * 4;
            for row in 0..usize::from(bitmap.height.get()) {
                let to = (usize::from(bitmap.y) + row) * full.stride.get() + usize::from(bitmap.x) * 4;
                let from = row * bitmap.stride.get();
                ensure!(to + row_bytes <= data.len() && from + row_bytes <= bitmap.data.len(), "patch exceeds baseline");
                data[to..to + row_bytes].copy_from_slice(&bitmap.data[from..from + row_bytes]);
            }
            let mut result = full.clone();
            result.data = data.into();
            *previous = Some(result.clone());
            return Ok(result);
    }
    ensure!(bitmap.x == 0 && bitmap.y == 0, "missing complete baseline");
    *previous = Some(bitmap.clone());
    Ok(bitmap)
}

#[derive(Clone,Debug)]
struct Snapshot {
    bitmap: BitmapUpdate,
    generation: u64,
}
pub struct Updates {
    frames: watch::Receiver<Option<Snapshot>>,
    repaint: tokio::time::Interval,
    last_repaint: tokio::time::Instant,
    baseline: Option<BitmapUpdate>,
    refresh_requested: bool,
    generation: u64,
    reader: JoinHandle<()>,
    writer: JoinHandle<()>,
    input: Arc<Mutex<Input>>,
}
impl Drop for Updates {
    fn drop(&mut self) {
        self.input.lock().unwrap().close();
        self.reader.abort();
        self.writer.abort();
    }
}
#[async_trait::async_trait]
impl RdpServerDisplayUpdates for Updates {
    fn request_refresh(&mut self) {
        self.refresh_requested=true;
        if let Some(sender)=self.input.lock().unwrap().sender.as_ref() {
            // Coalesced UI refreshes may be retried by the normal repaint
            // timer if this already-bounded control queue is saturated.
            let _=sender.try_send(json!({"type":"ping","repaint":true}));
        }
    }
    async fn next_update(&mut self) -> Result<Option<DisplayUpdate>> {
        // IronRDP retries Err immediately. A closed lease is terminal, not a
        // transient capture error: return None so it disconnects this client
        // and cannot busy-loop/starve the other users on the same runtime.
        loop {
            // A closed/revoked bridge must never replay its cached desktop.
            if self.frames.has_changed().is_err() {
                return Ok(None);
            }
            if self.refresh_requested {
                self.refresh_requested=false;
                let snapshot=self.frames.borrow_and_update().clone();
                if let Some(snapshot)=snapshot {
                    self.generation=snapshot.generation;
                    self.baseline=Some(snapshot.bitmap.clone());
                    self.last_repaint=tokio::time::Instant::now();
                    return Ok(Some(DisplayUpdate::BitmapRefresh(snapshot.bitmap)));
                }
                // A paused/revoked source must not replay its old baseline.
                self.baseline=None;
            }
            tokio::select! {
                biased;
                changed=self.frames.changed()=>{
                    if changed.is_err(){return Ok(None);}
                    let bitmap=self.frames.borrow_and_update().clone();
                    if let Some(snapshot)=bitmap {
                        let bitmap=snapshot.bitmap;
                        let refresh=self.baseline.is_none() || self.generation!=snapshot.generation || self.last_repaint.elapsed()>=Duration::from_secs(2);
                        self.generation=snapshot.generation;
                        self.baseline=Some(bitmap.clone());
                        if refresh {self.last_repaint=tokio::time::Instant::now();return Ok(Some(DisplayUpdate::BitmapRefresh(bitmap)));}
                        return Ok(Some(DisplayUpdate::Bitmap(bitmap)));
                    } else {
                        self.baseline=None;
                    }
                },
                _=self.repaint.tick()=>{
                    if self.last_repaint.elapsed()>=Duration::from_secs(2)
                        && let Some(bitmap)=self.baseline.clone() {
                            self.last_repaint=tokio::time::Instant::now();
                            return Ok(Some(DisplayUpdate::BitmapRefresh(bitmap)));
                    }
                }
            }
        }
    }
}

pub async fn connect(config: Config, input: Arc<Mutex<Input>>) -> Result<Updates> {
    let stream = config.open_agent().await?;
    connect_stream(config, input, stream).await
}

// Only the root-bootstrap-created route may call this. The relay socket is
// inside the broker's private directory; its listener was bound by root before
// the fixed-UID worker inherited it. It never names the GUI agent socket.
pub async fn connect_relay(
    config: Config,
    input: Arc<Mutex<Input>>,
    path: &Path,
    dynamic: bool,
) -> Result<Updates> {
    config.validate_for_uid(config.uid())?;
    let stream = UnixStream::connect(path).await?;
    ensure!(
        matches!(stream.peer_cred()?.uid(), 0) || stream.peer_cred()?.uid() == config.uid(),
        "unexpected relay UID"
    );
    connect_stream_mode(config, input, stream, dynamic).await
}

async fn connect_stream(
    config: Config,
    input: Arc<Mutex<Input>>,
    stream: UnixStream,
) -> Result<Updates> {connect_stream_mode(config,input,stream,false).await}

pub async fn checked_greeting(stream:&mut UnixStream,config:&Config)->Result<Vec<u8>> {
    let (kind,payload)=tokio::time::timeout(Duration::from_secs(3),read_packet(stream,65536)).await??;
    ensure!(kind==1,"missing agent greeting");let hello:Hello=serde_json::from_slice(&payload)?;
    ensure!(hello.identity==config.identity && hello.schema==1 && hello.width==config.width && hello.height==config.height && hello.lease!=0,"stale GUI descriptor");
    let mut packet=((payload.len()+1) as u32).to_be_bytes().to_vec();packet.push(1);packet.extend(payload);Ok(packet)
}
async fn connect_stream_mode(config:Config,input:Arc<Mutex<Input>>,mut stream:UnixStream,dynamic:bool)->Result<Updates> {
    let (kind, payload) =
        tokio::time::timeout(Duration::from_secs(3), read_packet(&mut stream, 65536)).await??;
    ensure!(kind == 1, "missing bridge greeting");
    let hello: Hello = serde_json::from_slice(&payload)?;
    ensure!(
        hello.schema == 1
            && ((!dynamic && hello.identity == config.identity) || (dynamic && hello.identity.uid==config.uid() && hello.identity.audit_session_id>0 && hello.identity.nonce>0 && hello.identity.epoch>0 && !hello.identity.boot_id.is_empty()))
            && hello.width == config.width
            && hello.height == config.height
            && hello.lease != 0,
        "stale bridge identity"
    );
    eprintln!("{}",json!({"event":"bridge_identity","uid":hello.identity.uid,"audit":hello.identity.audit_session_id,"nonce":hello.identity.nonce,"dynamic":dynamic}));
    let (mut read, mut write) = stream.into_split();
    // Independent complete snapshots can replace queued snapshots. Patches
    // must first be reconstructed against the previous complete image.
    let (frames, receiver) = watch::channel(None);
    let (sender, mut commands) = mpsc::channel::<Value>(128);
    let recovery_sender=sender.downgrade();
    {let mut state=input.lock().unwrap();state.sender=Some(sender);state.suspended=true;state.capture_generation=0;}
    let reader_input=input.clone();
    let reader = tokio::spawn(async move {
        let result: Result<()> = async {
            let mut initialized = false;
            let mut complete: Option<BitmapUpdate> = None;
            let mut generation=0u64;
            let mut native_generation=0u64;
            let mut need_transition=false;
            let mut resync_id=0u32;
            let mut required_resync=None;
            let mut resync_deadline=None;
            loop {
                if resync_deadline.is_some_and(|deadline|tokio::time::Instant::now()>=deadline) {
                    bail!("GUI did not acknowledge bounded resynchronization");
                }
                let mut stalled=false;
                let (kind,payload)=read_packet_recovering(&mut read,36*1024*1024,|| {
                    {let mut state=reader_input.lock().unwrap();state.suspended=true;state.audio.invalidate();}
                    generation+=1;stalled=true;
                    resync_id=resync_id.checked_add(1).context("resynchronization counter exhausted")?;
                    required_resync=Some(resync_id);
                    resync_deadline=Some(tokio::time::Instant::now()+Duration::from_secs(30));
                    recovery_sender.upgrade().context("controller closed during resynchronization")?
                        .try_send(json!({"type":"ping","resync":resync_id})).context("cannot queue bounded GUI resynchronization")?;
                    frames.send(Some(Snapshot {bitmap:paused_bitmap(&config),generation}))?;
                    eprintln!("{}",json!({"event":"bridge_waiting_for_gui","grace_seconds":30}));
                    Ok(())
                }).await?;
                if stalled {complete=None;initialized=false;need_transition=true;}
                match kind {
                    2 | 5 => {
                        let bitmap = decode_bitmap(&config, payload, kind == 5)?;
                        if need_transition {continue;}
                        ensure!(kind == 2 || initialized, "patch before full desktop");
                        let bitmap = reconstruct(&mut complete, bitmap)?;
                        if frames.send(Some(Snapshot {bitmap,generation})).is_err() {
                            return Ok(());
                        }
                        initialized = true;
                        let mut input=reader_input.lock().unwrap();
                        if input.suspended {
                            eprintln!("{}",json!({"event":"bridge_pixels_resumed","uid":hello.identity.uid,"lease":hello.lease,"generation":native_generation}));
                        }
                        input.suspended=false;
                    }
                    3 => {
                        let heartbeat: Value = serde_json::from_slice(&payload)?;
                        ensure!(heartbeat["lease"] == hello.lease, "wrong bridge lease");
                    }
                    7 => {
                        let state=reader_input.lock().unwrap();
                        state.audio.submit(&payload,native_generation,hello.lease,!state.suspended && initialized && !need_transition)?;
                    }
                    6 => {
                        ensure!(payload.len()<=512,"oversized capture state");
                        #[derive(Deserialize)]
                        #[serde(deny_unknown_fields)]
                        struct CaptureState {lease:u32,generation:u64,paused:bool,#[serde(default)]resync:u32}
                        let state:CaptureState=serde_json::from_slice(&payload)?;
                        ensure!(state.lease==hello.lease && state.generation>native_generation,"invalid capture transition");
                        native_generation=state.generation;generation+=1;
                        complete=None;initialized=false;
                        need_transition=state.paused || required_resync.is_some_and(|id|id!=state.resync);
                        if !need_transition {required_resync=None;resync_deadline=None;}
                        {let mut input=reader_input.lock().unwrap();input.suspended=true;input.audio.invalidate();input.high_surrogate=None;input.capture_generation=native_generation;}
                        let snapshot=state.paused.then(||Snapshot {bitmap:paused_bitmap(&config),generation});
                        if frames.send(snapshot).is_err(){return Ok(());}
                        eprintln!("{}",json!({"event":"bridge_capture_state","uid":hello.identity.uid,"lease":hello.lease,"paused":state.paused,"generation":native_generation,"resync":state.resync,"awaiting_transition":need_transition}));
                    }
                    _ => bail!("unexpected bridge packet"),
                }
            }
        }
        .await;
        if let Err(error) = result {
            eprintln!("{}", json!({"event":"bridge_reader_ended","error":format!("{error:#}")}));
            // Dropping the sole sender ends exactly this viewer.
            drop(frames);
        }
    });
    let writer_audio=input.lock().unwrap().audio.clone();
    let writer = tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_millis(500));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut seq = 0u64;
        loop {
            // Keep full capture samples until region updates pass the native
            // Windows App repaint test. Its display can otherwise retain black
            // areas after a dialog or window transition. The RDP encoder still
            // computes dirty rectangles from these complete samples.
            let event = tokio::select! {event=commands.recv()=>match event{Some(v)=>v,None=>break},_=tick.tick()=>json!({"type":"ping","regions":false,"audio":writer_audio.started()})};
            let mut message = serde_json::to_value(&hello.identity).unwrap();
            seq += 1;
            message["lease"] = json!(hello.lease);
            message["seq"] = json!(seq);
            for (key, value) in event.as_object().unwrap() {
                message[key] = value.clone();
            }
            let payload = serde_json::to_vec(&message).unwrap();
            if payload.len() > 4095 {
                break;
            }
            let result = async {
                write.write_u32((payload.len() + 1) as u32).await?;
                write.write_u8(4).await?;
                write.write_all(&payload).await
            }
            .await;
            if result.is_err() {
                break;
            }
        }
    });
    Ok(Updates {
        frames: receiver,
        repaint: tokio::time::interval(Duration::from_secs(2)),
        last_repaint: tokio::time::Instant::now(),
        baseline: None,
        refresh_requested: false,
        generation: 0,
        reader,
        writer,
        input,
    })
}

/// Bounded VM integration check: consumes one frame, injects no OS input.
pub async fn check(config: Config) -> Result<()> {
    config.validate()?;
    async fn open(config: &Config) -> Result<(UnixStream, Value)> {
        let mut stream = UnixStream::connect(&config.socket).await?;
        ensure!(
            stream.peer_cred()?.uid() == config.identity.uid,
            "wrong peer UID"
        );
        let (kind, body) = read_packet(&mut stream, 65536).await?;
        ensure!(kind == 1, "missing greeting");
        let hello: Hello = serde_json::from_slice(&body)?;
        ensure!(
            hello.identity == config.identity && hello.lease != 0,
            "wrong identity"
        );
        Ok((stream, serde_json::from_slice(&body)?))
    }
    async fn send(stream: &mut UnixStream, message: &Value) -> Result<()> {
        let bytes = serde_json::to_vec(message)?;
        stream.write_u32((bytes.len() + 1) as u32).await?;
        stream.write_u8(4).await?;
        stream.write_all(&bytes).await?;
        Ok(())
    }
    async fn expect_close(stream: &mut UnixStream, seconds:u64) -> Result<()> {
        tokio::time::timeout(Duration::from_secs(seconds), async {
            loop {
                if read_packet(stream, 36 * 1024 * 1024).await.is_err() {
                    return;
                }
            }
        })
        .await
        .context("agent did not close invalid lease")?;
        Ok(())
    }
    tokio::time::timeout(Duration::from_secs(60),async {
        let (mut stream,mut hello)=open(&config).await?;
        let mut second=UnixStream::connect(&config.socket).await?;
        ensure!(second.read_u8().await.is_err(),"second controller accepted");
        hello["type"]=json!("ping");hello["seq"]=json!(1);
        send(&mut stream,&hello).await?;
        loop {let(kind,body)=read_packet(&mut stream,36*1024*1024).await?;if kind==2{decode_bitmap(&config,body,false)?;break;}}
        send(&mut stream,&hello).await?;expect_close(&mut stream,4).await?;
        let (mut stream,mut hello)=open(&config).await?;
        hello["type"]=json!("ping");hello["seq"]=json!(1);hello["audit_session_id"]=json!(config.identity.audit_session_id+1);
        send(&mut stream,&hello).await?;expect_close(&mut stream,4).await?;
        let (mut stream,_)=open(&config).await?;
        stream.write_u32(65536).await?;expect_close(&mut stream,4).await?;
        let (mut stream,_)=open(&config).await?;
        let started=std::time::Instant::now();expect_close(&mut stream,35).await?;
        ensure!(started.elapsed()>=Duration::from_millis(29800),"lease expired unexpectedly early");
        println!("{{\"event\":\"bridge_check\",\"result\":\"PASS\",\"exclusive\":true,\"frame_geometry\":true,\"replay_rejected\":true,\"stale_session_rejected\":true,\"oversized_rejected\":true,\"heartbeat_expiry\":true}}");
        Ok(())
    }).await?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn fixed_uid_relay_rejects_a_stale_or_foreign_agent_greeting() {
        let config=Config::service(502,1920,1200);
        for foreign in [false,true] {
            let (mut source,mut peer)=UnixStream::pair().unwrap();
            let mut message=serde_json::to_value(&config.identity).unwrap();
            message["schema"]=json!(1);message["width"]=json!(1920);message["height"]=json!(1200);message["lease"]=json!(8);
            if foreign {message["uid"]=json!(503);}
            let body=serde_json::to_vec(&message).unwrap();
            peer.write_u32((body.len()+1) as u32).await.unwrap();peer.write_u8(1).await.unwrap();peer.write_all(&body).await.unwrap();
            let result=checked_greeting(&mut source,&config).await;
            assert_eq!(result.is_ok(),!foreign);
        }
    }
    fn frame(value: u8) -> BitmapUpdate {
        BitmapUpdate { x:0,y:0,width:NonZeroU16::new(4).unwrap(),height:NonZeroU16::new(2).unwrap(),
            stride:NonZeroUsize::new(16).unwrap(),format:PixelFormat::BgrA32,data:vec![value;32].into() }
    }

    fn sample(value:u8)->Snapshot {Snapshot {bitmap:frame(value),generation:0}}
    #[tokio::test(start_paused=true)]
    async fn soft_timeout_preserves_a_partial_header_and_same_packet() {
        let (mut reader,mut writer)=tokio::io::duplex(64);
        tokio::spawn(async move {
            writer.write_all(&[0,0]).await.unwrap();
            tokio::time::sleep(Duration::from_secs(8)).await;
            writer.write_all(&[0,4,6,7,8,9]).await.unwrap();
        });
        let mut pauses=0;
        let (kind,payload)=read_packet_recovering(&mut reader,64,||{pauses+=1;Ok(())}).await.unwrap();
        assert_eq!((kind,payload),(6,vec![7,8,9]));assert_eq!(pauses,1);
    }
    #[tokio::test(start_paused=true)]
    async fn soft_timeout_is_bounded_by_a_hard_transport_deadline() {
        let (mut reader,_writer)=tokio::io::duplex(64);let mut pauses=0;
        let start=tokio::time::Instant::now();
        assert!(read_packet_recovering(&mut reader,64,||{pauses+=1;Ok(())}).await.is_err());
        assert_eq!(pauses,1);assert_eq!(start.elapsed(),Duration::from_secs(30));
    }
    #[tokio::test]
    async fn malformed_packet_is_not_given_a_recovery_grace() {
        let (mut reader,mut writer)=tokio::io::duplex(64);
        writer.write_all(&65u32.to_be_bytes()).await.unwrap();
        let mut pauses=0;
        assert!(read_packet_recovering(&mut reader,64,||{pauses+=1;Ok(())}).await.is_err());
        assert_eq!(pauses,0);
    }
    #[tokio::test]
    async fn input_retains_its_enqueue_generation_and_paused_input_is_not_queued() {
        let (sender,mut receiver)=mpsc::channel(4);
        let mut input=Input {sender:Some(sender),..Input::default()};
        input.keyboard(KeyboardEvent::Pressed {code:0x21,extended:false});
        input.suspended=true;input.capture_generation=4;
        input.keyboard(KeyboardEvent::Pressed {code:0x22,extended:false});
        assert_eq!(receiver.recv().await.unwrap()["capture_generation"],0);
        assert!(receiver.try_recv().is_err());
        input.suspended=false;input.keyboard(KeyboardEvent::Pressed {code:0x21,extended:false});
        assert_eq!(receiver.recv().await.unwrap()["capture_generation"],4);
    }
    fn stream() -> (watch::Sender<Option<Snapshot>>, Updates) {
        let (sender,frames)=watch::channel(None);
        (sender,Updates {frames,repaint:tokio::time::interval(Duration::from_secs(2)),
            last_repaint:tokio::time::Instant::now(),baseline:None,refresh_requested:false,generation:0,
            reader:tokio::spawn(std::future::pending()),writer:tokio::spawn(std::future::pending()),input:Arc::default()})
    }

    #[tokio::test(start_paused = true)]
    async fn stalled_transport_requires_its_matching_resync_ack_before_pixels_or_input_resume() {
        async fn send(writer:&mut (impl AsyncWriteExt+Unpin),kind:u8,body:Vec<u8>) {
            writer.write_u32(u32::try_from(body.len()+1).unwrap()).await.unwrap();
            writer.write_u8(kind).await.unwrap();writer.write_all(&body).await.unwrap();
        }
        fn pixels(value:u8)->Vec<u8> {
            let mut bytes=Vec::new();for n in [4u32,2,16]{bytes.extend(n.to_be_bytes());}bytes.extend([value;32]);bytes
        }
        let config:Config=serde_json::from_value(json!({"uid":502,"audit_session_id":7,"nonce":9,"epoch":1,"boot_id":"unit",
            "socket":"/tmp/zmacrds-m2-unit/agent.sock","schema":1,"width":4,"height":2})).unwrap();
        let (client,mut agent)=UnixStream::pair().unwrap();let mut hello=serde_json::to_value(&config.identity).unwrap();
        for (key,value) in [("lease",8),("width",4),("height",2),("schema",1)]{hello[key]=json!(value);}
        send(&mut agent,1,serde_json::to_vec(&hello).unwrap()).await;
        let input=Arc::new(Mutex::new(Input::default()));let mut updates=connect_stream(config,input.clone(),client).await.unwrap();
        let (mut reader,mut writer)=agent.into_split();
        send(&mut writer,6,serde_json::to_vec(&json!({"lease":8,"generation":1,"paused":false,"resync":0})).unwrap()).await;
        send(&mut writer,2,pixels(7)).await;
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
        tokio::task::yield_now().await;tokio::time::advance(Duration::from_millis(3100)).await;
        let mut requested=0;
        for _ in 0..10 {
            let (kind,body)=read_packet(&mut reader,4096).await.unwrap();assert_eq!(kind,4);
            let command:Value=serde_json::from_slice(&body).unwrap();
            if let Some(id)=command["resync"].as_u64(){requested=id;break;}
        }
        assert_eq!(requested,1);assert!(input.lock().unwrap().suspended);
        send(&mut writer,6,serde_json::to_vec(&json!({"lease":8,"generation":2,"paused":false,"resync":0})).unwrap()).await;
        send(&mut writer,2,pixels(8)).await;
        for _ in 0..10 {tokio::task::yield_now().await;}
        assert!(input.lock().unwrap().suspended,"pre-stall state cannot reopen input");
        send(&mut writer,6,serde_json::to_vec(&json!({"lease":8,"generation":3,"paused":false,"resync":requested})).unwrap()).await;
        send(&mut writer,2,pixels(9)).await;
        match updates.next_update().await.unwrap().unwrap() {
            DisplayUpdate::BitmapRefresh(bitmap)=>assert_eq!(&bitmap.data[..],&[9;32]),
            _=>panic!("matching resynchronization must repaint the new full baseline"),
        }
        assert!(!input.lock().unwrap().suspended);assert_eq!(input.lock().unwrap().capture_generation,3);
        input.lock().unwrap().close();
        tokio::time::timeout(Duration::from_millis(100),&mut updates.writer).await.unwrap().unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn client_refresh_is_immediate_but_cannot_replay_paused_or_closed_capture() {
        let (sender,mut updates)=stream();
        sender.send(Some(sample(7))).unwrap();
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
        let now=tokio::time::Instant::now();
        updates.request_refresh();
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
        assert_eq!(tokio::time::Instant::now(),now);
        sender.send(None).unwrap();updates.request_refresh();
        assert!(tokio::time::timeout(Duration::from_millis(10),updates.next_update()).await.is_err());
        assert!(updates.baseline.is_none());
        sender.send(Some(sample(9))).unwrap();updates.request_refresh();
        match updates.next_update().await.unwrap().unwrap() {
            DisplayUpdate::BitmapRefresh(bitmap)=>assert_eq!(&bitmap.data[..],&[9;32]),
            _=>panic!("refresh must use the new valid source"),
        }
        drop(sender);updates.request_refresh();
        assert!(updates.next_update().await.unwrap().is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn static_desktop_repaints_without_new_capture_and_never_after_eof() {
        let (sender,mut updates)=stream();
        sender.send(Some(sample(7))).unwrap();
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
        tokio::time::advance(Duration::from_secs(2)).await;
        match updates.next_update().await.unwrap().unwrap() {
            DisplayUpdate::BitmapRefresh(bitmap)=>assert_eq!(&bitmap.data[..],&[7;32]),
            _=>panic!("idle recovery must repaint the whole baseline"),
        }
        drop(sender);
        assert!(updates.next_update().await.unwrap().is_none());
    }

    #[tokio::test]
    async fn capture_transition_survives_watch_coalescing_and_forces_full_repaint() {
        let (sender,mut updates)=stream();
        sender.send(Some(sample(7))).unwrap();
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
        sender.send(None).unwrap();
        sender.send(Some(Snapshot {bitmap:frame(7),generation:2})).unwrap();
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
    }
    #[tokio::test(start_paused=true)]
    async fn suspended_baseline_is_synthetic_and_never_repaints_previous_desktop() {
        let (sender,mut updates)=stream();
        sender.send(Some(sample(7))).unwrap();updates.next_update().await.unwrap();
        sender.send(None).unwrap();
        assert!(tokio::time::timeout(Duration::from_millis(10),updates.next_update()).await.is_err());
        assert!(updates.baseline.is_none());
        sender.send(Some(Snapshot {bitmap:frame(0),generation:1})).unwrap();
        updates.next_update().await.unwrap();
        tokio::time::advance(Duration::from_secs(2)).await;
        match updates.next_update().await.unwrap().unwrap() {
            DisplayUpdate::BitmapRefresh(bitmap)=>assert_eq!(&bitmap.data[..],&[0;32]),
            _=>panic!("only the synthetic pause screen may repaint"),
        }
        sender.send(Some(Snapshot {bitmap:frame(7),generation:2})).unwrap();
        assert!(matches!(updates.next_update().await.unwrap(),Some(DisplayUpdate::BitmapRefresh(_))));
        drop(sender);assert!(updates.next_update().await.unwrap().is_none());
    }
    #[tokio::test]
    async fn slow_consumer_gets_latest_complete_image_instead_of_old_backlog() {
        let (sender,mut updates)=stream();
        for value in 0..100 {sender.send(Some(sample(value))).unwrap();}
        match updates.next_update().await.unwrap().unwrap() {
            DisplayUpdate::BitmapRefresh(bitmap)=>assert_eq!(&bitmap.data[..],&[99;32]),
            _=>panic!("first image must establish a complete baseline"),
        }
    }

    #[test]
    fn dependent_regions_are_reconstructed_before_latest_image_can_replace_them() {
        let mut previous=None;
        reconstruct(&mut previous,frame(0)).unwrap();
        for (x,y,value) in [(1,0,3),(3,1,9)] {
            reconstruct(&mut previous,BitmapUpdate {x,y,width:NonZeroU16::new(1).unwrap(),
                height:NonZeroU16::new(1).unwrap(),stride:NonZeroUsize::new(4).unwrap(),
                format:PixelFormat::BgrA32,data:vec![value;4].into()}).unwrap();
        }
        let full=previous.unwrap();
        assert_eq!((full.x,full.y,full.width.get(),full.height.get()),(0,0,4,2));
        assert_eq!(&full.data[4..8],&[3;4]);
        assert_eq!(&full.data[28..32],&[9;4]);
        assert_eq!(&full.data[8..28],&[0;20]);
    }
    #[tokio::test]
    async fn terminal_bridge_closure_ends_stream_instead_of_retrying_errors() {
        let (sender, frames) = watch::channel(None);
        let mut updates = Updates {
            frames,
            repaint: tokio::time::interval(Duration::from_secs(2)),
            last_repaint: tokio::time::Instant::now(),
            baseline: None,
            refresh_requested: false,
            generation: 0,
            reader: tokio::spawn(std::future::pending()),
            writer: tokio::spawn(std::future::pending()),
            input: Arc::default(),
        };
        drop(sender);
        for _ in 0..2 {
            assert!(
                tokio::time::timeout(Duration::from_millis(50), updates.next_update())
                    .await
                    .unwrap()
                    .unwrap()
                    .is_none()
            );
        }
    }
    #[test]
    fn rejects_truncated_and_mismatched_frames() {
        let config: Config=serde_json::from_value(json!({"uid":502,"audit_session_id":7,"nonce":9,"epoch":1,"boot_id":"test","socket":"/tmp/zmacrds-m2-test/agent.sock","schema":1,"width":4,"height":1})).unwrap();
        assert!(decode_bitmap(&config, vec![0; 11], false).is_err());
        let mut frame = vec![0; 28];
        frame[..4].copy_from_slice(&4u32.to_be_bytes());
        frame[4..8].copy_from_slice(&1u32.to_be_bytes());
        frame[8..12].copy_from_slice(&16u32.to_be_bytes());
        assert!(decode_bitmap(&config, frame.clone(), false).is_ok());
        let mut region = Vec::new();
        for word in [2u32, 0, 2, 1, 8] {
            region.extend(word.to_be_bytes());
        }
        region.extend([3; 8]);
        let patch = decode_bitmap(&config, region.clone(), true).unwrap();
        assert_eq!(
            (patch.x, patch.y, patch.width.get(), patch.data.len()),
            (2, 0, 2, 8)
        );
        region[3] = 3;
        assert!(decode_bitmap(&config, region, true).is_err());
        frame[3] = 8;
        assert!(decode_bitmap(&config, frame, false).is_err());
    }
    #[test]
    fn maps_modifiers_arrows_and_unknown_keys_explicitly() {
        assert_eq!(mac_key(0x2a, false), Some(56));
        assert_eq!(mac_key(0x1d, true), Some(62));
        assert_eq!(mac_key(0x5b, true), Some(55));
        assert_eq!(mac_key(0x4b, true), Some(123));
        assert_eq!(mac_key(0xff, false), None);
    }
    #[tokio::test]
    async fn unicode_surrogates_and_overflow_are_bounded() {
        let (sender, mut receiver) = mpsc::channel(2);
        let mut input = Input {
            sender: Some(sender),
            high_surrogate: None,
            ..Input::default()
        };
        input.keyboard(KeyboardEvent::UnicodePressed(0xd83c));
        assert!(receiver.try_recv().is_err());
        input.keyboard(KeyboardEvent::UnicodePressed(0xdf0d));
        assert_eq!(receiver.recv().await.unwrap()["text"], "🌍");
        input.keyboard(KeyboardEvent::UnicodePressed(0xdf0d));
        assert!(receiver.try_recv().is_err());
        for _ in 0..3 {
            input.mouse(MouseEvent::LeftPressed);
        }
        assert!(input.sender.is_none());
    }
}
