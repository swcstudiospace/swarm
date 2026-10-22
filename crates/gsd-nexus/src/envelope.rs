/// A message carried over a Nexus topic, tagged for request/response correlation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NexusMessage {
    /// Topic the message is published on.
    pub topic: String,
    /// Identifier linking a response to the request that produced it.
    pub correlation_id: String,
    /// Opaque message body.
    pub payload: Vec<u8>,
}
