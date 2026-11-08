//! Private process adapter for the browser pilot. A separate RDP connection
//! owns each adapter, including its audio. Credentials are read once on stdin.
use super::*;
use ironrdp_rdpsnd::{
    client::RdpsndClientHandler,
    pdu::{AudioFormat, PitchPdu, VolumePdu, WaveFormat},
};
use std::{borrow::Cow, io::BufRead, net::Ipv4Addr};
use tokio::{
    io::{self, AsyncWriteExt},
    sync::{mpsc, oneshot, watch},
};
use zeroize::{Zeroize, Zeroizing};

const WIDTH: usize = 1920;
const HEIGHT: usize = 1200;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Login {
    username: String,
    password: String,
}
impl Drop for Login {
    fn drop(&mut self) {
        self.password.zeroize();
    }
}

#[derive(Debug)]
struct Sound {
    formats: [AudioFormat; 1],
    queue: mpsc::Sender<Vec<u8>>,
}
impl RdpsndClientHandler for Sound {
    fn get_formats(&self) -> &[AudioFormat] {
        &self.formats
    }
    fn wave(&mut self, format: usize, captured_ms: u32, data: Cow<'_, [u8]>) {
        if format == 0 && !data.is_empty() && data.len() <= 19200 && data.len().is_multiple_of(4) {
            let mut packet = Vec::with_capacity(4 + data.len());
            packet.extend_from_slice(&captured_ms.to_be_bytes());
            packet.extend_from_slice(&data);
            let _ = self.queue.try_send(packet);
        }
    }
    fn set_volume(&mut self, _: VolumePdu) {}
    fn set_pitch(&mut self, _: PitchPdu) {}
    fn close(&mut self) {}
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Input {
    Move {
        x: u16,
        y: u16,
    },
    Button {
        x: u16,
        y: u16,
        button: u8,
        down: bool,
    },
    Scroll {
        x: u16,
        y: u16,
        delta: i16,
    },
    Key {
        code: u8,
        extended: bool,
        down: bool,
    },
    Text {
        text: String,
    },
}

struct Geometry {
    source: (usize, usize),
    fit: (usize, usize),
    inset: (usize, usize),
    columns: Vec<usize>,
    rows: Vec<usize>,
}
impl Geometry {
    fn new(width: u16, height: u16) -> Result<Self> {
        ensure!(
            (64..=4096).contains(&width) && (64..=2160).contains(&height),
            "unexpected desktop size"
        );
        let (w, h) = (usize::from(width), usize::from(height));
        let (fw, fh) = if WIDTH * h <= HEIGHT * w {
            (WIDTH, h * WIDTH / w)
        } else {
            (w * HEIGHT / h, HEIGHT)
        };
        Ok(Self {
            source: (w, h),
            fit: (fw, fh),
            inset: ((WIDTH - fw) / 2, (HEIGHT - fh) / 2),
            columns: (0..fw).map(|x| x * w / fw * 4).collect(),
            rows: (0..fh).map(|y| y * h / fh * w * 4).collect(),
        })
    }
    fn image(&self, bgra: &[u8]) -> Vec<u8> {
        if self.source == (WIDTH, HEIGHT) {
            return bgra.to_vec();
        }
        let mut out = vec![0; WIDTH * HEIGHT * 4];
        for (y, offset) in self.rows.iter().copied().enumerate() {
            let start = ((self.inset.1 + y) * WIDTH + self.inset.0) * 4;
            let row = &mut out[start..start + self.fit.0 * 4];
            if self.fit.0 == self.source.0 {
                row.copy_from_slice(&bgra[offset..offset + self.fit.0 * 4]);
            } else {
                for (dst, x) in row.chunks_exact_mut(4).zip(self.columns.iter().copied()) {
                    dst.copy_from_slice(&bgra[offset + x..offset + x + 4]);
                }
            }
        }
        out
    }
    fn point(&self, x: u16, y: u16) -> (u16, u16) {
        let map = |n: u16, inset: usize, fit: usize, source: usize| {
            (usize::from(n).saturating_sub(inset).min(fit - 1) * source / fit) as u16
        };
        (
            map(x, self.inset.0, self.fit.0, self.source.0),
            map(y, self.inset.1, self.fit.1, self.source.1),
        )
    }
    fn events(&self, input: Input) -> Result<Vec<Event>> {
        Ok(match input {
            Input::Move { x, y } => {
                let (x, y) = self.point(x, y);
                vec![mouse(x, y, PointerFlags::MOVE)]
            }
            Input::Button { x, y, button, down } => {
                let (x, y) = self.point(x, y);
                let button = match button {
                    0 => PointerFlags::LEFT_BUTTON,
                    1 => PointerFlags::MIDDLE_BUTTON_OR_WHEEL,
                    2 => PointerFlags::RIGHT_BUTTON,
                    _ => bail!("invalid button"),
                };
                vec![
                    mouse(x, y, PointerFlags::MOVE),
                    mouse(
                        x,
                        y,
                        button
                            | if down {
                                PointerFlags::DOWN
                            } else {
                                PointerFlags::empty()
                            },
                    ),
                ]
            }
            Input::Scroll { x, y, delta } => {
                let (x, y) = self.point(x, y);
                vec![
                    mouse(x, y, PointerFlags::MOVE),
                    Event::MouseEvent(MousePdu {
                        flags: PointerFlags::VERTICAL_WHEEL,
                        x_position: x,
                        y_position: y,
                        number_of_wheel_rotation_units: delta.clamp(-255, 255),
                    }),
                ]
            }
            Input::Key {
                code,
                extended,
                down,
            } => {
                ensure!(code > 0 && code < 128, "invalid keyboard code");
                action_events(
                    &Action {
                        at_ms: 0,
                        kind: Kind::Key {
                            code,
                            extended,
                            down,
                        },
                    },
                    (0, 0),
                )?
            }
            Input::Text { text } => action_events(
                &Action {
                    at_ms: 0,
                    kind: Kind::Text { text },
                },
                (0, 0),
            )?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_hd_desktop_retains_original_pixels_and_coordinates() {
        let native = Geometry::new(1920, 1200).unwrap();
        let mut frame = vec![0; 1920 * 1200 * 4];
        for (i, value) in frame.iter_mut().enumerate() {
            *value = (i % 251) as u8;
        }
        assert_eq!(native.image(&frame), frame);
        assert_eq!(native.point(1919, 1199), (1919, 1199));

        let wide = Geometry::new(1920, 1080).unwrap();
        let pixels = &frame[..1920 * 1080 * 4];
        let image = wide.image(pixels);
        let bar = 60 * 1920 * 4;
        assert_eq!(&image[bar..image.len() - bar], pixels);
        assert_eq!(wide.point(1919, 1139), (1919, 1079));
    }
}

fn line(input: &mut impl BufRead) -> Result<Option<Zeroizing<Vec<u8>>>> {
    let mut bytes = Zeroizing::new(Vec::new());
    let count = input.take(4097).read_until(b'\n', &mut bytes)?;
    if count == 0 {
        return Ok(None);
    }
    ensure!(
        count <= 4096 && bytes.last() == Some(&b'\n'),
        "invalid adapter message length"
    );
    Ok(Some(bytes))
}

async fn packet(output: &mut io::Stdout, kind: u8, data: &[u8]) -> Result<()> {
    let len = (data.len() as u32).to_be_bytes();
    output
        .write_all(&[kind, len[0], len[1], len[2], len[3]])
        .await?;
    output.write_all(data).await?;
    output.flush().await?;
    Ok(())
}

pub async fn run(args: &[String]) -> Result<()> {
    ensure!(
        args.len() == 3,
        "--web-source PRIVATE_IPV4 PORT PINNED_CERTIFICATE"
    );
    let address: Ipv4Addr = args[0].parse()?;
    ensure!(
        address.is_private() || address.is_loopback(),
        "expected a private desktop host"
    );
    let port: u16 = args[1].parse()?;
    ensure!(port >= 1024, "expected an unprivileged port");
    let (events, mut keyboard) = mpsc::channel(32);
    let (login_send, login_receive) = oneshot::channel();
    // A blocking pipe read on its own thread does not hold up Tokio shutdown
    // if the server rejects or terminates this connection.
    std::thread::spawn(move || {
        let mut stdin = std::io::stdin().lock();
        let login = line(&mut stdin).and_then(|bytes| {
            Ok(serde_json::from_slice::<Login>(
                &bytes.context("missing login")?,
            )?)
        });
        let failed = login.is_err();
        if login_send.send(login).is_err() || failed {
            return;
        }
        loop {
            let item = match line(&mut stdin) {
                Ok(Some(bytes)) => {
                    serde_json::from_slice::<Input>(&bytes).map_err(anyhow::Error::from)
                }
                Ok(None) => break,
                Err(error) => Err(error),
            };
            let failed = item.is_err();
            if events.blocking_send(item).is_err() || failed {
                break;
            }
        }
    });
    let mut login: Login = login_receive.await??;
    ensure!(
        login.username.len() <= 64
            && !login.username.is_empty()
            && login
                .username
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
            && !login.password.is_empty()
            && login.password.len() <= 1024
            && !login.password.contains('\0'),
        "invalid login fields"
    );
    let mut config = Config {
        host: address.to_string(),
        port,
        certificate: args[2].clone().into(),
        username: std::mem::take(&mut login.username),
        password: std::mem::take(&mut login.password),
        uid: 0,
        nonce: 0,
        seconds: 0,
        motion_probe: None,
        audio_probe: None,
        service_probe: false,
        frame_summary: false,
        measure_text_latency: false,
        expect_denied: false,
        actions: vec![],
    };
    let (audio, mut pcm) = mpsc::channel(6);
    let sound = Sound {
        queue: audio,
        formats: [AudioFormat {
            format: WaveFormat::PCM,
            n_channels: 2,
            n_samples_per_sec: 48000,
            n_avg_bytes_per_sec: 192000,
            n_block_align: 4,
            bits_per_sample: 16,
            data: None,
        }],
    };
    let (mut wire, connection) = tokio::time::timeout(
        Duration::from_secs(100),
        connect_desktop(&config, Some(Box::new(sound))),
    )
    .await??;
    config.password.zeroize();
    drop(config);
    let (w, h) = (
        connection.desktop_size.width,
        connection.desktop_size.height,
    );
    let geometry = Geometry::new(w, h)?;
    let mut image = DecodedImage::new(PixelFormat::BgrA32, w, h);
    let mut stage = make_stage(connection);
    let (frame, mut latest) = watch::channel(None::<Arc<Vec<u8>>>);
    let mut sender = tokio::spawn(async move {
        let mut output = io::stdout();
        loop {
            tokio::select! {
                biased;
                audio=pcm.recv()=>if let Some(audio)=audio {packet(&mut output,2,&audio).await.context("write audio to web gateway")?;} else {break;},
                next=latest.changed()=>{
                    next?;
                    let value=latest.borrow_and_update().clone();
                    if let Some(value)=value {packet(&mut output,1,&value).await.context("write frame to web gateway")?;}
                }
            }
        }
        Ok::<(), anyhow::Error>(())
    });
    let mut ticker = tokio::time::interval(Duration::from_millis(33));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut dirty = false;
    loop {
        tokio::select! {
            outcome=&mut sender=>return outcome?,
            incoming=wire.read_pdu()=>{
                let (kind,bytes)=incoming.context("read RDP desktop")?;
                ensure!(!denied_packet(&bytes),"desktop authentication denied");
                for output in stage.process(&mut image,kind,&bytes)? {
                    match output {
                        ActiveStageOutput::ResponseFrame(data) if !data.is_empty()=>wire.write_all(&data).await.context("reply to RDP desktop")?,
                        ActiveStageOutput::GraphicsUpdate(_)=>dirty=true,
                        ActiveStageOutput::Terminate(_)|ActiveStageOutput::DeactivateAll=>bail!("desktop connection ended"),
                        _=>{}
                    }
                }
            }
            message=keyboard.recv()=>{
                let Some(input)=message else {break;};
                for output in stage.process_fastpath_input(&mut image,&geometry.events(input?)?)? {
                    if let ActiveStageOutput::ResponseFrame(data)=output {wire.write_all(&data).await.context("send input to RDP desktop")?;}
                }
            }
            _=ticker.tick(), if dirty=>{
                frame.send_replace(Some(Arc::new(geometry.image(image.data()))));
                dirty=false;
            }
        }
    }
    sender.abort();
    Ok(())
}
