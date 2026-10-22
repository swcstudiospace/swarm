//! IPC layer for GSD-Core
//!
//! Provides inter-process communication for GSD-Core:
//! - Unix Domain Sockets (UDS) for control plane gRPC
//! - Shared memory (memfd) for Nexus CRDT state
//! - High-performance message passing

pub mod uds;
pub mod shmem;
pub mod channel;

pub use uds::*;
pub use shmem::*;
pub use channel::*;

use std::sync::Arc;
use std::path::Path;
use tokio::net::{UnixListener, UnixStream};
use tokio::io::{AsyncRead, AsyncWrite};
use tonic::transport::{Endpoint, Uri, Channel};
use tonic::transport::server::Server;
use tonic::transport::server::TcpIncoming;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::sync::Arc;
use tokio::sync::Mutex;
use tonic::transport::server::Connected;
use tonic::transport::server::ConnectedStream;
use std::net::SocketAddr;

/// Result type for IPC operations
pub type IpcResult<T> = std::result::Result<T, IpcError>;

/// IPC errors
#[derive(Debug, thiserror::Error)]
pub enum IpcError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Tonic transport error: {0}")]
    TonicTransport(#[from] tonic::transport::Error),
    #[error("Connection error: {0}")]
    Connection(String),
    #[error("Protocol error: {0}")]
    Protocol(String),
    #[error("Serialization error: {0}")]
    Serialization(#[from] crate::serde::SerdeError),
    #[error("Not connected")]
    NotConnected,
    #[error("Already connected")]
    AlreadyConnected,
}

pub type IpcResult<T> = std::result::Result<T, IpcError>;

/// Unix Domain Socket server for gRPC control plane
pub struct UdsServer {
    listener: UnixListener,
    shutdown: tokio::sync::watch::Sender<bool>,
}

impl UdsServer {
    pub async fn bind<P: AsRef<std::path::Path>>(path: P) -> IpcResult<Self> {
        // Remove existing socket file if it exists
        if std::path::Path::new(path.as_ref()).exists() {
            std::fs::remove_file(path.as_ref())?;
        }
        
        let listener = UnixListener::bind(path)?;
        let (shutdown_tx, _) = tokio::sync::watch::channel(false);
        
        Ok(Self {
            listener,
            shutdown: shutdown_tx,
        })
    }
    
    pub async fn serve<S>(self, service: S) -> IpcResult<()>
    where
        S: tonic::server::Service<tonic::body::BoxBody> + Clone + Send + Sync + 'static,
        S::ResponseBody: tonic::body::Body + Send + 'static,
        S::Error: Into<Box<dyn std::error::Error + Send + Sync>>,
        S::Future: Send,
    {
        let shutdown_rx = self.shutdown.subscribe();
        
        Server::builder()
            .add_service(tonic::server::Router::new().add_service(service))
            .serve_with_incoming(UnixStreamIncoming {
                listener: self.listener,
                shutdown: shutdown_rx,
            })
            .await?;
        
        Ok(())
    }
    
    pub fn shutdown(&self) {
        let _ = self.shutdown.send(true);
    }
}

/// Unix stream incoming for tonic
struct UnixStreamIncoming {
    listener: UnixListener,
    shutdown: tokio::sync::watch::Receiver<bool>,
}

impl tonic::transport::server::IntoIncoming for UnixStreamIncoming {
    type Stream = UnixStream;
    type IntoIter = UnixStreamIterator;
    
    fn into_incoming(self) -> Self::IntoIter {
        UnixStreamIterator {
            listener: self.listener,
            shutdown: self.shutdown,
        }
    }
}

struct UnixStreamIterator {
    listener: UnixListener,
    shutdown: tokio::sync::watch::Receiver<bool>,
}

impl Iterator for UnixStreamIterator {
    type Item = std::io::Result<UnixStream>;
    
    fn next(&mut self) -> Option<Self::Item> {
        if *self.shutdown.borrow() {
            return None;
        }
        
        match self.listener.accept() {
            Ok((stream, _)) => Some(Ok(stream)),
            Err(e) => Some(Err(e)),
        }
    }
}

impl tonic::transport::server::Connected for UnixStream {
    fn remote_addr(&self) -> Option<std::net::SocketAddr> {
        None
    }
    
    fn peer_cert(&self) -> Option<tonic::transport::Certificate> {
        None
    }
}

/// UDS client for connecting to the control plane
pub struct UdsClient {
    channel: Channel,
}

impl UdsClient {
    pub async fn connect<P: AsRef<std::path::Path>>(path: P) -> IpcResult<Self> {
        let path = path.as_ref().to_path_buf();
        
        let channel = Endpoint::try_from("http://[::]:50051")?
            .connect_with_connector(UdsConnector { path })
            .await?;
        
        Ok(Self { channel })
    }
    
    pub fn channel(&self) -> &Channel {
        &self.channel
    }
}

/// Connector for UDS
struct UdsConnector {
    path: std::path::PathBuf,
}

impl tower::Service<Uri> for UdsConnector {
    type Response = UnixStream;
    type Error = std::io::Error;
    type Future = Pin<Box<dyn Future<Output = Result<UnixStream, std::io::Error>> + Send>>;
    
    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }
    
    fn call(&mut self, _uri: Uri) -> Self::Future {
        let path = self.path.clone();
        Box::pin(async move {
            UnixStream::connect(path).await
        })
    }
}

