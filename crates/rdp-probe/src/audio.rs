//! Per-authenticated-connection PCM output. Never reads a host audio device.
use anyhow::{Result, ensure};
use ironrdp_rdpsnd::{pdu::{AudioFormat, WaveFormat}, server::{NegotiatedFormat, RdpsndError}};
use ironrdp_server::{RdpsndServerHandler, RdpsndServerMessage, ServerEvent, ServerEventSender, SoundServerFactory};
use std::sync::{Arc, Mutex, atomic::{AtomicU64, Ordering}};
use std::time::{Duration, Instant};
use tokio::sync::mpsc::UnboundedSender;

pub fn pcm_format() -> AudioFormat {
    AudioFormat {format:WaveFormat::PCM,n_channels:2,n_samples_per_sec:48000,
        n_avg_bytes_per_sec:192000,n_block_align:4,bits_per_sample:16,data:None}
}
#[derive(Debug, Default)]
struct State {sender:Option<UnboundedSender<ServerEvent>>,started:bool}
#[derive(Debug)]
pub struct Audio {
    state:Mutex<State>,
    validity:Arc<AtomicU64>,
    queue:Arc<tokio::sync::Semaphore>,
}
impl Default for Audio {fn default()->Self {Self {state:Mutex::default(),validity:Arc::default(),queue:Arc::new(tokio::sync::Semaphore::new(4))}}}
impl Audio {
    pub fn started(&self)->bool {self.state.lock().unwrap().started}
    pub fn invalidate(&self) {self.validity.fetch_add(1,Ordering::AcqRel);}
    pub fn submit(&self, payload:&[u8], generation:u64, lease:u32, enabled:bool)->Result<()> {
        // Header: lease:u32, generation:u64, monotonic_raw_ms:u64, PCM S16LE.
        ensure!(payload.len()>=24 && payload.len()<=19220 && (payload.len()-20).is_multiple_of(4),"invalid audio packet length");
        ensure!(u32::from_be_bytes(payload[0..4].try_into().unwrap())==lease,"wrong audio lease");
        let source_generation=u64::from_be_bytes(payload[4..12].try_into().unwrap());
        let captured=u64::from_be_bytes(payload[12..20].try_into().unwrap());
        let now=monotonic_ms();
        if !enabled || source_generation!=generation || captured>now || now-captured>200 {return Ok(());}
        let state=self.state.lock().unwrap();
        if !state.started {return Ok(());}
        if let Some(sender)=&state.sender {
            // No unbounded sound backlog behind a slow network writer.
            let Ok(permit)=self.queue.clone().try_acquire_owned() else {return Ok(());};
            let age=now-captured;
            let _=sender.send(ServerEvent::RdpsndGuarded {
                message:RdpsndServerMessage::Wave(payload[20..].to_vec(),captured as u32),
                validity:self.validity.clone(),generation:self.validity.load(Ordering::Acquire),
                expires:Instant::now()+Duration::from_millis(200-age), _permit:permit,
            });
        }
        Ok(())
    }
}
pub fn monotonic_ms()->u64 {
    let mut ts:libc::timespec=unsafe {std::mem::zeroed()};
    if unsafe {libc::clock_gettime(libc::CLOCK_MONOTONIC_RAW,&mut ts)}!=0 {return 0;}
    ts.tv_sec as u64*1000+ts.tv_nsec as u64/1_000_000
}
pub struct Factory(pub Arc<Audio>);
impl ServerEventSender for Factory {
    fn set_sender(&mut self,sender:UnboundedSender<ServerEvent>) {self.0.state.lock().unwrap().sender=Some(sender);}
}
impl SoundServerFactory for Factory {
    fn build_backend(&self)->Box<dyn RdpsndServerHandler> {Box::new(Backend {audio:self.0.clone(),formats:vec![pcm_format()]})}
}
#[derive(Debug)]
struct Backend {audio:Arc<Audio>,formats:Vec<AudioFormat>}
impl RdpsndServerHandler for Backend {
    fn get_formats(&self)->&[AudioFormat] {&self.formats}
    fn choose_format<'a>(&mut self,common:&'a [NegotiatedFormat])->Option<&'a NegotiatedFormat> {
        common.iter().find(|f| f.format()==&pcm_format())
    }
    fn start(&mut self,_:&NegotiatedFormat)->Result<(),Box<dyn RdpsndError>> {
        self.audio.state.lock().unwrap().started=true;
        eprintln!("{{\"event\":\"audio_negotiated\",\"rate\":48000,\"channels\":2,\"bits\":16}}");
        Ok(())
    }
    fn stop(&mut self) {self.audio.invalidate();self.audio.state.lock().unwrap().started=false;}
}
impl Drop for Backend {fn drop(&mut self) {self.stop();}}

#[cfg(test)]
mod tests {
    use super::*;
    fn packet(generation:u64,lease:u32)->Vec<u8> {
        [lease.to_be_bytes().as_slice(),generation.to_be_bytes().as_slice(),monotonic_ms().to_be_bytes().as_slice(),&[1,0,2,0]].concat()
    }
    #[test]
    fn isolation_freshness_and_bounded_queue() {
        let a=Audio::default(); let b=Audio::default();
        let (tx,mut rx)=tokio::sync::mpsc::unbounded_channel();
        *a.state.lock().unwrap()=State {sender:Some(tx),started:true};
        let (other,mut other_rx)=tokio::sync::mpsc::unbounded_channel();
        *b.state.lock().unwrap()=State {sender:Some(other),started:true};
        let p=packet(3,42);
        assert!(a.submit(&p,3,43,true).is_err());
        a.submit(&p,2,42,true).unwrap();a.submit(&p,3,42,false).unwrap();
        assert!(rx.try_recv().is_err());
        for _ in 0..20 {a.submit(&p,3,42,true).unwrap();}
        assert_eq!(rx.len(),4);assert!(other_rx.try_recv().is_err());
        let ServerEvent::RdpsndGuarded {validity,generation,..}=rx.try_recv().unwrap() else {panic!()};
        a.invalidate();assert_ne!(validity.load(Ordering::Acquire),generation);
        assert!(a.submit(&[0;23],3,42,true).is_err());
    }
    #[test]
    fn stale_and_future_pcm_are_discarded() {
        let a=Audio::default();let (tx,rx)=tokio::sync::mpsc::unbounded_channel();
        *a.state.lock().unwrap()=State {sender:Some(tx),started:true};
        for stamp in [monotonic_ms()-1000,monotonic_ms()+1000] {
            let mut p=packet(1,2);p[12..20].copy_from_slice(&stamp.to_be_bytes());a.submit(&p,1,2,true).unwrap();
        }
        assert_eq!(rx.len(),0);
    }
}
