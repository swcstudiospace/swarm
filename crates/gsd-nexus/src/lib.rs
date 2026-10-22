//! Nexus coordinator skeleton: pub/sub envelope types and traits.
//!
//! Barrier join and any transport implementation are out of scope for this crate's skeleton.

pub mod envelope;

pub use envelope::NexusMessage;

use std::fmt;

/// Errors returned by Nexus publish and subscribe operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NexusError {
    /// The Nexus channel has been closed and accepts no further traffic.
    Closed,
    /// The named topic is not known to the Nexus.
    UnknownTopic(String),
}

impl fmt::Display for NexusError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NexusError::Closed => write!(f, "nexus channel is closed"),
            NexusError::UnknownTopic(topic) => write!(f, "unknown nexus topic: {topic}"),
        }
    }
}

impl std::error::Error for NexusError {}

/// Publishes messages onto the Nexus.
pub trait NexusPublisher {
    /// Publishes `message` to its topic.
    fn publish(&self, message: NexusMessage) -> Result<(), NexusError>;
}

/// Subscribes to topics on the Nexus.
pub trait NexusSubscriber {
    /// Subscribes to the named `topic`.
    fn subscribe(&self, topic: &str) -> Result<(), NexusError>;
}
