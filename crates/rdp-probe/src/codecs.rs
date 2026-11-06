//! One advertisement policy for both experimental RDP entry points.
use ironrdp_pdu::rdp::capability_sets::{
    BitmapCodecs, Codec, CodecProperty, NsCodec, server_codecs_capabilities,
};

pub fn capabilities() -> BitmapCodecs {
    let experimental=std::env::var("ZMACRDS_EXPERIMENTAL_NSCODEC").is_ok_and(|value|value=="1");
    policy(experimental)
}

fn policy(experimental:bool)->BitmapCodecs {
    let mut codecs = server_codecs_capabilities(&[]).expect("built-in codec configuration");
    if experimental {codecs.0.push(Codec {
        id: 0, // Client assigns the codec ID in Confirm Active.
        property: CodecProperty::NsCodec(NsCodec {
            is_dynamic_fidelity_allowed: false,
            is_subsampling_allowed: false,
            color_loss_level: 1,
        }),
    });}
    codecs
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_compatibility_and_experimental_policies_are_explicit() {
        let codecs = policy(false);
        assert!(!codecs.0.iter().any(|c| matches!(c.property, CodecProperty::NsCodec(_))));
        assert!(codecs.0.iter().any(|c| matches!(c.property, CodecProperty::RemoteFx(_))));
        let encoded = ironrdp_pdu::encode_vec(&codecs).unwrap();
        assert!(!encoded.is_empty());
        assert!(policy(true).0.iter().any(|c|matches!(c.property,CodecProperty::NsCodec(_))));
    }
}
