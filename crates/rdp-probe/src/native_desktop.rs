//! Keep Apple's authenticated virtual display alive for its RDP user. The root
//! initializer passes an already-authenticated TCP socket, never a password.
//! Only a small raw rectangle is requested; pixels are discarded in memory.
use anyhow::{Result, ensure};
use std::{ffi::CStr, os::{fd::FromRawFd,unix::fs::{MetadataExt,PermissionsExt}}, path::PathBuf, time::{Duration, Instant}};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};

async fn discard(reader: &mut (impl AsyncRead + Unpin), mut count: usize) -> Result<()> {
    let mut bytes=[0u8;8192];
    while count>0 { let n=count.min(bytes.len());reader.read_exact(&mut bytes[..n]).await?;count-=n; }
    Ok(())
}
async fn update(reader: &mut (impl AsyncRead + Unpin)) -> Result<bool> {
    let kind=reader.read_u8().await?;
    tokio::time::timeout(Duration::from_secs(15), async {
        match kind {
            0 => {
                reader.read_u8().await?;let count=reader.read_u16().await?;
                ensure!(count<=4096,"native rectangle limit");let mut pixels=false;let mut total=0usize;
                for _ in 0..count {
                    let mut header=[0u8;12];reader.read_exact(&mut header).await?;
                    let width=usize::from(u16::from_be_bytes([header[4],header[5]]));
                    let height=usize::from(u16::from_be_bytes([header[6],header[7]]));
                    let encoding=i32::from_be_bytes(header[8..12].try_into()?);
                    let length=match encoding {0=>width.checked_mul(height).and_then(|n|n.checked_mul(4)), -223=>Some(0), _=>None};
                    let length=length.ok_or_else(||anyhow::anyhow!("unexpected native encoding"))?;
                    total=total.checked_add(length).ok_or_else(||anyhow::anyhow!("native frame overflow"))?;
                    ensure!(total<=128*1024*1024,"native frame limit");
                    discard(reader,length).await?;pixels|=length>0;
                }
                Ok(pixels)
            },
            2=>Ok(false),
            3=>{let mut header=[0u8;7];reader.read_exact(&mut header).await?;let count=u32::from_be_bytes(header[3..7].try_into()?) as usize;ensure!(count<=65536,"native text limit");discard(reader,count).await?;Ok(false)},
            _=>anyhow::bail!("unexpected native message"),
        }
    }).await?
}

