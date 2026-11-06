//! The unprivileged TLS broker delegates password verification to the local
//! root controller. Passwords travel only in bounded in-memory IPC messages.
use serde_json::json;
use std::{io::{BufRead, BufReader, Read, Write}, os::{fd::AsRawFd, unix::net::UnixStream}, time::Duration};

pub async fn verify(uid: u32, username: &str, password: &str) -> bool {
    if password.is_empty() || password.len() > 1024 || password.contains('\0') { return false; }
    let request = zeroize::Zeroizing::new(serde_json::to_vec(&json!({
        "action":"authenticate-rdp", "uid":uid, "username":username, "password":password
    })).unwrap_or_default());
    tokio::task::spawn_blocking(move || -> Option<bool> {
        let mut stream=UnixStream::connect("/var/run/org.zmacrds.control.sock").ok()?;
        let mut peer=0; let mut group=0;
        if unsafe { libc::getpeereid(stream.as_raw_fd(), &mut peer, &mut group) } != 0 || peer != 0 { return None; }
        stream.set_read_timeout(Some(Duration::from_secs(95))).ok()?;
        stream.set_write_timeout(Some(Duration::from_secs(2))).ok()?;
        stream.write_all(&request).ok()?; stream.write_all(b"\n").ok()?;
        let mut reply=Vec::new();
        let n=BufReader::new(stream).take(4097).read_until(b'\n',&mut reply).ok()?;
        if n>4096 || !reply.ends_with(b"\n") { return None; }
        let result:serde_json::Value=serde_json::from_slice(&reply).ok()?;
        Some(result["ok"]==true && result["uid"]==uid)
    }).await.ok().flatten().unwrap_or(false)
}
