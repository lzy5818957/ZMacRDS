//! Fixed-UID local workers. No TLS, passwords, account selection or OS input.
//! A root bootstrap supplies a listener FD, then permanently drops the worker
//! to its configured UID. The broker communicates only over private sockets.
use anyhow::{Context, Result, ensure};
use std::{
    ffi::CString,
    fs, io,
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::{
            fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
            net::{UnixListener, UnixStream},
            process::CommandExt,
        },
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};
use tokio::io::{AsyncReadExt,AsyncWriteExt};

pub struct Directory {
    path: PathBuf,
    handle: fs::File,
    sockets: Vec<CString>,
}
impl Directory {
    pub fn create() -> Result<Self> {
        let mut template = b"/tmp/zmacrds-m3-XXXXXX\0".to_vec();
        let ptr = unsafe { libc::mkdtemp(template.as_mut_ptr().cast()) };
        ensure!(!ptr.is_null(), "cannot create private relay directory");
        let path = unsafe { std::ffi::CStr::from_ptr(ptr) }
            .to_str()?
            .to_owned();
        let path = PathBuf::from(path);
        // Keep the directory private to root while bind/chmod/chown still use
        // paths. A broker account cannot swap a socket for a symlink here.
        let handle = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
            .open(&path)
            .inspect_err(|_| {
                let _ = fs::remove_dir(&path);
            })?;
        Ok(Self {
            path,
            handle,
            sockets: Vec::new(),
        })
    }
    pub fn socket(&mut self, uid: u32) -> PathBuf {
        let name = format!("{uid}.sock");
        self.sockets
            .push(CString::new(name.clone()).expect("numeric UID"));
        self.path.join(name)
    }
    pub fn transfer(&self, uid: u32, gid: u32) -> Result<()> {
        if unsafe { libc::fchown(self.handle.as_raw_fd(), uid, gid) } != 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(())
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        // Even a partial privilege-drop failure must never follow a replaced
        // directory path as root. Unlink only the configured names on our FD.
        for name in &self.sockets {
            unsafe {
                libc::unlinkat(self.handle.as_raw_fd(), name.as_ptr(), 0);
            }
        }
        if let (Ok(fd), Ok(path)) = (self.handle.metadata(), fs::symlink_metadata(&self.path))
            && (fd.dev(), fd.ino()) == (path.dev(), path.ino())
        {
            let _ = fs::remove_dir(&self.path);
        }
    }
}
fn chown(path: &Path, uid: u32, gid: u32) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let name = CString::new(path.as_os_str().as_bytes())?;
    if unsafe { libc::chown(name.as_ptr(), uid, gid) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(())
}

pub fn account_gid(uid: u32) -> Result<u32> {
    // Bootstrap only, before concurrent tasks. Copy the value from libc's
    // static record immediately; no retained pointer crosses another lookup.
    ensure!(uid >= 501, "only ordinary local test accounts are allowed");
    let pw = unsafe { libc::getpwuid(uid) };
    ensure!(!pw.is_null(), "unknown local UID");
    Ok(unsafe { (*pw).pw_gid })
}

pub fn drop_privileges(uid: u32, gid: u32) -> io::Result<()> {
    // These calls are async-signal-safe and also used inside pre_exec.
    if unsafe { libc::setgroups(1, &gid) } != 0
        || unsafe { libc::setgid(gid) } != 0
        || unsafe { libc::setuid(uid) } != 0
    {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::getuid() } != uid || unsafe { libc::geteuid() } != uid {
        return Err(io::Error::other("UID drop failed"));
    }
    Ok(())
}

