//! The host side of the `gsd:core-host@0.2.0` world: the imports `log`, `time`, `rand` and
//! `storage`, each gated by a capability token.
//!
//! The read route's world (`gsd:core@0.1.0`) imports nothing and keeps its empty linker. A
//! component that declares this world's imports is linked by [`HostBridge`], which adds only the
//! imports the host can serve: `log`, `time` and `rand` always, and `storage` only with a
//! [`SessionStorage`] backend. Any other import, `omp-callback` included, stays unresolved, so
//! instantiation fails with `E-DEP`.
//!
//! The guest never holds a token. The host presents one for each call ([`HostCall::token`]), and
//! every import checks it with [`CapabilityTokenManager::authorize`] before it acts. A refusal
//! traps the call with a fixed message, which [`HostCallError::Refused`] reports as `E-POLICY`. A
//! log batch over [`LOG_BATCH_CAP_BYTES`] is `E-CONTRACT`, and a storage backend fault is `E-DEP`.
//! A guest cannot catch a trap, so a refusal cannot be ignored.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Instant;

use gsd_failure::ErrorCode;
use gsd_security::CapabilityTokenManager;
use wasmtime::component::{Linker, Val};

use bindings::gsd::core_host::{log, rand, storage, time};

use crate::wasmtime_limits::{arm_call_budget, classify, Limits};
use crate::wasmtime_module_load::{GuestInstance, GuestLoader, GuestState, LoadError, LoadedGuest};

/// The largest total charge of the `log` events one call may emit. Each event is charged at least
/// one byte. Provisional (OD-9).
pub const LOG_BATCH_CAP_BYTES: usize = 100_000;

const LOG_INTERFACE: &str = "gsd:core-host/log@0.2.0";
const TIME_INTERFACE: &str = "gsd:core-host/time@0.2.0";
const RAND_INTERFACE: &str = "gsd:core-host/rand@0.2.0";
const STORAGE_INTERFACE: &str = "gsd:core-host/storage@0.2.0";

const LOG_SCOPE: &str = "log.write";
const TIME_SCOPE: &str = "time.read";
const RAND_SCOPE: &str = "rand.read";
const STORAGE_WRITE_SCOPE: &str = "storage.write";
const STORAGE_READ_SCOPE: &str = "storage.read";

/// The SplitMix64 increment, the golden-ratio constant.
const SPLITMIX_GAMMA: u64 = 0x9E37_79B9_7F4A_7C15;

/// Rust types and host traits generated from the `gsd:core-host@0.2.0` world. Trappable imports
/// return `wasmtime::Result`, so a refusal is raised as a trap.
pub mod bindings {
    wasmtime::component::bindgen!({
        path: "../gsd-guest/wit/host",
        world: "gsd-core-host",
        imports: { default: trappable },
    });
}

/// A session storage backend. The host supplies one to link the `storage` import; without one the
/// import is not linked.
pub trait SessionStorage: Send + Sync {
    /// Stores `value` under `key` in the namespace of `thread_id`.
    fn put(&self, thread_id: &str, key: &str, value: &str) -> Result<(), StorageUnavailable>;

    /// True when `key` holds a value in the namespace of `thread_id`.
    fn contains(&self, thread_id: &str, key: &str) -> Result<bool, StorageUnavailable>;
}

/// A session storage backend that cannot serve a call.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("the session storage backend is unavailable")]
pub struct StorageUnavailable;

/// A refusal raised by a host import. It is raised as a trap, so the guest cannot ignore it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
enum HostRefusal {
    /// The call carries no token for this import, or the token does not grant it.
    #[error("the host refused the call: its capability token does not grant this import")]
    Policy,
    /// The call's log events exceed the batch cap.
    #[error("the host refused the call: its log events exceed the batch cap")]
    Contract,
    /// The session storage backend cannot serve the call.
    #[error("the host refused the call: the session storage backend is unavailable")]
    Dep,
}

