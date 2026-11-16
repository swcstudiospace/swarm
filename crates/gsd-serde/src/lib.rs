//! Serialization layer for GSD-Core
//!
//! This crate provides serialization/deserialization for GSD-Core:
//! - Protobuf reflection-based serialization for storage
//! - Bincode encoding for efficient binary storage
//! - Checkpoint serialization for deterministic replay

use bincode::Options;
use prost_reflect::{DynamicMessage, MessageDescriptor};
use std::sync::Arc;

/// Serialization error
#[derive(Debug, thiserror::Error)]
pub enum SerdeError {
    #[error("Protobuf encode error: {0}")]
    ProtoEncode(#[from] prost::EncodeError),
    #[error("Protobuf decode error: {0}")]
    ProtoDecode(#[from] prost::DecodeError),
    #[error("Reflection protobuf encode error: {0}")]
    ReflectEncode(#[from] prost_reflect::prost::EncodeError),
    #[error("Reflection protobuf decode error: {0}")]
    ReflectDecode(#[from] prost_reflect::prost::DecodeError),
    #[error("Descriptor error: {0}")]
    Descriptor(#[from] prost_reflect::DescriptorError),
    #[error("Bincode error: {0}")]
    Bincode(#[from] bincode::Error),
    #[error("Reflection error: {0}")]
    Reflection(String),
    #[error("Any packing error: {0}")]
    AnyPacking(String),
    #[error("UTF-8 error: {0}")]
    Utf8(#[from] std::str::Utf8Error),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("batch record of {0} bytes does not fit its u32 length prefix")]
    RecordTooLarge(usize),
}

pub type SerdeResult<T> = std::result::Result<T, SerdeError>;

/// Length prefix for one batch record. A record a u32 cannot describe is refused.
fn frame_len(len: usize) -> SerdeResult<u32> {
    u32::try_from(len).map_err(|_| SerdeError::RecordTooLarge(len))
}

/// Configuration for serialization
#[derive(Debug, Clone)]
pub struct SerdeConfig {
    /// Use bincode for compact binary encoding
    pub use_bincode: bool,
    /// Include field names in output (for debugging)
    pub include_field_names: bool,
}

impl Default for SerdeConfig {
    fn default() -> Self {
        Self {
            use_bincode: true,
            include_field_names: false,
        }
    }
}

/// Decodes the bincode framing of one storage record into its protobuf bytes.
///
/// Trailing bytes are an error: a record must consume its whole input, so
/// concatenated or padded input is refused rather than silently ignored. The
/// options match `bincode::serialize` (fixint, little-endian), so the encoder's
/// wire format is unchanged.
fn decode_storage_bytes(bytes: &[u8]) -> SerdeResult<Vec<u8>> {
    bincode::DefaultOptions::new()
        .with_fixint_encoding()
        .reject_trailing_bytes()
        .deserialize::<Vec<u8>>(bytes)
        .map_err(Into::into)
}

/// Serializer for GSD-Core messages
pub struct Serializer {
    config: SerdeConfig,
}

impl Default for Serializer {
    fn default() -> Self {
        Self::new()
    }
}

impl Serializer {
    pub fn new() -> Self {
        Self {
            config: SerdeConfig::default(),
        }
    }

    pub fn with_config(config: SerdeConfig) -> Self {
        Self { config }
    }

    /// Serialize a Protobuf message to bincode bytes
    pub fn serialize<M: prost::Message>(&self, message: &M) -> SerdeResult<Vec<u8>> {
        if self.config.use_bincode {
            // Same bytes as `bincode::serialize(&protobuf)`: a u64 little-endian
            // length (bincode 1.x fixint) followed by the protobuf body. Both go
            // into one buffer sized up front, so the body is encoded once.
            let body_len = message.encoded_len();
            let mut buf = Vec::with_capacity(8 + body_len);
            buf.extend_from_slice(&(body_len as u64).to_le_bytes());
            message.encode(&mut buf)?;
            Ok(buf)
        } else {
            // Just protobuf
            let mut buf = Vec::new();
            message.encode(&mut buf)?;
            Ok(buf)
        }
    }

    /// Deserialize from bincode bytes to Protobuf message
    pub fn deserialize<M: prost::Message + Default>(&self, bytes: &[u8]) -> SerdeResult<M> {
        if self.config.use_bincode {
            let protobuf_bytes: Vec<u8> = decode_storage_bytes(bytes)?;
            M::decode(&*protobuf_bytes).map_err(Into::into)
        } else {
            M::decode(bytes).map_err(Into::into)
        }
    }

    /// Serialize using Protobuf reflection for dynamic messages
    pub fn serialize_dynamic(&self, message: &DynamicMessage) -> SerdeResult<Vec<u8>> {
        let mut buf = Vec::new();
        prost_reflect::prost::Message::encode(message, &mut buf)?;
        if self.config.use_bincode {
            Ok(bincode::serialize(&buf)?)
        } else {
            Ok(buf)
        }
    }

    /// Deserialize to DynamicMessage using reflection
    pub fn deserialize_dynamic(
        &self,
        descriptor: &MessageDescriptor,
        bytes: &[u8],
    ) -> SerdeResult<DynamicMessage> {
        let protobuf_bytes: Vec<u8> = if self.config.use_bincode {
            decode_storage_bytes(bytes)?
        } else {
            bytes.to_vec()
        };

        DynamicMessage::decode(descriptor.clone(), protobuf_bytes.as_slice())
            .map_err(SerdeError::ReflectDecode)
    }

    /// Serialize with field names for debugging
    pub fn serialize_debug<M: prost::Message + prost_reflect::ReflectMessage + std::fmt::Debug>(
        &self,
        message: &M,
    ) -> SerdeResult<String> {
        let mut buf = Vec::new();
        prost::Message::encode(message, &mut buf)?;
        let debug_str = format!(
            "{:?}",
            DynamicMessage::decode(message.descriptor(), buf.as_slice())?
        );
        Ok(debug_str)
    }
}

/// Deserializer for GSD-Core messages
pub struct Deserializer {
    config: SerdeConfig,
}

impl Default for Deserializer {
    fn default() -> Self {
        Self::new()
    }
}

impl Deserializer {
    pub fn new() -> Self {
        Self {
            config: SerdeConfig::default(),
        }
    }

    pub fn with_config(config: SerdeConfig) -> Self {
        Self { config }
    }

    /// Deserialize a Protobuf message from bincode bytes
    pub fn deserialize<M: prost::Message + Default>(&self, bytes: &[u8]) -> SerdeResult<M> {
        if self.config.use_bincode {
            let protobuf_bytes: Vec<u8> = decode_storage_bytes(bytes)?;
            M::decode(&*protobuf_bytes).map_err(Into::into)
        } else {
            M::decode(bytes).map_err(Into::into)
        }
    }

    /// Deserialize to DynamicMessage using reflection
    pub fn deserialize_dynamic(
        &self,
        descriptor: &prost_reflect::MessageDescriptor,
        bytes: &[u8],
    ) -> SerdeResult<prost_reflect::DynamicMessage> {
        let protobuf_bytes: Vec<u8> = if self.config.use_bincode {
            decode_storage_bytes(bytes)?
        } else {
            bytes.to_vec()
        };

        DynamicMessage::decode(descriptor.clone(), protobuf_bytes.as_slice())
            .map_err(SerdeError::ReflectDecode)
    }
}

/// Serialize for storage (checkpoints, artifacts)
pub fn serialize_for_storage<M: prost::Message>(message: &M) -> SerdeResult<Vec<u8>> {
    let serializer = Serializer::new();
    serializer.serialize(message)
}

/// Deserialize from storage
pub fn deserialize_from_storage<M: prost::Message + Default>(bytes: &[u8]) -> SerdeResult<M> {
    let deserializer = Deserializer::new();
    deserializer.deserialize(bytes)
}

/// Serialize with full debug info for debugging
pub fn serialize_debug<M: prost::Message + prost_reflect::ReflectMessage + std::fmt::Debug>(
    message: &M,
) -> SerdeResult<String> {
    let serializer = Serializer::new();
    serializer.serialize_debug(message)
}

/// Batch serialize multiple messages
pub fn serialize_batch<M: prost::Message>(messages: &[M]) -> SerdeResult<Vec<u8>> {
    let serializer = Serializer::new();
    let mut results = Vec::with_capacity(messages.len());

    for msg in messages {
        let encoded = serializer.serialize(msg)?;
        // Prefix with length for framing
        let len = frame_len(encoded.len())?.to_le_bytes();
        results.extend_from_slice(&len);
        results.extend_from_slice(&encoded);
    }

    Ok(results)
}

/// Deserialize batch of messages
pub fn deserialize_batch<M: prost::Message + Default>(bytes: &[u8]) -> SerdeResult<Vec<M>> {
    let mut offset = 0;
    let mut results = Vec::new();

    while offset < bytes.len() {
        // Each record is a 4-byte little-endian length followed by that many bytes.
        // Bytes that cannot form a whole record are truncated input, not a clean end.
        if bytes.len() - offset < 4 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "truncated batch: incomplete length prefix",
            )
            .into());
        }

        let len = u32::from_le_bytes([
            bytes[offset],
            bytes[offset + 1],
            bytes[offset + 2],
            bytes[offset + 3],
        ]) as usize;

        offset += 4;

        if bytes.len() - offset < len {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "truncated batch: record body shorter than its length prefix",
            )
            .into());
        }

        let msg_bytes = &bytes[offset..offset + len];
        let msg = deserialize_from_storage(msg_bytes)?;
        results.push(msg);

        offset += len;
    }

    Ok(results)
}

/// Registry for message descriptors (for dynamic serialization)
pub struct DescriptorRegistry {
    descriptors: dashmap::DashMap<String, Arc<prost_reflect::MessageDescriptor>>,
}

impl Default for DescriptorRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl DescriptorRegistry {
    pub fn new() -> Self {
        Self {
            descriptors: dashmap::DashMap::new(),
        }
    }

    pub fn register(&self, type_url: String, descriptor: prost_reflect::MessageDescriptor) {
        self.descriptors.insert(type_url, Arc::new(descriptor));
    }

    pub fn get(&self, type_url: &str) -> Option<Arc<prost_reflect::MessageDescriptor>> {
        self.descriptors.get(type_url).map(|v| v.clone())
    }

    pub fn register_from_file(
        &self,
        file_descriptor_set: &prost_types::FileDescriptorSet,
    ) -> Result<(), SerdeError> {
        let bytes = prost::Message::encode_to_vec(file_descriptor_set);
        let pool = prost_reflect::DescriptorPool::decode(bytes.as_slice())?;
        for file_desc in pool.files() {
            for msg_desc in file_desc.messages() {
                let type_url = msg_desc.full_name().to_string();
                self.register(type_url, msg_desc);
            }
        }
        Ok(())
    }
}

/// Serialize Any type (google.protobuf.Any)
pub fn pack_any<M: prost::Message>(message: &M, type_url: &str) -> SerdeResult<prost_types::Any> {
    let mut value = Vec::new();
    message.encode(&mut value)?;
    Ok(prost_types::Any {
        type_url: type_url.to_string(),
        value,
    })
}

/// Unpack Any type
pub fn unpack_any<M: prost::Message + Default>(any: &prost_types::Any) -> SerdeResult<M> {
    M::decode(&*any.value).map_err(Into::into)
}

/// Serialize checkpoint for storage
pub fn serialize_checkpoint<M: prost::Message>(checkpoint: &M) -> SerdeResult<Vec<u8>> {
    let serializer = Serializer::new();
    serializer.serialize(checkpoint)
}

/// Deserialize checkpoint
pub fn deserialize_checkpoint<M: prost::Message + Default>(bytes: &[u8]) -> SerdeResult<M> {
    let deserializer = Deserializer::new();
    deserializer.deserialize(bytes)
}

/// Serialize with version marker
pub fn serialize_with_version<M: prost::Message>(
    version: u32,
    message: &M,
) -> SerdeResult<Vec<u8>> {
    let mut buf = Vec::new();
    buf.extend_from_slice(&version.to_le_bytes());
    let serializer = Serializer::new();
    let encoded = serializer.serialize(message)?;
    buf.extend_from_slice(&encoded);
    Ok(buf)
}

/// Deserialize with version check
pub fn deserialize_with_version<M: prost::Message + Default>(
    bytes: &[u8],
    expected_version: u32,
) -> SerdeResult<M> {
    if bytes.len() < 4 {
        return Err(SerdeError::Reflection("Invalid versioned data".to_string()));
    }

    let version = u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);

    if version != expected_version {
        return Err(SerdeError::Reflection(format!(
            "Version mismatch: expected {}, got {}",
            expected_version, version
        )));
    }

    let deserializer = Deserializer::new();
    deserializer.deserialize(&bytes[4..])
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_proto::gsd::v1::{SubagentType, Task, TaskId};

    #[test]
    fn test_serialize_deserialize() {
        let task = Task {
            id: Some(TaskId {
                value: "test-1".to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: "test-agent".to_string(),
            }),
            ..Default::default()
        };

        let encoded = serialize_for_storage(&task).unwrap();
        let decoded: gsd_proto::gsd::v1::Task = deserialize_from_storage(&encoded).unwrap();

        assert_eq!(task.id, decoded.id);
    }

    #[test]
    fn test_batch_serialize_deserialize() {
        let tasks = vec![
            gsd_proto::gsd::v1::Task {
                id: Some(TaskId {
                    value: "1".to_string(),
                }),
                ..Default::default()
            },
            gsd_proto::gsd::v1::Task {
                id: Some(TaskId {
                    value: "2".to_string(),
                }),
                ..Default::default()
            },
        ];

        let encoded = serialize_batch(&tasks).unwrap();
        let decoded: Vec<gsd_proto::gsd::v1::Task> = deserialize_batch(&encoded).unwrap();

        assert_eq!(tasks.len(), decoded.len());
    }

    #[test]
    fn test_versioned_serialization() {
        let task = gsd_proto::gsd::v1::Task {
            id: Some(gsd_proto::gsd::v1::TaskId {
                value: "test".to_string(),
            }),
            ..Default::default()
        };

        let encoded = serialize_with_version(1, &task).unwrap();
        let decoded: gsd_proto::gsd::v1::Task = deserialize_with_version(&encoded, 1).unwrap();

        assert_eq!(task.id, decoded.id);
    }

    #[test]
    fn test_version_mismatch() {
        let task = gsd_proto::gsd::v1::Task::default();
        let encoded = serialize_with_version(1, &task).unwrap();

        let result = deserialize_with_version::<gsd_proto::gsd::v1::Task>(&encoded, 2);
        assert!(result.is_err());
    }

    #[test]
    fn test_batch_rejects_truncated_input() {
        let tasks: Vec<gsd_proto::gsd::v1::Task> = (1..=3)
            .map(|i| gsd_proto::gsd::v1::Task {
                id: Some(TaskId {
                    value: i.to_string(),
                }),
                ..Default::default()
            })
            .collect();
        let encoded = serialize_batch(&tasks).unwrap();

        let decoded: Vec<gsd_proto::gsd::v1::Task> = deserialize_batch(&encoded).unwrap();
        assert_eq!(decoded.len(), 3);

        let short_body = &encoded[..encoded.len() - 1];
        assert!(deserialize_batch::<gsd_proto::gsd::v1::Task>(short_body).is_err());

        let mut short_header = encoded.clone();
        short_header.extend_from_slice(&[0, 0]);
        assert!(deserialize_batch::<gsd_proto::gsd::v1::Task>(&short_header).is_err());
    }

    #[test]
    fn deserialize_from_storage_rejects_trailing_bytes() {
        let task = Task {
            id: Some(TaskId {
                value: "trailing".to_string(),
            }),
            ..Default::default()
        };
        let encoded = serialize_for_storage(&task).unwrap();
        assert!(deserialize_from_storage::<Task>(&encoded).is_ok());

        let doubled = [encoded.as_slice(), encoded.as_slice()].concat();
        let result = deserialize_from_storage::<Task>(&doubled);
        assert!(
            result.is_err(),
            "storage decoder accepted two concatenated records and ignored the trailing bytes"
        );
    }

    #[test]
    fn frame_len_refuses_records_beyond_u32() {
        assert_eq!(frame_len(7).unwrap(), 7);
        assert!(frame_len(u32::MAX as usize + 1).is_err());
    }

    #[test]
    fn deserialize_batch_has_no_unused_deserializer() {
        // Concatenated so this test's own source does not contain the forbidden line.
        let forbidden = concat!("let _deser", "ializer = Deserializer::new();");
        assert!(!include_str!("lib.rs").contains(forbidden));
    }

    mod alloc_count {
        use std::alloc::{GlobalAlloc, Layout, System};
        use std::cell::Cell;

        thread_local! {
            static ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
        }

        pub struct CountingAlloc;

        unsafe impl GlobalAlloc for CountingAlloc {
            unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
                ALLOCATIONS.with(|count| count.set(count.get() + 1));
                System.alloc(layout)
            }

            unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
                System.dealloc(ptr, layout)
            }

            unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
                ALLOCATIONS.with(|count| count.set(count.get() + 1));
                System.realloc(ptr, layout, new_size)
            }
        }

        /// Runs `f` and returns its result with the number of heap allocations
        /// made on this thread while it ran.
        pub fn allocations_during<R>(f: impl FnOnce() -> R) -> (R, usize) {
            let before = ALLOCATIONS.with(Cell::get);
            let out = f();
            (out, ALLOCATIONS.with(Cell::get) - before)
        }
    }

    #[global_allocator]
    static GLOBAL: alloc_count::CountingAlloc = alloc_count::CountingAlloc;

    #[test]
    fn serialize_storage_record_allocates_once_and_matches_bincode() {
        let task = Task {
            id: Some(TaskId {
                value: "allocation-count".repeat(4),
            }),
            ..Default::default()
        };
        let serializer = Serializer::new();

        let (encoded, allocations) =
            alloc_count::allocations_during(|| serializer.serialize(&task).unwrap());

        assert_eq!(allocations, 1);
        let mut protobuf = Vec::new();
        prost::Message::encode(&task, &mut protobuf).unwrap();
        assert_eq!(encoded, bincode::serialize(&protobuf).unwrap());
    }
}