pub struct Worker {
    child: Child,
    lifetime: Option<UnixStream>,
}
impl Worker {
    pub fn healthy(&mut self)->io::Result<bool> {Ok(self.child.try_wait()?.is_none())}
    pub fn spawn(
        socket: &Path,
        bridge: &Path,
        uid: u32,
        gid: u32,
        broker_uid: u32,
        broker_gid: u32,
    ) -> Result<Self> {
        let listener = UnixListener::bind(socket)?;
        fs::set_permissions(socket, fs::Permissions::from_mode(0o600))?;
        chown(socket, broker_uid, broker_gid)?;
        listener.set_nonblocking(true)?;
        let fd = listener.as_raw_fd();
        let (lifetime, child_lifetime) = UnixStream::pair()?;
        child_lifetime.set_nonblocking(true)?;
        let mut cmd = Command::new(std::env::current_exe()?);
        cmd.arg("--user-relay")
            .arg(bridge)
            .arg(broker_uid.to_string())
            .stdin(Stdio::from(OwnedFd::from(child_lifetime)))
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());
        unsafe {
            cmd.pre_exec(move || {
                if libc::dup2(fd, 3) < 0 || libc::fcntl(3, libc::F_SETFD, 0) < 0 {
                    return Err(io::Error::last_os_error());
                }
                drop_privileges(uid, gid)
            });
        }
        let child = cmd.spawn().context("start fixed-UID relay")?;
        Ok(Self {
            child,
            lifetime: Some(lifetime),
        })
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        // EOF stops workers even if the broker dies. Parent may wait for its
        // children after setuid, but must not need privilege to kill them.
        self.lifetime.take();
        let _ = self.child.wait();
    }
}

fn inherited_unix_fd(fd: libc::c_int, listener: bool) -> Result<()> {
    // The internal mode can also be invoked from a shell, where FD 3 might
    // be absent or already owned by Tokio. Never adopt an arbitrary raw FD.
    let mut addr: libc::sockaddr_storage = unsafe { std::mem::zeroed() };
    let mut size = std::mem::size_of_val(&addr) as libc::socklen_t;
    let ok = unsafe {
        libc::getsockname(
            fd,
            (&mut addr as *mut libc::sockaddr_storage).cast(),
            &mut size,
        )
    };
    ensure!(
        ok == 0 && i32::from(addr.ss_family) == libc::AF_UNIX,
        "missing inherited Unix socket"
    );
    let named = size as usize > std::mem::offset_of!(libc::sockaddr_un, sun_path)
        && unsafe { (*(&raw const addr).cast::<libc::sockaddr_un>()).sun_path[0] } != 0;
    let mut kind: libc::c_int = 0;
    size = std::mem::size_of_val(&kind) as libc::socklen_t;
    let ok = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            (&mut kind as *mut libc::c_int).cast(),
            &mut size,
        )
    };
    ensure!(
        ok == 0 && kind == libc::SOCK_STREAM,
        "expected Unix stream socket"
    );
    // Darwin does not expose SO_ACCEPTCONN on these Unix sockets. A relay
    // listener is named and unconnected; our lifetime socket is a connected pair.
    size = std::mem::size_of_val(&addr) as libc::socklen_t;
    let peer = unsafe {
        libc::getpeername(
            fd,
            (&mut addr as *mut libc::sockaddr_storage).cast(),
            &mut size,
        )
    };
    if listener {
        ensure!(
            named && peer < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::ENOTCONN),
            "expected named, unconnected relay socket"
        );
    } else {
        // During startup rollback the parent may already have closed its
        // unnamed pair. Accept that EOF so the worker can exit normally.
        let closed = peer < 0
            && matches!(
                io::Error::last_os_error().raw_os_error(),
                Some(libc::ENOTCONN | libc::EINVAL)
            );
        ensure!(
            !named && (peer == 0 || closed),
            "expected unnamed lifetime socket"
        );
    }
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    ensure!(
        flags >= 0 && flags & libc::O_NONBLOCK != 0,
        "inherited socket must be nonblocking"
    );
    Ok(())
}