impl HostRefusal {
    fn error_code(&self) -> ErrorCode {
        match self {
            Self::Policy => ErrorCode::Policy,
            Self::Contract => ErrorCode::Contract,
            Self::Dep => ErrorCode::Dep,
        }
    }
}

/// The identity and entropy one call runs under, chosen by the host.
pub struct HostCall<'a> {
    /// The thread the call serves. A token must be issued for this thread, and storage is keyed
    /// by it.
    pub thread_id: &'a str,
    /// The capability token presented for every import of this call. `None` refuses every import.
    pub token: Option<&'a str>,
    /// The seed of the call's `rand` stream. The caller records it, and [`CallRecord::seed`]
    /// returns it.
    pub seed: u64,
}

/// What a successful call left behind: the seed it ran under and the events it logged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallRecord {
    /// The seed the call's `rand` stream started from.
    pub seed: u64,
    /// The `log` events the call emitted, in order.
    pub events: Vec<String>,
}

/// A failed host call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostCallError {
    /// A host import refused the call. The code is `E-POLICY`, `E-CONTRACT` or `E-DEP`.
    Refused(ErrorCode),
    /// The call did not complete. The code is classified as the read route's traps are.
    Trap(ErrorCode),
}

impl HostCallError {
    /// The taxonomy code of this failure, whichever variant carries it.
    pub fn error_code(&self) -> ErrorCode {
        match self {
            Self::Refused(code) | Self::Trap(code) => *code,
        }
    }
}

/// Classifies a runtime error from a host call. A refusal keeps its own code. Anything else is
/// classified exactly as the read route's traps are.
fn classify_host(error: &wasmtime::Error, deadline_configured: bool) -> HostCallError {
    match error.downcast_ref::<HostRefusal>() {
        Some(refusal) => HostCallError::Refused(refusal.error_code()),
        None => HostCallError::Trap(classify(error, deadline_configured)),
    }
}

/// The state of the call in progress. The import closures read and write it.
#[derive(Default)]
struct ActiveCall {
    thread_id: String,
    token: Option<String>,
    seed: u64,
    random: u64,
    log_bytes: usize,
    events: Vec<String>,
}

impl ActiveCall {
    fn new(call: &HostCall<'_>) -> Self {
        Self {
            thread_id: call.thread_id.to_owned(),
            token: call.token.map(str::to_owned),
            seed: call.seed,
            random: call.seed,
            log_bytes: 0,
            events: Vec::new(),
        }
    }

    /// Admits one log event of `len` bytes. The call's events may total at most
    /// [`LOG_BATCH_CAP_BYTES`]; the event that crosses the cap is refused, not truncated. Each
    /// event is charged at least one byte, so the number of events is bounded as well.
    fn admit_log(&mut self, len: usize) -> Result<(), HostRefusal> {
        let charged = len.max(1);
        let total = self.log_bytes.saturating_add(charged);
        if total > LOG_BATCH_CAP_BYTES {
            return Err(HostRefusal::Contract);
        }
        self.log_bytes = total;
        Ok(())
    }

    /// The next value of the call's SplitMix64 stream.
    fn next_random(&mut self) -> u64 {
        self.random = self.random.wrapping_add(SPLITMIX_GAMMA);
        let mut z = self.random;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}

/// State shared by the import closures of one instance.
struct HostShared {
    manager: Arc<CapabilityTokenManager>,
    storage: Option<Arc<dyn SessionStorage>>,
    origin: Instant,
    active: Mutex<ActiveCall>,
}

impl HostShared {
    fn lock(&self) -> MutexGuard<'_, ActiveCall> {
        self.active.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Checks the call's token for `scope` and this call's thread.
    fn authorize(&self, active: &ActiveCall, scope: &str) -> Result<(), HostRefusal> {
        let token = active.token.as_deref().ok_or(HostRefusal::Policy)?;
        self.manager
            .authorize(token, &active.thread_id, scope, None)
            .map(|_claims| ())
            .map_err(|_refused| HostRefusal::Policy)
    }