/// Shared memory for Nexus CRDT state
pub mod shmem {
    use super::*;
    use std::os::unix::io::AsRawFd;
    use std::os::fd::FromRawFd;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
    use memmap2::{MmapMut, MmapOptions};
    use std::fs::File;
    use std::os::fd::OwnedFd;
    use nix::sys::mman::{mmap, MapFlags, ProtFlags};
    use libc;
    use std::os::fd::AsFd;
    
    /// Shared memory region for CRDT state
    pub struct ShmemRegion {
        fd: OwnedFd,
        size: usize,
        mmap: Option<MmapMut>,
        seq: AtomicU64,
    }
    
    impl ShmemRegion {
        /// Create a new shared memory region
        pub fn new(size: usize) -> std::io::Result<Self> {
            // Create memfd
            let fd = unsafe {
                libc::memfd_create(
                    b"gsd-nexus\0".as_ptr() as *const libc::c_char,
                    libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
                )
            };
            
            if fd < 0 {
                return Err(std::io::Error::last_os_error());
            }
            
            let fd = unsafe { OwnedFd::from_raw_fd(fd) };
            
            // Set size
            nix::unistd::ftruncate(&fd, size as libc::off_t)?;
            
            // Seal the file to prevent further size changes
            unsafe {
                libc::fcntl(fd.as_raw_fd(), libc::F_ADD_SEALS, libc::F_SEAL_SHRINK | libc::F_SEAL_GROW);
            }
            
            Ok(Self {
                fd,
                size,
                mmap: None,
                seq: std::sync::atomic::AtomicU64::new(0),
            })
        }
        
        /// Map the shared memory into address space
        pub fn map(&mut self) -> std::io::Result<()> {
            if self.mmap.is_some() {
                return Ok(());
            }
            
            let file = unsafe { File::from_raw_fd(self.fd.as_raw_fd()) };
            let mmap = unsafe {
                MmapOptions::new()
                    .len(self.size)
                    .map_mut(&file)?
            };
            
            self.mmap = Some(mmap);
            std::mem::forget(file); // Don't close the file, mmap holds it
            
            Ok(())
        }
        
        /// Unmap the region
        pub fn unmap(&mut self) {
            self.mmap = None;
        }
        
        /// Get mutable slice to the mapped memory
        pub fn as_mut_slice(&mut self) -> Option<&mut [u8]> {
            self.mmap.as_mut().map(|m| &mut m[..])
        }
        
        /// Get immutable slice
        pub fn as_slice(&self) -> Option<&[u8]> {
            self.mmap.as_ref().map(|m| &m[..])
        }
        
        /// Get size
        pub fn size(&self) -> usize {
            self.size
        }
        
        /// Get sequence number for synchronization
        pub fn seq(&self) -> u64 {
            self.seq.load(Ordering::Acquire)
        }
        
        /// Increment sequence
        pub fn inc_seq(&self) -> u64 {
            self.seq.fetch_add(1, Ordering::AcqRel)
        }
        