pub async fn run(fd:i32, uid:u32, ready_fd:i32, width:u16, height:u16, token:&str)->Result<()> {
    ensure!(uid>=501 && unsafe{libc::getuid()}==uid && unsafe{libc::geteuid()}==uid,"holder must run as its desktop user");
    ensure!(fd>=3 && ready_fd>=3 && fd!=ready_fd && width>0 && height>0,"invalid inherited descriptors");
    ensure!(token.len()==32 && token.bytes().all(|c|c.is_ascii_hexdigit()),"invalid generation label");
    // Validate before adopting an arbitrary FD supplied on a command line.
    let mut peer:libc::sockaddr_in=unsafe{std::mem::zeroed()};let mut size=std::mem::size_of_val(&peer) as libc::socklen_t;
    ensure!(unsafe{libc::getpeername(fd,(&mut peer as *mut libc::sockaddr_in).cast(),&mut size)}==0 &&
        i32::from(peer.sin_family)==libc::AF_INET && peer.sin_addr.s_addr==u32::from_ne_bytes([127,0,0,1]) && u16::from_be(peer.sin_port)==5900,"expected authenticated local Apple socket");
    let mut kind=0;size=std::mem::size_of_val(&kind) as libc::socklen_t;
    ensure!(unsafe{libc::getsockopt(fd,libc::SOL_SOCKET,libc::SO_TYPE,(&mut kind as *mut i32).cast(),&mut size)}==0 && kind==libc::SOCK_STREAM,"expected TCP stream");
    let mut pipe:libc::stat=unsafe{std::mem::zeroed()};
    ensure!(unsafe{libc::fstat(ready_fd,&mut pipe)}==0 && pipe.st_mode & libc::S_IFMT==libc::S_IFIFO,"ready descriptor must be a pipe");
    let meta=unsafe{libc::getpwuid(uid)};ensure!(!meta.is_null(),"unknown desktop user");
    let home=unsafe{CStr::from_ptr((*meta).pw_dir)}.to_str()?;
    let state=PathBuf::from(home).join("Library/Application Support/ZMacRDS/Service/state.json");
    let socket=unsafe{std::net::TcpStream::from_raw_fd(fd)};socket.set_nonblocking(true)?;
    let mut socket=tokio::net::TcpStream::from_std(socket)?;
    let mut ready=Some(unsafe{std::fs::File::from_raw_fd(ready_fd)});
    std::eprintln!("native display holder started for UID {uid}");
    // SetPixelFormat: 32-bit little-endian BGRX. SetEncodings: raw only.
    socket.write_all(&[0,0,0,0,32,24,0,1,0,255,0,255,0,255,16,8,0,0,0,0,2,0,0,1,0,0,0,0]).await?;
    // Apple's standard RFB clients start in observe mode. Select control for
    // this authenticated user's display, as the existing VNC gateway does.
    socket.write_all(&[10,0,0,1]).await?;
    std::eprintln!("native pixel format selected");
    let mut request=vec![3,0,0,0,0,0];request.extend_from_slice(&width.min(64).to_be_bytes());request.extend_from_slice(&height.min(64).to_be_bytes());
    // Some Apple displays do not start updating until a full initial refresh.
    // Stream and discard that one frame, then keep only a tiny region alive.
    let mut initial=vec![3,0,0,0,0,0];initial.extend_from_slice(&width.to_be_bytes());initial.extend_from_slice(&height.to_be_bytes());
    let (mut reader,mut writer)=socket.into_split();
    let lease=PathBuf::from(format!("/var/run/zmacrds-control/native-{uid}.lease"));
    let mut activated=false;
    let mut timer=tokio::time::interval(Duration::from_secs(1));let start=Instant::now();let mut last_connected=start;let mut connected=false;let mut outstanding=0u8;let mut tick=0;
    loop {
        let frame=update(&mut reader);tokio::pin!(frame);
        loop {
            tokio::select! {
                result=&mut frame=>{let pixels=result?;outstanding=outstanding.saturating_sub(1);if pixels && let Some(mut pipe)=ready.take(){use std::io::Write;pipe.write_all(b"1")?;std::eprintln!("native initial pixels received");}break;},
                _=timer.tick()=>{
                    tick+=1;
                    let generation=std::fs::symlink_metadata(&lease).ok().filter(|m|m.is_file() && m.uid()==0 && m.len()<=4096 && m.permissions().mode()&0o022==0)
                        .and_then(|_|std::fs::read(&lease).ok()).and_then(|b|serde_json::from_slice::<serde_json::Value>(&b).ok());
                    if generation.as_ref().is_some_and(|g|g["token"]==token && g["uid"]==uid) {activated=true;}
                    else if activated {return Ok(());}
                    let mut fresh=false;
                    if let Ok(bytes)=crate::private_file_owned(&state,uid)
                        && let Ok(data)=serde_json::from_slice::<serde_json::Value>(&bytes)
                    {
                        let now=crate::audio::monotonic_ms();let at=data["uptime_ms"].as_u64().unwrap_or(0);
                        fresh=data["uid"].as_u64()==Some(u64::from(uid)) && at<=now && now-at<6000;
                        if activated && fresh && generation.as_ref().is_some_and(|g|g["audit_session_id"]!=data["audit_session_id"] || g["boot_id"]!=data["boot_id"]) {return Ok(());}
                        if fresh && (data["rdp_connected"]==true || data["rdp_connected"].as_u64()==Some(1)) {connected=true;last_connected=Instant::now();}
                    }
                    if (!connected && start.elapsed()>Duration::from_secs(120)) ||
                        (connected && last_connected.elapsed()>Duration::from_secs(if fresh{15}else{120})) {return Ok(());}
                    if tick%2==1 && outstanding<2 {writer.write_all(if tick==1 {&initial}else{&request}).await?;outstanding+=1;}
                }
            }
        }
    }
}

#[cfg(test)] mod tests {
    use super::*;
    #[tokio::test] async fn native_decoder_accepts_raw_pixels_and_rejects_unrequested_encodings() {
        let mut data=vec![0,0,0,1,0,0,0,0,0,1,0,1,0,0,0,0,10,20,30,0];
        assert!(update(&mut data.as_slice()).await.unwrap());
        data[15]=7;assert!(update(&mut data.as_slice()).await.is_err());
        let mut empty:&[u8]=&[0,0,0,0];assert!(!update(&mut empty).await.unwrap());
    }
}