    /// Authorizes the call for `scope` and returns the thread its storage is keyed by.
    fn authorized_thread(&self, scope: &str) -> Result<String, HostRefusal> {
        let active = self.lock();
        self.authorize(&active, scope)?;
        Ok(active.thread_id.clone())
    }

    fn backend(&self) -> Result<&dyn SessionStorage, HostRefusal> {
        self.storage.as_deref().ok_or(HostRefusal::Dep)
    }

    fn begin(&self, call: &HostCall<'_>) {
        *self.lock() = ActiveCall::new(call);
    }

    /// Takes what the finished call recorded and clears its token from memory.
    fn finish(&self) -> CallRecord {
        let mut active = self.lock();
        let record = CallRecord {
            seed: active.seed,
            events: std::mem::take(&mut active.events),
        };
        *active = ActiveCall::default();
        record
    }
}

/// The four host imports as views of the shared state.
struct Imports<'a>(&'a HostShared);

impl log::Host for Imports<'_> {
    fn emit(&mut self, message: String) -> wasmtime::Result<()> {
        let mut active = self.0.lock();
        self.0.authorize(&active, LOG_SCOPE)?;
        active.admit_log(message.len())?;
        active.events.push(message);
        Ok(())
    }
}

impl time::Host for Imports<'_> {
    fn now(&mut self) -> wasmtime::Result<u64> {
        let active = self.0.lock();
        self.0.authorize(&active, TIME_SCOPE)?;
        Ok(u64::try_from(self.0.origin.elapsed().as_nanos()).unwrap_or(u64::MAX))
    }
}

impl rand::Host for Imports<'_> {
    fn next(&mut self) -> wasmtime::Result<u64> {
        let mut active = self.0.lock();
        self.0.authorize(&active, RAND_SCOPE)?;
        Ok(active.next_random())
    }
}

impl storage::Host for Imports<'_> {
    fn put(&mut self, key: String, value: String) -> wasmtime::Result<()> {
        let thread_id = self.0.authorized_thread(STORAGE_WRITE_SCOPE)?;
        let backend = self.0.backend()?;
        backend
            .put(&thread_id, &key, &value)
            .map_err(|_unavailable| HostRefusal::Dep)?;
        Ok(())
    }

    fn contains(&mut self, key: String) -> wasmtime::Result<bool> {
        let thread_id = self.0.authorized_thread(STORAGE_READ_SCOPE)?;
        let backend = self.0.backend()?;
        let present = backend
            .contains(&thread_id, &key)
            .map_err(|_unavailable| HostRefusal::Dep)?;
        Ok(present)
    }
}

/// Links the imports the host can serve. `storage` is linked only when `with_storage` is true.
fn link(
    linker: &mut Linker<GuestState>,
    shared: &Arc<HostShared>,
    with_storage: bool,
) -> Result<(), LoadError> {
    {
        let mut inst = linker
            .instance(LOG_INTERFACE)
            .map_err(|_| LoadError::Instantiate)?;
        let host = Arc::clone(shared);
        inst.func_wrap("emit", move |_, (message,): (String,)| {
            log::Host::emit(&mut Imports(&*host), message)
        })
        .map_err(|_| LoadError::Instantiate)?;
    }
    {
        let mut inst = linker
            .instance(TIME_INTERFACE)
            .map_err(|_| LoadError::Instantiate)?;
        let host = Arc::clone(shared);
        inst.func_wrap("now", move |_, (): ()| -> wasmtime::Result<(u64,)> {
            Ok((time::Host::now(&mut Imports(&*host))?,))
        })
        .map_err(|_| LoadError::Instantiate)?;
    }
    {
        let mut inst = linker
            .instance(RAND_INTERFACE)
            .map_err(|_| LoadError::Instantiate)?;
        let host = Arc::clone(shared);
        inst.func_wrap("next", move |_, (): ()| -> wasmtime::Result<(u64,)> {
            Ok((rand::Host::next(&mut Imports(&*host))?,))
        })
        .map_err(|_| LoadError::Instantiate)?;
    }
    if with_storage {
        let mut inst = linker
            .instance(STORAGE_INTERFACE)
            .map_err(|_| LoadError::Instantiate)?;
        let host = Arc::clone(shared);
        inst.func_wrap("put", move |_, (key, value): (String, String)| {
            storage::Host::put(&mut Imports(&*host), key, value)
        })
        .map_err(|_| LoadError::Instantiate)?;
        let host = Arc::clone(shared);
        inst.func_wrap(
            "contains",
            move |_, (key,): (String,)| -> wasmtime::Result<(bool,)> {
                Ok((storage::Host::contains(&mut Imports(&*host), key)?,))
            },
        )
        .map_err(|_| LoadError::Instantiate)?;
    }
    Ok(())
}

