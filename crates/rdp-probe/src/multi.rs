//! TLS RDP entry points with per-connection state and fixed UID routes.
use super::{Secret, bridge, equal_secret, load_tls, private_file, private_file_owned, relay};
use anyhow::{Context, Result, ensure};
use ironrdp_server::{
    CredentialDecision, CredentialValidationError, CredentialValidator, Credentials, DesktopSize,
    DisplayUpdate, KeyboardEvent, MouseEvent, RdpServer, RdpServerDisplay, RdpServerDisplayUpdates,
    RdpServerInputHandler, tokio_rustls::TlsAcceptor,
};
use serde::Deserialize;
use serde_json::json;
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    net::TcpListener,
    sync::{OwnedSemaphorePermit, Semaphore, watch},
    task::JoinSet,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Configuration {
    #[serde(default = "default_port")]
    port: u16,
    #[serde(default)]
    lan_port: Option<u16>,
    #[serde(default)]
    local_auth: bool,
    broker_uid: u32,
    certificate: PathBuf,
    private_key: PathBuf,
    users: Vec<User>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct User {
    uid: u32,
    credentials: Secret,
    bridge: PathBuf,
    #[serde(default)]
    service: bool,
    #[serde(default="service_width")]
    width:u16,
    #[serde(default="service_height")]
    height:u16,
}
fn service_width()->u16 {1920}
fn service_height()->u16 {1200}
fn valid_login_name(name:&str, local:bool)->bool {
    !name.is_empty() && if local {
        name.len()<=1024 && !name.chars().any(|c|c.is_control() || c=='/' || c=='\\')
    } else {name.len()<=64 && name.bytes().all(|c|c.is_ascii_alphanumeric() || b"_-".contains(&c))}
}
struct Route {
    dynamic:bool,
    credentials: Secret,
    bridge: bridge::Config,
    socket: PathBuf,
    controller: Arc<Semaphore>,
    latest: Mutex<Weak<Connection>>,
}
struct Routes {
    users: Vec<Arc<Route>>,
    size: DesktopSize,
    local_auth: bool,
}
struct Lease {
    route: Arc<Route>,
    _permit: OwnedSemaphorePermit,
}
impl Routes {
    fn authenticate(&self, credentials: &Credentials) -> Option<Arc<Route>> {
        if (!self.local_auth && credentials.domain.as_ref().is_some_and(|v| !v.is_empty() && v != "."))
            || credentials.password.len() > 1024
        {
            return None;
        }
        // Windows may supply .\user or a client domain. This service always
        // verifies the canonical account in /Local/Default, never a domain.
        let username = if self.local_auth { credentials.username.rsplit('\\').next()? } else { credentials.username.as_str() };
        let route = self
            .users
            .iter()
            .find(|r| r.credentials.username.eq_ignore_ascii_case(username))?;
        if !self.local_auth && !equal_secret(
            route.credentials.password.as_bytes(),
            credentials.password.as_bytes(),
        ) {
            return None;
        }
        Some(route.clone())
    }
    async fn claim(
        &self,
        credentials: &Credentials,
        connection: &Arc<Connection>,
    ) -> Option<Lease> {
        // Check the password first: an invalid attempt must not affect a live desktop.
        let route = self.authenticate(credentials)?;
        if self.local_auth && !crate::local_auth::verify(route.bridge.uid(), &route.credentials.username, &credentials.password).await {
            return None;
        }
        {
            let mut latest = route.latest.lock().unwrap();
            if let Some(old) = latest.upgrade() {
                old.revoked.send_replace(true);
                println!(
                    "{}",
                    json!({"event":"multi_takeover","uid":route.bridge.uid(),
                    "previous_connection":old.id,"new_connection":connection.id})
                );
            }
            *latest = Arc::downgrade(connection);
        }
        // Wait for the old transport and its input/GUI bridge to drop. Another
        // login can supersede this one while it is still waiting for the permit.
        let mut revoked = connection.revoked.subscribe();
        let permit = tokio::select! {
            biased;
            _ = revoked.wait_for(|&stop| stop) => return None,
            result = route.controller.clone().acquire_owned() => result.ok()?,
        };
        if *connection.revoked.borrow() {
            return None;
        }
        Some(Lease {
            route,
            _permit: permit,
        })
    }
}

fn default_port() -> u16 {
    3390
}

fn allowed_peer(address: std::net::IpAddr) -> bool {
    match address {
        std::net::IpAddr::V4(ip) => ip.is_loopback() || ip.is_private() || ip.is_link_local() || u32::from(ip) & 0xffc00000 == 0x64400000,
        std::net::IpAddr::V6(ip) => ip.is_loopback(),
    }
}

pub struct Runtime {
    port: u16,
    lan_port: Option<u16>,
    // Drop workers before unlinking the directory. stdin EOF is the lifetime
    // signal; the network process cannot kill a different UID after dropping.
    _workers: Vec<relay::Worker>,
    _directory: relay::Directory,
    routes: Arc<Routes>,
    tls: TlsAcceptor,
}

pub fn bootstrap(path: &Path) -> Result<Runtime> {
    ensure!(
        unsafe { libc::getuid() } == 0 && unsafe { libc::geteuid() } == 0,
        "--multi requires root bootstrap; network handling will drop to broker_uid"
    );
    let bytes = zeroize::Zeroizing::new(private_file(path)?);
    let config: Configuration =
        serde_json::from_slice(&bytes).context("invalid multi-user configuration")?;
    ensure!(
        !config.users.is_empty() && config.users.len() <= 128,
        "limit: 1..128 local accounts"
    );
    ensure!(
        config.certificate.is_absolute() && config.private_key.is_absolute(),
        "TLS paths must be absolute"
    );
    ensure!(
        config.port >= 1024,
        "only unprivileged loopback ports are supported"
    );
    if let Some(port) = config.lan_port {
        ensure!(port >= 1024 && port != config.port, "invalid LAN port");
    }
    let broker_gid = relay::account_gid(config.broker_uid)?;
    let tls = load_tls(&config.certificate, &config.private_key)?;
    let mut names = HashSet::new();
    let mut uids = HashSet::new();
    // Validate all configuration before launching anything or dropping UID.
    let mut pending = Vec::new();
    let mut size = None;
    for user in config.users {
        ensure!(
            user.uid != config.broker_uid && uids.insert(user.uid),
            "duplicate or broker UID in routes"
        );
        let gid = relay::account_gid(user.uid)?;
        ensure!(
            valid_login_name(&user.credentials.username,config.local_auth)
                && names.insert(user.credentials.username.clone()),
            "invalid or duplicate login name"
        );
        ensure!(
            config.local_auth || (16..=1024).contains(&user.credentials.password.len()),
            "invalid test password length"
        );
        ensure!(user.bridge.is_absolute(), "bridge path must be absolute");
        let bridge: bridge::Config = if user.service {
            ensure!(user.bridge.ends_with("Library/Application Support/ZMacRDS/Service/bridge.json"),"service descriptor must use the fixed per-user path");
            bridge::Config::service(user.uid,user.width,user.height)
        } else {serde_json::from_slice(&private_file_owned(&user.bridge, user.uid)?)?};
        bridge.validate_for_uid(user.uid)?;
        let geometry = bridge.size();
        if let Some((width, height)) = size {
            ensure!(
                (geometry.width, geometry.height) == (width, height),
                "this experiment requires equal desktop geometry across users"
            );
        } else {
            size = Some((geometry.width, geometry.height));
        }
        pending.push((user, gid, bridge));
    }
    let mut directory = relay::Directory::create()?;
    let mut workers = Vec::new();
    let mut users = Vec::new();
    for (user, gid, bridge) in pending {
        let socket = directory.socket(user.uid);
        workers.push(relay::Worker::spawn(
            &socket,
            &user.bridge,
            user.uid,
            gid,
            config.broker_uid,
            broker_gid,
        )?);
        users.push(Arc::new(Route {
            dynamic:user.service,
            credentials: user.credentials,
            bridge,
            socket,
            controller: Arc::new(Semaphore::new(1)),
            latest: Mutex::new(Weak::new()),
        }));
    }
    // No privileged path operations after the broker can modify the directory.
    directory.transfer(config.broker_uid, broker_gid)?;
    relay::drop_privileges(config.broker_uid, broker_gid)?;
    // setuid from root must also discard saved-root privileges.
    ensure!(
        unsafe { libc::setuid(0) } == -1 && unsafe { libc::geteuid() } == config.broker_uid,
        "broker retained root privilege"
    );
    let (width, height) = size.unwrap();
    Ok(Runtime {
        port: config.port,
        lan_port: config.lan_port,
        _workers: workers,
        _directory: directory,
        routes: Arc::new(Routes {
            users,
            size: DesktopSize { width, height },
            local_auth: config.local_auth,
        }),
        tls,
    })
}

#[derive(Default)]
struct Counters {
    frames: AtomicU64,
    keyboard: AtomicU64,
    mouse: AtomicU64,
}
struct Connection {
    id: u64,
    lease: Mutex<Option<Lease>>,
    input: Arc<Mutex<bridge::Input>>,
    ready: watch::Sender<bool>,
    revoked: watch::Sender<bool>,
    counters: Arc<Counters>,
}
impl Connection {
    fn new(id: u64) -> Arc<Self> {
        let (ready, _) = watch::channel(false);
        Arc::new(Self {
            id,
            lease: Mutex::new(None),
            input: Arc::default(),
            ready,
            revoked: watch::channel(false).0,
            counters: Arc::default(),
        })
    }
    fn route(&self) -> Result<Arc<Route>> {
        ensure!(!*self.revoked.borrow(), "connection superseded");
        self.lease
            .lock()
            .unwrap()
            .as_ref()
            .map(|l| l.route.clone())
            .context("connection is not authenticated")
    }
}
struct Guard {
    connection: Arc<Connection>,
    started: Instant,
}
impl Drop for Guard {
    fn drop(&mut self) {
        self.connection.input.lock().unwrap().close();
        let lease = self.connection.lease.lock().unwrap().take();
        println!(
            "{}",
            json!({"event":"multi_disconnected","connection":self.connection.id,
            "uid":lease.as_ref().map(|l|l.route.bridge.uid()),"duration_ms":self.started.elapsed().as_millis(),
            "frames":self.connection.counters.frames.load(Ordering::Relaxed),
            "keyboard_events":self.connection.counters.keyboard.load(Ordering::Relaxed),
            "mouse_events":self.connection.counters.mouse.load(Ordering::Relaxed)})
        );
        // The permit drops only for this UID. No global authentication reset.
    }
}
struct Auth {
    routes: Arc<Routes>,
    connection: Arc<Connection>,
}
#[async_trait::async_trait]
impl CredentialValidator for Auth {
    async fn validate(
        &self,
        credentials: &Credentials,
    ) -> Result<CredentialDecision, CredentialValidationError> {
        // Reactivation or repeated validation may not replace an assigned UID.
        if self.connection.lease.lock().unwrap().is_some() {
            return Ok(CredentialDecision::Reject);
        }
        let lease = self.routes.claim(credentials, &self.connection).await;
        let accepted = lease.is_some();
        println!(
            "{}",
            json!({"event":"multi_authentication","connection":self.connection.id,
            "accepted":accepted,"uid":lease.as_ref().map(|l|l.route.bridge.uid())})
        );
        *self.connection.lease.lock().unwrap() = lease;
        Ok(if accepted {
            CredentialDecision::Accept
        } else {
            CredentialDecision::Reject
        })
    }
}
#[derive(Clone)]
struct Handler {
    routes: Arc<Routes>,
    connection: Arc<Connection>,
}
impl RdpServerInputHandler for Handler {
    fn keyboard(&mut self, event: KeyboardEvent) {
        if self.connection.route().is_err() {
            return;
        }
        self.connection
            .counters
            .keyboard
            .fetch_add(1, Ordering::Relaxed);
        self.connection.input.lock().unwrap().keyboard(event);
    }
    fn mouse(&mut self, event: MouseEvent) {
        if self.connection.route().is_err() {
            return;
        }
        self.connection
            .counters
            .mouse
            .fetch_add(1, Ordering::Relaxed);
        self.connection.input.lock().unwrap().mouse(event);
    }
}
struct Updates {
    inner: bridge::Updates,
    counters: Arc<Counters>,
}
#[async_trait::async_trait]
impl RdpServerDisplayUpdates for Updates {
    async fn next_update(&mut self) -> Result<Option<DisplayUpdate>> {
        let update = self.inner.next_update().await?;
        if update.is_some() {
            self.counters.frames.fetch_add(1, Ordering::Relaxed);
        }
        Ok(update)
    }
}
#[async_trait::async_trait]
impl RdpServerDisplay for Handler {
    async fn size(&mut self) -> DesktopSize {
        self.routes.size
    }
    async fn updates(&mut self) -> Result<Box<dyn RdpServerDisplayUpdates>> {
        // Independent gate: IronRDP may omit validator when ClientInfo has no
        // credentials. Such connections never open a relay or see a frame.
        let route = self.connection.route()?;
        let inner = bridge::connect_relay(
            route.bridge.clone(),
            self.connection.input.clone(),
            &route.socket,
            route.dynamic,
        )
        .await?;
        println!(
            "{}",
            json!({"event":"multi_display_started","connection":self.connection.id,"uid":route.bridge.uid(),"dynamic":route.dynamic})
        );
        self.connection.ready.send_replace(true);
        Ok(Box::new(Updates {
            inner,
            counters: self.connection.counters.clone(),
        }))
    }
}

impl Runtime {
    pub async fn run(mut self) -> Result<()> {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        let listener = TcpListener::bind(("127.0.0.1", self.port)).await?;
        let lan = match self.lan_port {
            Some(port) => Some(TcpListener::bind(("0.0.0.0", port)).await?),
            None => None,
        };
        println!(
            "{}",
            json!({"event":"multi_starting","address":listener.local_addr()?.to_string(),"lan_port":self.lan_port,"local_auth":self.routes.local_auth,"uid":unsafe{libc::geteuid()},"users":self.routes.users.len(),"security":"TLS","max_connections":16})
        );
        tokio::task::LocalSet::new().run_until(async {
            let mut tasks=JoinSet::new();let mut next=0u64;
            let mut health=tokio::time::interval(Duration::from_secs(2));
            loop {
                tokio::select! {
                    _=tokio::signal::ctrl_c()=>break,
                    _=terminate.recv()=>break,
                    _=health.tick()=>{for worker in &mut self._workers {ensure!(worker.healthy()?,"fixed-UID relay exited; restarting service is required");}},
                    Some(result)=tasks.join_next(),if !tasks.is_empty()=>{if let Err(error)=result {eprintln!("connection task: {error}");}},
                    accepted=async {
                        match &lan {
                            Some(lan) => tokio::select! { result=listener.accept()=>result, result=lan.accept()=>result },
                            None => listener.accept().await,
                        }
                    }=>{
                        let(stream,peer)=accepted?;
                        if !allowed_peer(peer.ip()) || tasks.len()>=16 {drop(stream);continue;}
                        next+=1;let connection=Connection::new(next);
                        let routes=self.routes.clone();let tls=self.tls.clone();let port=self.port;
                        tasks.spawn_local(async move {
                            let _guard=Guard{connection:connection.clone(),started:Instant::now()};
                            let handler=Handler{routes:routes.clone(),connection:connection.clone()};
                            let auth=Auth{routes,connection:connection.clone()};
                            let mut server=RdpServer::builder().with_addr(([127,0,0,1],port)).with_tls(tls)
                                .with_input_handler(handler.clone()).with_display_handler(handler)
                                .with_bitmap_codecs(crate::codecs::capabilities())
                                .with_desktop_resize_support(false)
                                .with_sound_factory(Some(Box::new(crate::audio::Factory(connection.input.lock().unwrap().audio.clone()))))
                                .with_credential_validator(Some(Arc::new(auth))).with_max_request_size(1024*1024).build();
                            let mut ready=connection.ready.subscribe();
                            let mut revoked=connection.revoked.subscribe();
                            let result=tokio::select! {
                              biased;
                              _=revoked.wait_for(|&stop|stop)=>Ok(()),
                              result=async {
                                let run=server.run_connection(stream);tokio::pin!(run);
                                let ready_before_deadline=tokio::time::timeout(Duration::from_secs(120),async {
                                    while !*ready.borrow_and_update(){ready.changed().await?;}
                                    Ok::<(),anyhow::Error>(())
                                });
                                tokio::select! {
                                    result=&mut run=>result,
                                    status=ready_before_deadline=>match status {
                                        Ok(Ok(()))=>run.await,
                                        _=>Err(anyhow::anyhow!("connection setup deadline exceeded")),
                                    }
                                }
                              }=>result
                            };
                            println!("{}",json!({"event":"multi_transport_ended","connection":connection.id,"error":result.is_err()}));
                            // Drop protocol handlers/updates before Guard releases the UID.
                            drop(server);
                        });
                    }
                }
            }
            tasks.abort_all();while tasks.join_next().await.is_some(){}
            Ok::<(),anyhow::Error>(())
        }).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn local_names_allow_unicode_but_not_protocol_injection() {
        assert!(valid_login_name("测试用户",true));assert!(!valid_login_name("测试用户",false));
        for name in ["", "a\nb", "DOMAIN\\name", "a/b"] {assert!(!valid_login_name(name,true));}
    }
    #[test]
    fn direct_entry_accepts_private_networks_but_rejects_public_and_multicast() {
        for address in ["127.0.0.1", "10.1.2.3", "172.16.2.1", "192.168.64.1", "169.254.2.1", "100.64.0.1", "100.127.255.254", "::1"] {
            assert!(allowed_peer(address.parse().unwrap()), "{address}");
        }
        for address in ["8.8.8.8", "172.32.0.1", "100.128.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255", "2001:4860:4860::8888"] {
            assert!(!allowed_peer(address.parse().unwrap()), "{address}");
        }
    }
    #[tokio::test]
    async fn failed_system_authentication_cannot_take_over_a_valid_lease() {
        let mut routes=routes();
        let current=Connection::new(1);
        let lease=routes.claim(&credentials(502), &current).await.unwrap();
        // Switch only the verifier, preserving this user's live controller.
        Arc::get_mut(&mut routes).unwrap().local_auth=true;
        let attempt=Connection::new(2);
        let mut login=credentials(502);login.password.clear();
        login.username=".\\USER502".into();login.domain=Some("WORKGROUP".into());
        assert!(routes.claim(&login,&attempt).await.is_none());
        assert!(!*current.revoked.borrow());drop(lease);
    }
    fn routes() -> Arc<Routes> {
        Arc::new(Routes{local_auth:false,size:DesktopSize{width:1920,height:1200},users:[502,503].into_iter().map(|uid| {
            let bridge=serde_json::from_value(json!({"uid":uid,"audit_session_id":uid+10,"nonce":uid+20,"epoch":1,"boot_id":"test","socket":"/tmp/zmacrds-m2-test/agent.sock","width":1920,"height":1200,"schema":1})).unwrap();
            Arc::new(Route{dynamic:false,credentials:Secret{username:format!("user{uid}"),password:format!("test-password-for-{uid}")},bridge,socket:PathBuf::from("/not-connected-in-unit-test"),controller:Arc::new(Semaphore::new(1)),latest:Mutex::new(Weak::new())})
        }).collect()})
    }
    fn credentials(uid: u32) -> Credentials {
        Credentials {
            username: format!("user{uid}"),
            password: format!("test-password-for-{uid}"),
            domain: None,
        }
    }
    #[tokio::test]
    async fn takeover_waits_for_its_old_controller_and_leaves_other_user_connected() {
        let routes = routes();
        let a = Connection::new(1);
        let b = Connection::new(2);
        let duplicate = Connection::new(3);
        for (c, uid) in [(&a, 502), (&b, 503)] {
            assert_eq!(
                Auth {
                    routes: routes.clone(),
                    connection: c.clone()
                }
                .validate(&credentials(uid))
                .await
                .unwrap(),
                CredentialDecision::Accept
            );
            assert_eq!(c.route().unwrap().bridge.uid(), uid);
        }
        let mut wrong = credentials(502);
        wrong.password = credentials(503).password;
        assert!(routes.claim(&wrong, &duplicate).await.is_none());
        assert!(!*a.revoked.borrow());
        let auth = Auth {
            routes: routes.clone(),
            connection: duplicate.clone(),
        };
        let owner = credentials(502);
        let mut takeover = Box::pin(auth.validate(&owner));
        tokio::select! {
            result=&mut takeover=>panic!("takeover before old guard dropped: {result:?}"),
            _=tokio::task::yield_now()=>{}
        }
        assert!(*a.revoked.borrow());
        assert!(!*b.revoked.borrow());
        assert!(a.route().is_err());
        drop(Guard {
            connection: a,
            started: Instant::now(),
        });
        assert_eq!(takeover.await.unwrap(), CredentialDecision::Accept);
        assert_eq!(duplicate.route().unwrap().bridge.uid(), 502);
        assert_eq!(b.route().unwrap().bridge.uid(), 503);
    }
    #[tokio::test]
    async fn newest_valid_login_supersedes_even_a_pending_replacement() {
        let routes = routes();
        let first = Connection::new(10);
        let second = Connection::new(11);
        let third = Connection::new(12);
        let login = credentials(502);
        let first_lease = routes.claim(&login, &first).await.unwrap();
        let mut second_login = Box::pin(routes.claim(&login, &second));
        tokio::select! {
            result=&mut second_login=>panic!("second acquired while first still held: {}",result.is_some()),
            _=tokio::task::yield_now()=>{}
        }
        assert!(*first.revoked.borrow());
        let mut third_login = Box::pin(routes.claim(&login, &third));
        tokio::select! {
            result=&mut third_login=>panic!("third acquired before first dropped: {}",result.is_some()),
            _=tokio::task::yield_now()=>{}
        }
        assert!(second_login.await.is_none());
        drop(first_lease);
        let newest = third_login.await.unwrap();
        assert_eq!(newest.route.bridge.uid(), 502);
        assert!(!*third.revoked.borrow());
    }

    #[tokio::test]
    async fn missing_wrong_and_cross_user_credentials_do_not_expose_desktop() {
        let routes = routes();
        let c = Connection::new(1);
        let mut handler = Handler {
            routes: routes.clone(),
            connection: c.clone(),
        };
        handler.keyboard(KeyboardEvent::UnicodePressed(65));
        handler.mouse(MouseEvent::LeftPressed);
        assert_eq!(c.counters.keyboard.load(Ordering::Relaxed), 0);
        assert!(handler.updates().await.is_err());
        let auth = Auth {
            routes: routes.clone(),
            connection: c,
        };
        let mut wrong = credentials(502);
        wrong.password = credentials(503).password;
        assert_eq!(
            auth.validate(&wrong).await.unwrap(),
            CredentialDecision::Reject
        );
        wrong = credentials(502);
        wrong.domain = Some("unrecognized".into());
        assert_eq!(
            auth.validate(&wrong).await.unwrap(),
            CredentialDecision::Reject
        );
        assert!(routes.authenticate(&credentials(502)).is_some());
        assert!(routes.authenticate(&credentials(503)).is_some());
    }
    #[tokio::test]
    async fn assigned_connection_cannot_switch_identity_or_release_other_user() {
        let routes = routes();
        let c = Connection::new(1);
        let auth = Auth {
            routes: routes.clone(),
            connection: c.clone(),
        };
        assert_eq!(
            auth.validate(&credentials(502)).await.unwrap(),
            CredentialDecision::Accept
        );
        assert_eq!(
            auth.validate(&credentials(503)).await.unwrap(),
            CredentialDecision::Reject
        );
        assert_eq!(c.route().unwrap().bridge.uid(), 502);
        assert!(routes.authenticate(&credentials(503)).is_some());
    }
}
