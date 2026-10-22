//! I/O utilities for GSD-Core
//!
//! Provides async I/O utilities for file operations, streaming, and buffering.

use bytes::BytesMut;
use std::io;
use std::io::SeekFrom;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::SystemTime;
use tokio::fs;
use tokio::fs::OpenOptions;
use tokio::io::{
    AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncSeek, AsyncWrite, AsyncWriteExt, BufReader,
    BufWriter, ReadBuf,
};

/// Async file reader with buffering and position tracking
pub struct AsyncFileReader {
    reader: BufReader<fs::File>,
    path: PathBuf,
    position: u64,
}

impl AsyncFileReader {
    pub async fn open<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let file = fs::File::open(&path).await?;
        let reader = BufReader::new(file);
        Ok(Self {
            reader,
            path: path.as_ref().to_path_buf(),
            position: 0,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn position(&self) -> u64 {
        self.position
    }

    pub async fn read_exact(&mut self, buf: &mut [u8]) -> io::Result<()> {
        let n = self.reader.read_exact(buf).await?;
        self.position += n as u64;
        Ok(())
    }

    pub async fn read_to_end(&mut self, buf: &mut Vec<u8>) -> io::Result<usize> {
        let n = self.reader.read_to_end(buf).await?;
        self.position += n as u64;
        Ok(n)
    }

    pub async fn read_line(&mut self, buf: &mut String) -> io::Result<usize> {
        let n = self.reader.read_line(buf).await?;
        self.position += n as u64;
        Ok(n)
    }
}

impl AsyncRead for AsyncFileReader {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        Pin::new(&mut this.reader).poll_read(cx, buf)
    }
}

impl AsyncSeek for AsyncFileReader {
    fn start_seek(self: Pin<&mut Self>, pos: SeekFrom) -> io::Result<()> {
        Pin::new(&mut self.get_mut().reader).start_seek(pos)
    }

    fn poll_complete(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<u64>> {
        Pin::new(&mut self.get_mut().reader).poll_complete(cx)
    }
}

/// Async file writer with buffering and position tracking
pub struct AsyncFileWriter {
    writer: BufWriter<fs::File>,
    path: PathBuf,
    position: u64,
}

impl AsyncFileWriter {
    pub async fn create<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let file = fs::File::create(&path).await?;
        let writer = BufWriter::new(file);
        Ok(Self {
            writer,
            path: path.as_ref().to_path_buf(),
            position: 0,
        })
    }

    pub async fn append<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let file = OpenOptions::new()
            .append(true)
            .create(true)
            .open(&path)
            .await?;
        let writer = BufWriter::new(file);
        Ok(Self {
            writer,
            path: path.as_ref().to_path_buf(),
            position: 0,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn position(&self) -> u64 {
        self.position
    }

    pub async fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        self.writer.write_all(buf).await?;
        self.position += buf.len() as u64;
        Ok(())
    }

    pub async fn flush(&mut self) -> io::Result<()> {
        self.writer.flush().await
    }
}

impl AsyncWrite for AsyncFileWriter {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        Pin::new(&mut this.writer).poll_write(cx, buf)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().writer).poll_flush(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().writer).poll_shutdown(cx)
    }
}

impl AsyncSeek for AsyncFileWriter {
    fn start_seek(self: Pin<&mut Self>, pos: SeekFrom) -> io::Result<()> {
        Pin::new(&mut self.get_mut().writer).start_seek(pos)
    }

    fn poll_complete(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<u64>> {
        Pin::new(&mut self.get_mut().writer).poll_complete(cx)
    }
}

/// Streaming JSON writer for NDJSON output
pub struct NdjsonWriter<W: AsyncWrite + Unpin> {
    writer: BufWriter<W>,
}

impl<W: AsyncWrite + Unpin> NdjsonWriter<W> {
    pub fn new(writer: W) -> Self {
        Self {
            writer: BufWriter::new(writer),
        }
    }

    pub async fn write_value<T: serde::Serialize>(&mut self, value: &T) -> io::Result<()> {
        let mut buf = Vec::new();
        serde_json::to_writer(&mut buf, value)?;
        buf.push(b'\n');
        self.writer.write_all(&buf).await
    }

    pub async fn flush(&mut self) -> io::Result<()> {
        self.writer.flush().await
    }
}

/// Streaming JSON reader for NDJSON input
pub struct NdjsonReader<R: AsyncRead + Unpin> {
    reader: BufReader<R>,
    buffer: String,
}

impl<R: AsyncRead + Unpin> NdjsonReader<R> {
    pub fn new(reader: R) -> Self {
        Self {
            reader: BufReader::new(reader),
            buffer: String::new(),
        }
    }

    pub async fn read_value<T: for<'de> serde::Deserialize<'de>>(
        &mut self,
    ) -> io::Result<Option<T>> {
        let mut line = String::new();
        let n = self.reader.read_line(&mut line).await?;
        if n == 0 {
            return Ok(None);
        }
        let value = serde_json::from_str(line.trim())?;
        Ok(Some(value))
    }
}

/// Async temp file manager
pub struct TempFileManager {
    dir: PathBuf,
    files: Vec<PathBuf>,
}

impl TempFileManager {
    pub fn new() -> io::Result<Self> {
        let dir = tempfile::tempdir()?.keep();
        Ok(Self {
            dir,
            files: Vec::new(),
        })
    }