/// Builds guest instances that run with the host imports this bridge can serve.
pub struct HostBridge {
    manager: Arc<CapabilityTokenManager>,
    storage: Option<Arc<dyn SessionStorage>>,
}

impl HostBridge {
    /// A bridge that checks tokens with `manager`. `storage` is the session backend, if the host
    /// has one.
    pub fn new(
        manager: Arc<CapabilityTokenManager>,
        storage: Option<Arc<dyn SessionStorage>>,
    ) -> Self {
        Self { manager, storage }
    }

    /// Instantiates `guest` under `limits` with the imports this bridge can serve.
    ///
    /// A component that imports anything the bridge does not link, `omp-callback` or `storage`
    /// without a backend, fails with `E-DEP`.
    pub fn instantiate(
        &self,
        loader: &GuestLoader,
        guest: &LoadedGuest,
        limits: &Limits,
    ) -> Result<HostInstance, LoadError> {
        let shared = Arc::new(HostShared {
            manager: Arc::clone(&self.manager),
            storage: self.storage.clone(),
            origin: Instant::now(),
            active: Mutex::new(ActiveCall::default()),
        });
        let with_storage = self.storage.is_some();
        let instance =
            loader.instantiate_with(guest, limits, |linker| link(linker, &shared, with_storage))?;
        Ok(HostInstance { instance, shared })
    }
}

/// A guest instance linked by a [`HostBridge`].
pub struct HostInstance {
    instance: GuestInstance,
    shared: Arc<HostShared>,
}

impl HostInstance {
    /// Calls the guest export `export` under `call`. `results` must match the export's results.
    ///
    /// The call's seed and log events come back in the [`CallRecord`]. Each call re-arms fuel and
    /// the deadline from the limits this instance started with. A refused or trapped call leaves
    /// the instance unusable, so a caller starts a fresh instance after a failed call.
    pub fn call(
        &mut self,
        call: &HostCall<'_>,
        export: &str,
        results: &mut [Val],
    ) -> Result<CallRecord, HostCallError> {
        self.shared.begin(call);
        let outcome = self.run(export, results);
        let record = self.shared.finish();
        outcome.map(|()| record)
    }

