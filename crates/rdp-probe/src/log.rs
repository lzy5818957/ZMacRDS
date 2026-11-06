//! Optional bounded service logs. Initialization/writes happen unprivileged.
use std::{fs::{self,OpenOptions},io::Write,os::unix::fs::{MetadataExt,OpenOptionsExt,PermissionsExt},path::Path,sync::Mutex};
static PATH:std::sync::OnceLock<std::path::PathBuf>=std::sync::OnceLock::new();
pub fn initialize(path:std::path::PathBuf) {let _=PATH.set(path);}
static LOCK:Mutex<()>=Mutex::new(());
pub fn line(line:&str,error:bool) {
    let Some(path)=PATH.get().cloned().or_else(||std::env::var_os("ZMACRDS_LOG_PATH").map(std::path::PathBuf::from)) else {if error {std::eprintln!("{line}");} else {std::println!("{line}");} return;};
    let _lock=LOCK.lock().unwrap_or_else(|e|e.into_inner());
    if write(&path,line).is_err(){std::eprintln!("service log unavailable");}
}
fn write(path:&Path,line:&str)->std::io::Result<()> {
    let uid=unsafe {libc::geteuid()};
    let parent=path.parent().ok_or_else(||std::io::Error::other("missing log directory"))?;
    let meta=fs::symlink_metadata(parent)?;
    if uid==0 || !meta.is_dir() || meta.uid()!=uid || meta.permissions().mode()&0o077!=0 {return Err(std::io::Error::other("unsafe log directory"));}
    if let Ok(meta)=fs::symlink_metadata(path) {
        if !meta.is_file() || meta.uid()!=uid || meta.permissions().mode()&0o077!=0 {return Err(std::io::Error::other("unsafe log"));}
        if meta.len()>=4*1024*1024 {
            let _=fs::remove_file(path.with_extension("log.2"));
            let _=fs::rename(path.with_extension("log.1"),path.with_extension("log.2"));
            fs::rename(path,path.with_extension("log.1"))?;
        }
    }
    let mut file=OpenOptions::new().append(true).create(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(path)?;
    writeln!(file,"{line}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rotates_bounded_logs_and_rejects_symlinks() {
        let path=std::env::temp_dir().join(format!("zmacrds-log-test-{}-{}",std::process::id(),crate::audio::monotonic_ms()));
        fs::create_dir(&path).unwrap();fs::set_permissions(&path,fs::Permissions::from_mode(0o700)).unwrap();
        let log=path.join("server.log");
        write(&log,"first").unwrap();
        OpenOptions::new().write(true).open(&log).unwrap().set_len(4*1024*1024).unwrap();
        write(&log,"next").unwrap();
        assert_eq!(fs::read_to_string(&log).unwrap(),"next\n");
        assert_eq!(fs::metadata(path.join("server.log.1")).unwrap().len(),4*1024*1024);
        let alias=path.join("alias.log");std::os::unix::fs::symlink(&log,&alias).unwrap();
        assert!(write(&alias,"wrong").is_err());assert_eq!(fs::read_to_string(&log).unwrap(),"next\n");
        fs::remove_file(alias).unwrap();fs::remove_file(log).unwrap();fs::remove_file(path.join("server.log.1")).unwrap();fs::remove_dir(path).unwrap();
    }
}