    pub fn path(&self) -> &Path {
        &self.dir
    }

    pub async fn create_file(&mut self, prefix: &str) -> io::Result<(PathBuf, fs::File)> {
        let path = self
            .dir
            .join(format!("{}_{}", prefix, uuid::Uuid::new_v4()));
        let file = fs::File::create(&path).await?;
        self.files.push(path.clone());
        Ok((path, file))
    }

    pub async fn cleanup(&mut self) -> io::Result<()> {
        for file in &self.files {
            let _ = fs::remove_file(file).await;
        }
        self.files.clear();
        Ok(())
    }
}

impl Default for TempFileManager {
    fn default() -> Self {
        Self::new().expect("Failed to create temp dir")
    }
}

impl Drop for TempFileManager {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Async copy with progress tracking
pub async fn copy_with_progress<R, W>(
    mut reader: R,
    mut writer: W,
    mut progress: impl FnMut(u64, u64) + Send,
) -> io::Result<u64>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let mut buffer = [0u8; 8192];
    let mut total = 0u64;

    loop {
        let n = match reader.read(&mut buffer).await {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) => return Err(e),
        };
        writer.write_all(&buffer[..n]).await?;
        total += n as u64;
        progress(total, 0);
    }

    writer.flush().await?;
    Ok(total)
}

/// Async copy with progress and cancellation
pub async fn copy_with_progress_and_cancel<R, W>(
    mut reader: R,
    mut writer: W,
    mut progress: impl FnMut(u64, u64) + Send,
    cancel: tokio::sync::watch::Receiver<bool>,
) -> io::Result<u64>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let mut buffer = [0u8; 8192];
    let mut total = 0u64;
    let mut cancel_rx = cancel;

    loop {
        tokio::select! {
            biased;
            _ = cancel_rx.changed() => {
                if *cancel_rx.borrow() {
                    return Err(io::Error::new(io::ErrorKind::Interrupted, "Cancelled"));
                }
            }
            n = reader.read(&mut buffer) => {
                let n = match n {
                    Ok(0) => break,
                    Ok(n) => n,
                    Err(e) => return Err(e),
                };
                writer.write_all(&buffer[..n]).await?;
                total += n as u64;
                progress(total, 0);
            }
        }
    }

    writer.flush().await?;
    Ok(total)
}

/// Async line reader with configurable delimiter
pub struct AsyncLineReader<R: AsyncRead + Unpin> {
    reader: BufReader<R>,
    delimiter: Vec<u8>,
    buffer: BytesMut,
}

impl<R: AsyncRead + Unpin> AsyncLineReader<R> {
    pub fn new(reader: R, delimiter: Vec<u8>) -> Self {
        Self {
            reader: BufReader::new(reader),
            delimiter,
            buffer: BytesMut::with_capacity(8192),
        }
    }

    pub async fn read_line(&mut self, buf: &mut Vec<u8>) -> io::Result<usize> {
        let mut buf = Vec::new();
        let n = self.reader.read_until(b'\n', &mut buf).await?;
        Ok(n)
    }
}

/// Async file copy with verification
pub async fn copy_and_verify<P: AsRef<Path>, Q: AsRef<Path>>(src: P, dst: Q) -> io::Result<()> {
    let src_file = fs::File::open(src).await?;
    let dst_file = fs::File::create(dst).await?;

    let mut src_reader = BufReader::new(src_file);
    let mut dst_writer = BufWriter::new(dst_file);

    let mut buffer = [0u8; 8192];
    let mut hasher = blake3::Hasher::new();

    loop {
        let n = src_reader.read(&mut buffer).await?;
        if n == 0 {
            break;
        }
        dst_writer.write_all(&buffer[..n]).await?;
        hasher.update(&buffer[..n]);
    }

    dst_writer.flush().await?;
    Ok(())
}

/// Async file metadata
#[derive(Debug, Clone)]
pub struct FileMetadata {
    pub path: PathBuf,
    pub size: u64,
    pub modified: SystemTime,
    pub created: Option<SystemTime>,
    pub is_dir: bool,
    pub is_file: bool,
    pub is_symlink: bool,
}

impl FileMetadata {
    pub async fn from_path<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let path = path.as_ref();
        let metadata = fs::metadata(path).await?;
        let file_type = metadata.file_type();

        Ok(Self {
            path: path.to_path_buf(),
            size: metadata.len(),
            modified: metadata.modified()?,
            created: metadata.created().ok(),
            is_dir: file_type.is_dir(),
            is_file: file_type.is_file(),
            is_symlink: file_type.is_symlink(),
        })
    }
}

/// Recursive directory walker with async iteration
pub struct AsyncWalkDir {
    stack: Vec<std::fs::ReadDir>,
}