    fn run(&mut self, export: &str, results: &mut [Val]) -> Result<(), HostCallError> {
        let deadline_configured = self.instance.deadline_configured();
        let limits = self.instance.limits();
        let (store, instance) = self.instance.parts();
        arm_call_budget(store, &limits)
            .map_err(|error| classify_host(&error, deadline_configured))?;
        let func = instance
            .get_func(&mut *store, export)
            .ok_or(HostCallError::Trap(ErrorCode::Internal))?;
        func.call(&mut *store, &[], results)
            .map_err(|error| classify_host(&error, deadline_configured))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wasmtime_module_build::{describe, MODULE_NAME};
    use gsd_security::SecurityConfig;
    use std::collections::HashMap;
    use std::time::Duration;

    const SEMVER: &str = "0.1.0";
    const SECRET: &str = "0123456789abcdef0123456789abcdef";
    const THREAD: &str = "thread-1";
    const SEED: u64 = 7;

    const LIMITS: Limits = Limits {
        fuel: 10_000_000,
        memory_bytes: 65536,
        deadline_ticks: None,
    };

    /// Imports `log`, `time` and `rand`. `emit` logs "hello". `flood` logs 10 000 eleven-byte
    /// events in one call, and `flood-empty` logs 100 001 empty events in one call. `now` and
    /// `next` return the time reading and the next random value.
    const CORE: &str = r#"(component
  (import "gsd:core-host/log@0.2.0" (instance $log (export "emit" (func (param "message" string)))))
  (import "gsd:core-host/time@0.2.0" (instance $time (export "now" (func (result u64)))))
  (import "gsd:core-host/rand@0.2.0" (instance $rand (export "next" (func (result u64)))))
  (core module $libc (memory (export "memory") 1))
  (core instance $libc_i (instantiate $libc))
  (alias core export $libc_i "memory" (core memory $mem))
  (alias export $log "emit" (func $emit))
  (alias export $time "now" (func $now))
  (alias export $rand "next" (func $next))
  (core func $emit_lower (canon lower (func $emit) (memory $mem)))
  (core func $now_lower (canon lower (func $now)))
  (core func $next_lower (canon lower (func $next)))
  (core module $m
    (import "host" "emit" (func $emit (param i32 i32)))
    (import "host" "now" (func $now (result i64)))
    (import "host" "next" (func $next (result i64)))
    (import "libc" "memory" (memory 1))
    (data (i32.const 0) "hello world")
    (func (export "emit") (call $emit (i32.const 0) (i32.const 5)))
    (func (export "flood")
      (local $i i32)
      (block $done
        (loop $again
          (br_if $done (i32.ge_u (local.get $i) (i32.const 10000)))
          (call $emit (i32.const 0) (i32.const 11))
          (local.set $i (i32.add (local.get $i) (i32.const 1)))
          (br $again))))
    (func (export "flood_empty")
      (local $i i32)
      (block $done
        (loop $again
          (br_if $done (i32.ge_u (local.get $i) (i32.const 100001)))
          (call $emit (i32.const 0) (i32.const 0))
          (local.set $i (i32.add (local.get $i) (i32.const 1)))
          (br $again))))
    (func (export "now") (result i64) (call $now))
    (func (export "next") (result i64) (call $next)))
  (core instance $m_i (instantiate $m
    (with "host" (instance
      (export "emit" (func $emit_lower))
      (export "now" (func $now_lower))
      (export "next" (func $next_lower))))
    (with "libc" (instance $libc_i))))
  (func (export "emit") (canon lift (core func $m_i "emit")))
  (func (export "flood") (canon lift (core func $m_i "flood")))
  (func (export "flood-empty") (canon lift (core func $m_i "flood_empty")))
  (func (export "now") (result u64) (canon lift (core func $m_i "now")))
  (func (export "next") (result u64) (canon lift (core func $m_i "next"))))"#;

