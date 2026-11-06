//! Measures only known synthetic tones; does not save or play received audio.
use ironrdp_rdpsnd::{client::RdpsndClientHandler,pdu::{AudioFormat,WaveFormat,VolumePdu,PitchPdu}};
use std::{borrow::Cow,sync::{Arc,Mutex}};
#[derive(Debug,Default)]
pub struct Stats {pub received:u64,pub waves:u64,pub frames:u64,pub own:f64,pub foreign:f64,pub invalid:bool}
#[derive(Debug)]
pub struct Backend {pub stats:Arc<Mutex<Stats>>,pub tones:[u16;2],pub formats:Vec<AudioFormat>}
impl Backend {
    pub fn new(stats:Arc<Mutex<Stats>>,tones:[u16;2])->Self {
        Self {stats,tones,formats:vec![AudioFormat {format:WaveFormat::PCM,n_channels:2,n_samples_per_sec:48000,
            n_avg_bytes_per_sec:192000,n_block_align:4,bits_per_sample:16,data:None}]}
    }
}
fn power(samples:&[f64],frequency:f64)->f64 {
    let mut real=0.;let mut imag=0.;let mut weights=0.;
    for (i,&sample) in samples.iter().enumerate() {
        let weight=0.5-0.5*(2.*std::f64::consts::PI*i as f64/(samples.len()-1) as f64).cos();
        let angle=2.*std::f64::consts::PI*frequency*i as f64/48000.;
        real+=sample*weight*angle.cos();imag+=sample*weight*angle.sin();weights+=weight;
    }
    (real*real+imag*imag)/(weights*weights)
}
impl RdpsndClientHandler for Backend {
    fn get_formats(&self)->&[AudioFormat] {&self.formats}
    fn wave(&mut self,format_no:usize,_:u32,data:Cow<'_,[u8]>) {
        let mut stats=self.stats.lock().unwrap();stats.received+=1;
        if format_no!=0 || !data.len().is_multiple_of(4) || data.len()>19200 {stats.invalid=true;return;}
        let frames=data.len()/4;
        if frames<256 {return;}
        stats.waves+=1;stats.frames+=frames as u64;
        // Measure each channel separately. Mixing before measurement could
        // cancel opposite-phase leakage; checking only left misses right-only audio.
        for offset in [0,2] {
            let samples:Vec<f64>=data.as_chunks::<4>().0.iter().map(|b|i16::from_le_bytes([b[offset],b[offset+1]]) as f64/32768.).collect();
            stats.own+=power(&samples,f64::from(self.tones[0]))/2.;
            stats.foreign+=power(&samples,f64::from(self.tones[1]))/2.;
        }
    }
    fn set_volume(&mut self,_:VolumePdu) {}
    fn set_pitch(&mut self,_:PitchPdu) {}
    fn close(&mut self) {}
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn detects_a_foreign_tone_instead_of_just_counting_audio_packets() {
        let samples:Vec<f64>=(0..1024).map(|i|0.1*(2.*std::f64::consts::PI*440.*i as f64/48000.).sin()).collect();
        assert!(power(&samples,440.)/power(&samples,880.)>10000.);
        let mixed:Vec<f64>=samples.iter().enumerate().map(|(i,x)|x+0.05*(2.*std::f64::consts::PI*880.*i as f64/48000.).sin()).collect();
        assert!(power(&mixed,440.)/power(&mixed,880.)<10.);
    }
    #[test]
    fn detects_right_only_and_opposite_phase_foreign_audio() {
        for opposite_phase in [false,true] {
            let stats=Arc::new(Mutex::new(Stats::default()));
            let mut backend=Backend::new(stats.clone(),[440,880]);
            let mut pcm=Vec::new();
            for i in 0..1024 {
                let angle=2.*std::f64::consts::PI*i as f64/48000.;
                let own=0.1*(440.*angle).sin();let foreign=0.05*(880.*angle).sin();
                for sample in [own+if opposite_phase {-foreign} else {0.},own+foreign] {
                    pcm.extend_from_slice(&((sample*32768.) as i16).to_le_bytes());
                }
            }
            backend.wave(0,0,Cow::Owned(pcm));
            let result=stats.lock().unwrap();
            assert_eq!(result.frames,1024);assert!(result.foreign>0.0001);
            assert!(result.own/result.foreign<10.);
        }
    }
}