impl AsyncWalkDir {
    pub fn new<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let read_dir = std::fs::read_dir(path)?;
        Ok(Self {
            stack: vec![read_dir],
        })
    }
}

impl Iterator for AsyncWalkDir {
    type Item = io::Result<PathBuf>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            let current = self.stack.last_mut()?;
            match current.next() {
                Some(Ok(entry)) => {
                    let path = entry.path();
                    if path.is_dir() {
                        match std::fs::read_dir(&path) {
                            Ok(read_dir) => self.stack.push(read_dir),
                            Err(e) => return Some(Err(e)),
                        }
                    }
                    return Some(Ok(path));
                }
                Some(Err(e)) => return Some(Err(e)),
                None => {
                    self.stack.pop();
                }
            }
        }
    }
}

/// Memory-mapped file reader for large files
pub struct MmapReader {
    mmap: memmap2::Mmap,
    position: usize,
}

impl MmapReader {
    pub fn open<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let file = std::fs::File::open(path)?;
        let mmap = unsafe { memmap2::Mmap::map(&file)? };
        Ok(Self { mmap, position: 0 })
    }

    pub fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let remaining = self.mmap.len().saturating_sub(self.position);
        let len = std::cmp::min(buf.len(), remaining);
        buf[..len].copy_from_slice(&self.mmap[self.position..self.position + len]);
        self.position += len;
        Ok(len)
    }

    pub fn slice(&self, start: usize, len: usize) -> &[u8] {
        &self.mmap[start..start + len]
    }

    pub fn len(&self) -> usize {
        self.mmap.len()
    }
}

/// Async streaming compressor
pub struct AsyncCompressor<W: AsyncWrite + Unpin> {
    encoder: flate2::write::GzEncoder<Vec<u8>>,
    sink: W,
}

impl<W: AsyncWrite + Unpin> AsyncCompressor<W> {
    pub fn new(writer: W, level: flate2::Compression) -> Self {
        Self {
            encoder: flate2::write::GzEncoder::new(Vec::new(), level),
            sink: writer,
        }
    }

    pub async fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        std::io::Write::write_all(&mut self.encoder, buf)?;
        let pending = std::mem::take(self.encoder.get_mut());
        self.sink.write_all(&pending).await
    }

    pub async fn finish(mut self) -> io::Result<()> {
        let pending = self.encoder.finish()?;
        self.sink.write_all(&pending).await?;
        self.sink.flush().await
    }
}

/// Async streaming decompressor
pub struct AsyncDecompressor<R: AsyncRead + Unpin> {
    reader: R,
    decoder: flate2::write::GzDecoder<Vec<u8>>,
}

impl<R: AsyncRead + Unpin> AsyncDecompressor<R> {
    pub fn new(reader: R) -> Self {
        Self {
            reader,
            decoder: flate2::write::GzDecoder::new(Vec::new()),
        }
    }

    pub async fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        loop {
            let decoded = self.decoder.get_mut();
            if !decoded.is_empty() {
                let n = decoded.len().min(buf.len());
                buf[..n].copy_from_slice(&decoded[..n]);
                decoded.drain(..n);
                return Ok(n);
            }
            let mut chunk = [0u8; 8192];
            let n = self.reader.read(&mut chunk).await?;
            if n == 0 {
                self.decoder.try_finish()?;
                if self.decoder.get_mut().is_empty() {
                    return Ok(0);
                }
            } else {
                std::io::Write::write_all(&mut self.decoder, &chunk[..n])?;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn test_file_reader_writer() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.txt");

        let mut writer = AsyncFileWriter::create(&path).await.unwrap();
        writer.write_all(b"Hello, World!\n").await.unwrap();
        writer.flush().await.unwrap();

        let mut reader = AsyncFileReader::open(&path).await.unwrap();
        let mut buf = String::new();
        reader.read_line(&mut buf).await.unwrap();
        assert_eq!(buf, "Hello, World!\n");
    }

    #[tokio::test]
    async fn test_ndjson() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.ndjson");

        let mut writer = NdjsonWriter::new(fs::File::create(&path).await.unwrap());
        writer
            .write_value(&serde_json::json!({"a": 1}))
            .await
            .unwrap();
        writer
            .write_value(&serde_json::json!({"b": 2}))
            .await
            .unwrap();
        writer.flush().await.unwrap();

        let mut reader = NdjsonReader::new(fs::File::open(&path).await.unwrap());
        let v1: serde_json::Value = reader.read_value().await.unwrap().unwrap();
        let v2: serde_json::Value = reader.read_value().await.unwrap().unwrap();
        assert_eq!(v1["a"], 1);
        assert_eq!(v2["b"], 2);
    }

    #[tokio::test]
    async fn test_temp_file_manager() {
        let mut manager = TempFileManager::new().unwrap();
        let (path, mut file) = manager.create_file("test").await.unwrap();
        file.write_all(b"test").await.unwrap();
        assert!(path.exists());
        manager.cleanup().await.unwrap();
        assert!(!path.exists());
    }
}
