use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::Duration;

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum PoolState {
    Idle,
    Initializing,
    Active,
    Scaling,
    Draining,
    Terminating,
    Error,
}

pub struct SubagentPool {
    size: AtomicUsize,
    state: Arc<Mutex<PoolState>>,
    active: AtomicUsize,
    workers: AtomicUsize,
    // simple worker simulation
    threads: Vec<thread::JoinHandle<()>>,
}

impl SubagentPool {
    pub fn new(size: usize) -> Result<Self, String> {
        if size > 30 {
            return Err("exceeds 30-subagent cap".into());
        }
        Ok(Self {
            size: AtomicUsize::new(size),
            state: Arc::new(Mutex::new(PoolState::Idle)),
            active: AtomicUsize::new(0),
            workers: AtomicUsize::new(size),
            threads: vec![],
        })
    }

    pub fn create(&self) {
        let mut s = self.state.lock().unwrap();
        *s = PoolState::Active;
    }

    pub fn scale(&self, new_size: usize) -> Result<usize, String> {
        if new_size > 30 {
            return Err("exceeds cap".into());
        }
        {
            let mut s = self.state.lock().unwrap();
            *s = PoolState::Scaling;
        }
        self.size.store(new_size, Ordering::SeqCst);
        self.workers.store(new_size, Ordering::SeqCst);
        {
            let mut s = self.state.lock().unwrap();
            *s = PoolState::Active;
        }
        Ok(new_size)
    }

    pub fn state(&self) -> PoolState {
        *self.state.lock().unwrap()
    }

    pub fn size(&self) -> usize {
        self.size.load(Ordering::SeqCst)
    }

    pub fn active_workers(&self) -> usize {
        self.workers.load(Ordering::SeqCst)
    }

    pub fn submit(&self) -> Result<(), String> {
        if self.state() == PoolState::Error || self.state() == PoolState::Terminating {
            return Err("pool in terminal state".into());
        }
        if self.state() != PoolState::Active {
            let mut s = self.state.lock().unwrap();
            *s = PoolState::Error;
            return Err("fatal: entered Error state from submit".into());
        }
        self.active.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    pub fn force_error(&self) {
        let mut s = self.state.lock().unwrap();
        *s = PoolState::Error;
    }

    pub fn begin_scaling(&self) {
        let mut s = self.state.lock().unwrap();
        *s = PoolState::Scaling;
    }

    pub fn end_scaling(&self) {
        let mut s = self.state.lock().unwrap();
        if *s == PoolState::Scaling {
            *s = PoolState::Active;
        }
    }

    pub fn return_to_idle(&self) {
        self.active.store(0, Ordering::SeqCst);
        let mut s = self.state.lock().unwrap();
        *s = PoolState::Idle;
    }
}