    /// Imports `storage`. `put` stores "v" under "k". `contains` asks for "k".
    const STORAGE: &str = r#"(component
  (import "gsd:core-host/storage@0.2.0" (instance $storage
    (export "put" (func (param "key" string) (param "value" string)))
    (export "contains" (func (param "key" string) (result bool)))))
  (core module $libc (memory (export "memory") 1))
  (core instance $libc_i (instantiate $libc))
  (alias core export $libc_i "memory" (core memory $mem))
  (alias export $storage "put" (func $put))
  (alias export $storage "contains" (func $contains))
  (core func $put_lower (canon lower (func $put) (memory $mem)))
  (core func $contains_lower (canon lower (func $contains) (memory $mem)))
  (core module $m
    (import "host" "put" (func $put (param i32 i32 i32 i32)))
    (import "host" "contains" (func $contains (param i32 i32) (result i32)))
    (import "libc" "memory" (memory 1))
    (data (i32.const 0) "k")
    (data (i32.const 1) "v")
    (func (export "put") (call $put (i32.const 0) (i32.const 1) (i32.const 1) (i32.const 1)))
    (func (export "contains") (result i32) (call $contains (i32.const 0) (i32.const 1))))
  (core instance $m_i (instantiate $m
    (with "host" (instance
      (export "put" (func $put_lower))
      (export "contains" (func $contains_lower))))
    (with "libc" (instance $libc_i))))
  (func (export "put") (canon lift (core func $m_i "put")))
  (func (export "contains") (result bool) (canon lift (core func $m_i "contains"))))"#;

    /// Imports `omp-callback`, which the host bridge does not link.
    const OMP_CALLBACK: &str = r#"(component
  (import "gsd:core-host/omp-callback@0.2.0" (instance (export "call" (func)))))"#;

    #[derive(Default)]
    struct MemoryStorage {
        values: Mutex<HashMap<(String, String), String>>,
    }

    impl SessionStorage for MemoryStorage {
        fn put(&self, thread_id: &str, key: &str, value: &str) -> Result<(), StorageUnavailable> {
            let mut values = self.values.lock().unwrap_or_else(PoisonError::into_inner);
            values.insert((thread_id.to_owned(), key.to_owned()), value.to_owned());
            Ok(())
        }

        fn contains(&self, thread_id: &str, key: &str) -> Result<bool, StorageUnavailable> {
            let values = self.values.lock().unwrap_or_else(PoisonError::into_inner);
            Ok(values.contains_key(&(thread_id.to_owned(), key.to_owned())))
        }
    }

    struct DownStorage;

    impl SessionStorage for DownStorage {
        fn put(&self, _: &str, _: &str, _: &str) -> Result<(), StorageUnavailable> {
            Err(StorageUnavailable)
        }

        fn contains(&self, _: &str, _: &str) -> Result<bool, StorageUnavailable> {
            Err(StorageUnavailable)
        }
    }

    fn token_manager(ttl_secs: u64) -> Arc<CapabilityTokenManager> {
        let mut config = SecurityConfig::new(SECRET, "").expect("test secret is valid");
        config.capability_token_ttl_secs = ttl_secs;
        Arc::new(CapabilityTokenManager::new(config).expect("test config is valid"))
    }

    fn issue(manager: &CapabilityTokenManager, thread: &str, scopes: &[&str]) -> String {
        let scopes = scopes.iter().map(|scope| (*scope).to_owned()).collect();
        manager
            .issue(thread, scopes, Vec::new())
            .expect("token issues")
    }

    fn instance(bridge: &HostBridge, text: &str) -> HostInstance {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = wat::parse_str(text).expect("test WAT parses");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        bridge
            .instantiate(&loader, &guest, &LIMITS)
            .expect("component instantiates")
    }

    fn call(
        instance: &mut HostInstance,
        token: Option<&str>,
        seed: u64,
        export: &str,
        results: &mut [Val],
    ) -> Result<CallRecord, HostCallError> {
        instance.call(
            &HostCall {
                thread_id: THREAD,
                token,
                seed,
            },
            export,
            results,
        )
    }

    fn scalar_u64(results: &[Val]) -> u64 {
        match results {
            [Val::U64(value)] => *value,
            _ => panic!("the export returns one u64"),
        }
    }

    fn policy_refusal() -> Result<CallRecord, HostCallError> {
        Err(HostCallError::Refused(ErrorCode::Policy))
    }

    #[test]
    fn log_emits_its_event_with_a_valid_token() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let token = issue(&manager, THREAD, &[LOG_SCOPE]);
        let mut inst = instance(&bridge, CORE);
        let record = call(&mut inst, Some(&token), SEED, "emit", &mut [])
            .expect("log accepts a valid token");
        assert_eq!(record.events, vec!["hello".to_owned()]);
        assert_eq!(record.seed, SEED);
    }

    #[test]
    fn time_reads_the_host_clock_with_a_valid_token() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let token = issue(&manager, THREAD, &[TIME_SCOPE]);
        let mut inst = instance(&bridge, CORE);
        let mut first = [Val::U64(0)];
        call(&mut inst, Some(&token), SEED, "now", &mut first).expect("time accepts a valid token");
        let mut second = [Val::U64(0)];
        call(&mut inst, Some(&token), SEED, "now", &mut second)
            .expect("time accepts a valid token");
        assert!(scalar_u64(&second) >= scalar_u64(&first));
    }

    #[test]
    fn rand_is_a_function_of_the_recorded_seed() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let token = issue(&manager, THREAD, &[RAND_SCOPE]);
        let draw = |seed: u64| {
            let mut inst = instance(&bridge, CORE);
            let mut results = [Val::U64(0)];
            let record = call(&mut inst, Some(&token), seed, "next", &mut results)
                .expect("rand accepts a valid token");
            assert_eq!(record.seed, seed);
            scalar_u64(&results)
        };
        assert_eq!(draw(SEED), draw(SEED));
        assert_ne!(draw(SEED), draw(SEED + 1));
    }

    #[test]
    fn a_missing_token_is_refused_with_policy() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(manager, None);
        let mut emit = instance(&bridge, CORE);
        assert_eq!(
            call(&mut emit, None, SEED, "emit", &mut []),
            policy_refusal()
        );
        let mut now = instance(&bridge, CORE);
        assert_eq!(
            call(&mut now, None, SEED, "now", &mut [Val::U64(0)]),
            policy_refusal()
        );
        let mut next = instance(&bridge, CORE);
        assert_eq!(
            call(&mut next, None, SEED, "next", &mut [Val::U64(0)]),
            policy_refusal()
        );
    }

    #[test]
    fn a_wrong_scope_is_refused_with_policy() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let time_only = issue(&manager, THREAD, &[TIME_SCOPE]);
        let mut emit = instance(&bridge, CORE);
        assert_eq!(
            call(&mut emit, Some(&time_only), SEED, "emit", &mut []),
            policy_refusal()
        );
        let log_only = issue(&manager, THREAD, &[LOG_SCOPE]);
        let mut now = instance(&bridge, CORE);
        assert_eq!(
            call(&mut now, Some(&log_only), SEED, "now", &mut [Val::U64(0)]),
            policy_refusal()
        );
    }

    #[test]
    fn a_token_issued_for_another_thread_is_refused_with_policy() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let other = issue(&manager, "thread-2", &[LOG_SCOPE]);
        let mut inst = instance(&bridge, CORE);
        assert_eq!(
            call(&mut inst, Some(&other), SEED, "emit", &mut []),
            policy_refusal()
        );
    }

    #[test]
    fn an_expired_token_is_refused_with_policy() {
        let manager = token_manager(1);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let token = issue(&manager, THREAD, &[LOG_SCOPE]);
        std::thread::sleep(Duration::from_secs(2));
        let mut inst = instance(&bridge, CORE);
        assert_eq!(
            call(&mut inst, Some(&token), SEED, "emit", &mut []),
            policy_refusal()
        );
    }

    #[test]
    fn a_log_flood_in_one_call_is_refused_with_contract() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let token = issue(&manager, THREAD, &[LOG_SCOPE]);
        let mut inst = instance(&bridge, CORE);
        assert_eq!(
            call(&mut inst, Some(&token), SEED, "flood", &mut []),
            Err(HostCallError::Refused(ErrorCode::Contract))
        );
    }

    #[test]
    fn empty_events_count_against_the_batch_cap() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), None);
        let token = issue(&manager, THREAD, &[LOG_SCOPE]);
        let mut inst = instance(&bridge, CORE);
        assert_eq!(
            call(&mut inst, Some(&token), SEED, "flood-empty", &mut []),
            Err(HostCallError::Refused(ErrorCode::Contract))
        );
    }

    #[test]
    fn log_admits_events_up_to_the_batch_cap() {
        let mut active = ActiveCall::default();
        assert_eq!(active.admit_log(LOG_BATCH_CAP_BYTES), Ok(()));
        assert_eq!(active.admit_log(1), Err(HostRefusal::Contract));
    }

    #[test]
    fn omp_callback_fails_with_dep_at_instantiation() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(manager, None);
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = wat::parse_str(OMP_CALLBACK).expect("test WAT parses");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        match bridge.instantiate(&loader, &guest, &LIMITS) {
            Err(err) => assert_eq!(err.error_code(), ErrorCode::Dep),
            Ok(_) => panic!("omp-callback must not be linked"),
        }
    }

    #[test]
    fn storage_without_a_backend_fails_with_dep_at_instantiation() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(manager, None);
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = wat::parse_str(STORAGE).expect("test WAT parses");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        match bridge.instantiate(&loader, &guest, &LIMITS) {
            Err(err) => assert_eq!(err.error_code(), ErrorCode::Dep),
            Ok(_) => panic!("storage must not be linked without a backend"),
        }
    }

    #[test]
    fn storage_stores_and_reads_within_the_threads_namespace() {
        let manager = token_manager(600);
        let backend = Arc::new(MemoryStorage::default());
        let bridge = HostBridge::new(Arc::clone(&manager), Some(backend.clone()));
        let writer = issue(&manager, THREAD, &[STORAGE_WRITE_SCOPE, STORAGE_READ_SCOPE]);
        let mut put = instance(&bridge, STORAGE);
        call(&mut put, Some(&writer), SEED, "put", &mut []).expect("put accepts a valid token");
        assert_eq!(backend.contains(THREAD, "k"), Ok(true));

        let mut read = instance(&bridge, STORAGE);
        let mut found = [Val::Bool(false)];
        call(&mut read, Some(&writer), SEED, "contains", &mut found)
            .expect("contains accepts a valid token");
        assert!(matches!(found.as_slice(), [Val::Bool(true)]));

        let other = issue(&manager, "thread-2", &[STORAGE_READ_SCOPE]);
        let mut other_read = instance(&bridge, STORAGE);
        let mut absent = [Val::Bool(true)];
        other_read
            .call(
                &HostCall {
                    thread_id: "thread-2",
                    token: Some(&other),
                    seed: SEED,
                },
                "contains",
                &mut absent,
            )
            .expect("contains accepts a valid token for its own thread");
        assert!(matches!(absent.as_slice(), [Val::Bool(false)]));
    }

    #[test]
    fn storage_put_without_the_write_scope_is_refused_with_policy() {
        let manager = token_manager(600);
        let backend = Arc::new(MemoryStorage::default());
        let bridge = HostBridge::new(Arc::clone(&manager), Some(backend));
        let reader = issue(&manager, THREAD, &[STORAGE_READ_SCOPE]);
        let mut inst = instance(&bridge, STORAGE);
        assert_eq!(
            call(&mut inst, Some(&reader), SEED, "put", &mut []),
            policy_refusal()
        );
    }

    #[test]
    fn a_storage_backend_fault_is_refused_with_dep() {
        let manager = token_manager(600);
        let bridge = HostBridge::new(Arc::clone(&manager), Some(Arc::new(DownStorage)));
        let writer = issue(&manager, THREAD, &[STORAGE_WRITE_SCOPE]);
        let mut inst = instance(&bridge, STORAGE);
        assert_eq!(
            call(&mut inst, Some(&writer), SEED, "put", &mut []),
            Err(HostCallError::Refused(ErrorCode::Dep))
        );
    }
}