pub async fn run(bridge_path: &Path, broker_uid: u32) -> Result<()> {
    ensure!(
        unsafe { libc::geteuid() } != 0 && broker_uid >= 501,
        "relay must be unprivileged"
    );
    let uid=unsafe {libc::geteuid()};
    if let Some(parent)=bridge_path.parent(){crate::log::initialize(parent.join("relay.log"));}
    inherited_unix_fd(3, true)?;
    inherited_unix_fd(0, false)?;
    let std_listener = unsafe { UnixListener::from_raw_fd(3) };
    let std_lifetime = unsafe { UnixStream::from_raw_fd(0) };
    let listener = tokio::net::UnixListener::from_std(std_listener)?;
    let mut lifetime = tokio::net::UnixStream::from_std(std_lifetime)?;
    println!(
        "{}",
        serde_json::json!({"event":"relay_ready","uid":uid,"pid":std::process::id()})
    );
    let serve = async {
        loop {
            let (mut client, _) = listener.accept().await?;
            if client.peer_cred()?.uid() != broker_uid {
                continue;
            }
            let config=(||->Result<super::bridge::Config>{Ok(serde_json::from_slice(&super::private_file(bridge_path)?)?)})();
            let config=match config {Ok(config)=>config,Err(error)=>{eprintln!("{}",serde_json::json!({"event":"relay_unavailable","operation":"descriptor","error":error.to_string()}));continue;}};
            if config.validate().is_err(){continue;}
            let mut agent=match config.open_agent().await {Ok(agent)=>agent,Err(error)=>{eprintln!("{}",serde_json::json!({"event":"relay_unavailable","operation":"connect","error":error.to_string()}));continue;}};
            let greeting=match super::bridge::checked_greeting(&mut agent,&config).await {Ok(greeting)=>greeting,Err(error)=>{eprintln!("{}",serde_json::json!({"event":"relay_unavailable","operation":"greeting","error":error.to_string()}));continue;}};
            if client.write_all(&greeting).await.is_err(){continue;}
            let (mut cr, mut cw) = client.split();
            let (mut ar, mut aw) = agent.split();
            // EOF in either direction must close both halves immediately.
            tokio::select! {
                _ = tokio::io::copy(&mut cr, &mut aw) => {},
                _ = tokio::io::copy(&mut ar, &mut cw) => {},
            }
            drop(agent);
            drop(client);
            // The GUI agent polls every 10 ms. Give it a chance to retire the
            // closed controller before serving a new connection for this UID.
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        #[allow(unreachable_code)]
        Ok::<(), anyhow::Error>(())
    };
    let mut byte = [0u8; 1];
    tokio::select! { result=serve=>result, _=lifetime.read(&mut byte)=>Ok(()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn relay_does_not_adopt_a_non_listener_as_its_listener() -> Result<()> {
        let (socket, peer) = UnixStream::pair()?;
        socket.set_nonblocking(true)?;
        assert!(inherited_unix_fd(socket.as_raw_fd(), true).is_err());
        inherited_unix_fd(socket.as_raw_fd(), false)?;
        drop(peer);
        inherited_unix_fd(socket.as_raw_fd(), false)?;
        assert!(inherited_unix_fd(-1, false).is_err());
        Ok(())
    }
    #[test]
    fn renamed_runtime_directory_cannot_redirect_cleanup() -> Result<()> {
        let scratch = Directory::create()?;
        let mut runtime = Directory::create()?;
        let original = runtime.path.clone();
        fs::write(runtime.socket(502), b"ours")?;
        let moved = scratch.path.join("moved");
        let target = scratch.path.join("target");
        fs::create_dir(&target)?;
        fs::write(target.join("502.sock"), b"must survive")?;
        fs::rename(&original, &moved)?;
        std::os::unix::fs::symlink(&target, &original)?;
        drop(runtime);
        assert!(!moved.join("502.sock").exists());
        assert_eq!(fs::read(target.join("502.sock"))?, b"must survive");
        fs::remove_file(original)?;
        fs::remove_file(target.join("502.sock"))?;
        fs::remove_dir(target)?;
        fs::remove_dir(moved)?;
        Ok(())
    }
}