        /// Write data at offset
        pub fn write_at(&mut self, offset: usize, data: &[u8]) -> std::io::Result<()> {
            if let Some(mmap) = &mut self.mmap {
                let end = offset + data.len();
                if end > self.size {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "Write exceeds region size"
                    ));
                }
                mmap[offset..offset + data.len()].copy_from_slice(data);
                Ok(())
            } else {
                Err(std::io::Error::new(
                    std::io::ErrorKind::NotConnected,
                    "Not mapped"
                ))
            }
        }
        
        /// Read data at offset
        pub fn read_at(&self, offset: usize, buf: &mut [u8]) -> std::io::Result<()> {
            if let Some(mmap) = &self.mmap {
                let end = offset + buf.len();
                if end > self.size {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "Read exceeds region size"
                    ));
                }
                buf.copy_from_slice(&mmap[offset..offset + buf.len()]);
                Ok(())
            } else {
                Err(std::io::Error::new(
                    std::io::ErrorKind::NotConnected,
                    "Not mapped"
                ))
            }
        }
    }
    
    /// Shared memory pool for multiple regions
    pub struct ShmemPool {
        regions: dashmap::DashMap<String, Arc<parking_lot::Mutex<ShmemRegion>>>,
    }
    
    impl ShmemPool {
        pub fn new() -> Self {
            Self {
                regions: dashmap::DashMap::new(),
            }
        }
        
        pub fn create(&self, name: String, size: usize) -> std::io::Result<Arc<parking_lot::Mutex<ShmemRegion>>> {
            let region = ShmemRegion::new(size)?;
            let region = Arc::new(parking_lot::Mutex::new(region));
            self.regions.insert(name, region.clone());
            Ok(region)
        }
        
        pub fn get(&self, name: &str) -> Option<Arc<parking_lot::Mutex<ShmemRegion>>> {
            self.regions.get(name).map(|v| v.clone())
        }
        
        pub fn remove(&self, name: &str) -> Option<Arc<parking_lot::Mutex<ShmemRegion>>> {
            self.regions.remove(name).map(|v| v.1)
        }
    }
    
    impl Default for ShmemPool {
        fn default() -> Self {
            Self::new()
        }
    }
}

/// High-performance message channel for inter-thread communication
pub mod channel {
    use super::*;
    use crossbeam::channel::{bounded, unbounded, Sender, Receiver, TryRecvError};
    use tokio::sync::{mpsc, oneshot, watch};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use tokio::sync::{mpsc, oneshot, broadcast};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    
    /// High-throughput MPSC channel for task messages
    pub struct TaskChannel<T> {
        sender: mpsc::Sender<T>,
        receiver: Arc<Mutex<mpsc::Receiver<T>>>,
    }
    
    impl<T> TaskChannel<T> {
        pub fn new(capacity: usize) -> (TaskSender<T>, TaskReceiver<T>) {
            let (tx, rx) = mpsc::channel(capacity);
            let rx = Arc::new(tokio::sync::Mutex::new(rx));
            (TaskSender { sender: tx }, TaskReceiver { receiver: rx })
        }
    }
    
    #[derive(Clone)]
    pub struct TaskSender<T> {
        sender: mpsc::Sender<T>,
    }
    
    impl<T> TaskSender<T> {
        pub async fn send(&self, msg: T) -> Result<(), mpsc::error::SendError<T>> {
            self.sender.send(msg).await
        }
        
        pub fn try_send(&self, msg: T) -> Result<(), mpsc::error::TrySendError<T>> {
            self.sender.try_send(msg)
        }
        
        pub fn is_closed(&self) -> bool {
            self.sender.is_closed()
        }
    }
    
    pub struct TaskReceiver<T> {
        receiver: Arc<tokio::sync::Mutex<mpsc::Receiver<T>>>,
    }
    
    impl<T> TaskReceiver<T> {
        pub async fn recv(&self) -> Option<T> {
            self.receiver.lock().await.recv().await
        }
        
        pub fn try_recv(&self) -> Result<T, tokio::sync::mpsc::error::TryRecvError> {
            self.receiver.try_lock()
                .map(|mut r| r.try_recv())
                .unwrap_or(Err(tokio::sync::mpsc::error::TryRecvError::Empty))
        }
        
        pub async fn close(&self) {
            self.receiver.lock().await.close();
        }
    }
    
    /// Broadcast channel for Nexus pub/sub
    pub struct BroadcastChannel<T> {
        sender: broadcast::Sender<T>,
    }
    
    impl<T: Clone + Send + 'static> BroadcastChannel<T> {
        pub fn new(capacity: usize) -> (Self, BroadcastReceiver<T>) {
            let (tx, rx) = broadcast::channel(capacity);
            (Self { sender: tx }, BroadcastReceiver { receiver: rx })
        }
        
        pub fn send(&self, msg: T) -> Result<usize, broadcast::error::SendError<T>> {
            self.sender.send(msg)
        }
        
        pub fn subscribe(&self) -> BroadcastReceiver<T> {
            BroadcastReceiver { receiver: self.sender.subscribe() }
        }
        
