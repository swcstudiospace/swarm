use std::fmt;

/// A message carried over a Nexus topic, tagged for request/response correlation.
#[derive(Clone, PartialEq, Eq)]
pub struct NexusMessage {
    /// Topic the message is published on.
    pub topic: String,
    /// Identifier linking a response to the request that produced it.
    pub correlation_id: String,
    /// Opaque message body.
    pub payload: Vec<u8>,
}

impl fmt::Debug for NexusMessage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("NexusMessage")
            .field("topic", &self.topic)
            .field("correlation_id", &self.correlation_id)
            .field("payload_len", &self.payload.len())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_omits_payload_bytes() {
        let msg = NexusMessage {
            topic: "agent.task".to_owned(),
            correlation_id: "corr-1".to_owned(),
            payload: b"SECRET".to_vec(),
        };
        let text = format!("{msg:?}");
        assert!(text.contains("agent.task"), "{text}");
        assert!(text.contains("corr-1"), "{text}");
        assert!(text.contains("payload_len: 6"), "{text}");
        assert!(
            !text.contains("[83"),
            "payload bytes leaked into Debug: {text}"
        );
    }
}