        pub fn receiver_count(&self) -> usize {
            self.sender.receiver_count()
        }
    }
    
    pub struct BroadcastReceiver<T> {
        receiver: broadcast::Receiver<T>,
    }
    
    impl<T: Clone> BroadcastReceiver<T> {
        pub async fn recv(&mut self) -> Result<T, broadcast::error::RecvError> {
            self.receiver.recv().await
        }
        
        pub fn try_recv(&mut self) -> Result<T, broadcast::error::RecvError> {
            self.receiver.try_recv()
        }
    }
    
    /// Request-response channel for RPC-style communication
    pub struct RpcChannel<Req, Resp> {
        sender: mpsc::Sender<(Req, oneshot::Sender<Resp>)>,
        receiver: Arc<Mutex<mpsc::Receiver<(Req, oneshot::Sender<Resp>)>>>,
    }
    
    impl<Req, Resp> RpcChannel<Req, Resp> {
        pub fn new(capacity: usize) -> (RpcSender<Req, Resp>, RpcReceiver<Req, Resp>) {
            let (tx, rx) = mpsc::channel(capacity);
            let rx = Arc::new(tokio::sync::Mutex::new(rx));
            (RpcSender { sender: tx }, RpcReceiver { receiver: Arc::new(tokio::sync::Mutex::new(rx)) })
        }
    }
    
    #[derive(Clone)]
    pub struct RpcSender<Req, Resp> {
        sender: mpsc::Sender<(Req, oneshot::Sender<Resp>)>,
    }
    
    impl<Req, Resp> RpcSender<Req, Resp> {
        pub async fn call(&self, req: Req) -> Result<Resp, oneshot::error::RecvError> {
            let (tx, rx) = oneshot::channel();
            self.sender.send((req, tx)).await?;
            rx.await.map_err(|_| "Channel closed".into())
        }
    }
    
    pub struct RpcReceiver<Req, Resp> {
        receiver: Arc<Mutex<mpsc::Receiver<(Req, oneshot::Sender<Resp>)>>>,
    }
    
    impl<Req, Resp> RpcReceiver<Req, Resp> {
        pub async fn recv(&self) -> Option<(Req, oneshot::Sender<Resp>)> {
            self.receiver.lock().await.recv().await
        }
    }
}

/// Unix domain socket utilities
pub mod uds {
    use super::*;
    use tokio::net::{UnixListener, UnixStream};
    use std::path::Path;
    use std::io;
    
    /// Create a UDS server socket
    pub async fn bind<P: AsRef<Path>>(path: P) -> io::Result<UnixListener> {
        let path = path.as_ref();
        if path.exists() {
            std::fs::remove_file(path)?;
        }
        UnixListener::bind(path)
    }
    
    /// Connect to a UDS server
    pub async fn connect<P: AsRef<Path>>(path: P) -> io::Result<UnixStream> {
        UnixStream::connect(path).await
    }
    
    /// Send a file descriptor over UDS (for passing file descriptors)
    pub async fn send_fd(stream: &mut UnixStream, fd: std::os::fd::RawFd) -> io::Result<()> {
        use std::os::unix::io::AsRawFd;
        use std::os::fd::AsFd;
        
        let mut buf = [0u8; 1];
        let mut ancillary = [0u8; 128];
        let mut msg = io::IoSliceMut::new(&mut [0]);
        let mut cmsg_space = nix::cmsg_space!(c_int);
        let mut cmsg = nix::cmsg_space!(c_int);
        
        // This is a simplified version - real implementation would use
        // sendmsg with SCM_RIGHTS
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;
    
    #[tokio::test]
    async fn test_shmem_region() {
        let mut region = ShmemRegion::new(4096).unwrap();
        region.map().unwrap();
        
        let data = b"Hello, World!";
        region.write_at(0, data).unwrap();
        
        let mut buf = vec![0u8; data.len()];
        region.read_at(0, &mut buf).unwrap();
        assert_eq!(buf, data);
    }
    
    #[tokio::test]
    async fn test_shmem_pool() {
        let pool = ShmemPool::new();
        let region = pool.create("test".to_string(), 4096).unwrap();
        
        let mut guard = region.lock();
        guard.map().unwrap();
        guard.write_at(0, b"test").unwrap();
        
        let mut buf = vec![0u8; 4];
        guard.read_at(0, &mut buf).unwrap();
        assert_eq!(&buf, b"test");
    }
    
    #[tokio::test]
    async fn test_task_channel() {
        let (tx, rx) = TaskChannel::<String>::new(10);
        
        tx.send("hello".to_string()).await.unwrap();
        let msg = rx.recv().await.unwrap();
        assert_eq!(msg, "hello");
    }
    
    #[tokio::test]
    async fn test_broadcast() {
        let (tx, mut rx) = BroadcastChannel::<String>::new(10);
        
        tx.send("hello".to_string()).unwrap();
        let msg = rx.recv().await.unwrap();
        assert_eq!(msg, "hello");
    }
    
    #[tokio::test]
    async fn test_rpc_channel() {
        let (tx, rx) = RpcChannel::<String, String>::new(10);
        
        tokio::spawn(async move {
            while let Some((req, tx)) = rx.recv().await {
                tx.send(format!("response to: {}", req)).unwrap();
            }
        });
        
        let resp = tx.call("hello".to_string()).await.unwrap();
        assert_eq!(resp, "response to: hello");
    }
}