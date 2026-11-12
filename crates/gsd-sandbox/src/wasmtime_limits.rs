//! Per-call resource limits for the guest store: fuel, memory and the epoch deadline (D-P5-12).
//!
//! Fuel exhaustion maps to `E-INTERNAL` when no deadline is configured and to `E-TIMEOUT` when
//! one is. An epoch deadline trap maps to `E-TIMEOUT`. A memory growth past the cap is refused
//! as a trap carrying [`CapacityRefused`], which maps to `E-CAPACITY`.

use std::time::Duration;

use gsd_failure::ErrorCode;
use wasmtime::{ResourceLimiter, StoreLimits, StoreLimitsBuilder, Trap};

use crate::wasmtime_module_load::GuestState;

/// WebAssembly linear memory page size in bytes.
const PAGE_BYTES: usize = 65536;

/// One epoch tick: the epoch advances once per slice. A deadline of `n` ticks can trap one slice
/// early, so [`deadline_ticks_for`] adds a tick to cover it.
pub const EPOCH_SLICE: Duration = Duration::from_millis(500);

/// Resource limits applied to one guest store before instantiation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Fuel units available to the guest; must be at least 1.
    pub fuel: u64,
    /// Linear memory cap in bytes; must be a positive multiple of 65536.
    pub memory_bytes: usize,
    /// Epoch ticks until the deadline traps, one per [`EPOCH_SLICE`]. `None` means no deadline.
    /// When `Some`, at least 1.
    pub deadline_ticks: Option<u64>,
}

/// The number of [`EPOCH_SLICE`] ticks whose trap cannot land before `deadline` has elapsed.
///
/// The ticker is free-running, so the first tick after a call starts can arrive almost at once:
/// `n` ticks can trap after `n - 1` slices. Covering `deadline` takes `ceil(deadline / slice)`
/// slices, and one more tick absorbs the early tick. The division runs on nanoseconds, so no
/// sub-millisecond part of `deadline` is dropped. A zero deadline still gets one tick.
pub fn deadline_ticks_for(deadline: Duration) -> u64 {
    let ticks = deadline
        .as_nanos()
        .div_ceil(EPOCH_SLICE.as_nanos())
        .saturating_add(1);
    u64::try_from(ticks).unwrap_or(u64::MAX)
}

/// Refusals raised while validating [`Limits`].
#[derive(Debug, thiserror::Error)]
pub enum LimitsError {
    /// A limit value is outside its allowed range.
    #[error("guest limits are invalid: {0}")]
    Invalid(&'static str),
}

impl LimitsError {
    /// Maps every limits refusal to the shared `E-INPUT` code.
    pub fn error_code(&self) -> ErrorCode {
        match self {
            Self::Invalid(_) => ErrorCode::Input,
        }
    }
}

impl Limits {
    /// Refuses fuel of 0, a memory cap that is not a positive page multiple, or a zero deadline.
    pub fn validate(&self) -> Result<(), LimitsError> {
        if self.fuel < 1 {
            return Err(LimitsError::Invalid("fuel must be at least 1"));
        }
        if self.memory_bytes == 0 || !self.memory_bytes.is_multiple_of(PAGE_BYTES) {
            return Err(LimitsError::Invalid(
                "memory must be a positive multiple of 65536 bytes",
            ));
        }
        if self.deadline_ticks == Some(0) {
            return Err(LimitsError::Invalid("deadline ticks must be at least 1"));
        }
        Ok(())
    }
}

/// The store's resource limiter: the configured [`StoreLimits`], with a refused memory growth raised
/// as a trap carrying [`CapacityRefused`]. A raw `memory.grow` returns -1 on refusal, and the S-04
/// measurement recorded that -1 under `StoreLimits` alone; a guest can ignore -1 but cannot ignore a
/// trap.
#[derive(Default)]
pub(crate) struct GuestLimiter {
    inner: StoreLimits,
}

/// A memory growth the store's limits refused. A guest that grows past its cap traps with this
/// error, so the refusal cannot be ignored.
#[derive(Debug, thiserror::Error)]
#[error("guest memory growth exceeds the store's memory cap")]
pub struct CapacityRefused;

impl ResourceLimiter for GuestLimiter {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        if self.inner.memory_growing(current, desired, maximum)? {
            Ok(true)
        } else {
            Err(CapacityRefused.into())
        }
    }

    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.inner.table_growing(current, desired, maximum)
    }

    fn memory_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        self.inner.memory_grow_failed(error)
    }

    fn table_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        self.inner.table_grow_failed(error)
    }

    fn instances(&self) -> usize {
        self.inner.instances()
    }

    fn tables(&self) -> usize {
        self.inner.tables()
    }

    fn memories(&self) -> usize {
        self.inner.memories()
    }
}

/// The most tables one guest store may create.
const MAX_TABLES: usize = 8;
/// The most elements one guest table may hold, at instantiation and after growth.
const MAX_TABLE_ELEMENTS: usize = 100_000;
/// The most instances one guest store may create; the guest component has one core module.
const MAX_INSTANCES: usize = 8;

/// Installs the memory cap, the table and instance caps and the growth limiter on `store`, then
/// arms the first call's budget.
pub(crate) fn apply(
    store: &mut wasmtime::Store<GuestState>,
    limits: &Limits,
) -> wasmtime::Result<()> {
    store.data_mut().limits = GuestLimiter {
        inner: StoreLimitsBuilder::new()
            .memory_size(limits.memory_bytes)
            .tables(MAX_TABLES)
            .table_elements(MAX_TABLE_ELEMENTS)
            .instances(MAX_INSTANCES)
            .build(),
    };
    store.limiter(|state| &mut state.limits);
    arm_call_budget(store, limits)
}

/// Gives the next call the full fuel and epoch deadline of `limits`.
///
/// The epoch deadline is always set: a store with epoch interruption traps immediately unless a
/// deadline is given. `None` uses a deadline that is never reached. Fuel and the deadline are per
/// call, so an instance that serves many calls starts each one with a fresh budget.
pub(crate) fn arm_call_budget(
    store: &mut wasmtime::Store<GuestState>,
    limits: &Limits,
) -> wasmtime::Result<()> {
    store.set_fuel(limits.fuel)?;
    store.set_epoch_deadline(limits.deadline_ticks.unwrap_or(u64::MAX));
    Ok(())
}

/// Maps a guest call error to its error code without carrying the trap message.
///
/// A refused memory growth ([`CapacityRefused`]) is `E-CAPACITY`. `OutOfFuel` is `E-TIMEOUT` when a
/// deadline is configured and `E-INTERNAL` otherwise. An epoch `Interrupt` is `E-TIMEOUT`. Every
/// other error is `E-INTERNAL`.
pub fn classify(error: &wasmtime::Error, deadline_configured: bool) -> ErrorCode {
    if error.downcast_ref::<CapacityRefused>().is_some() {
        return ErrorCode::Capacity;
    }
    match error.downcast_ref::<Trap>() {
        Some(Trap::OutOfFuel) if deadline_configured => ErrorCode::Timeout,
        Some(Trap::OutOfFuel) => ErrorCode::Internal,
        Some(Trap::Interrupt) => ErrorCode::Timeout,
        _ => ErrorCode::Internal,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wasmtime_module_build::{describe, MODULE_NAME};
    use crate::wasmtime_module_load::{GuestInstance, GuestLoader};
    use wasmtime::component::Val;

    const SEMVER: &str = "0.1.0";

    const SPIN: &str = r#"(component
        (core module $m (func (export "spin") (loop $l (br $l))))
        (core instance $i (instantiate $m))
        (func (export "spin") (canon lift (core func $i "spin"))))"#;

    fn start(text: &str, limits: &Limits) -> GuestInstance {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = wat::parse_str(text).expect("test WAT parses");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        loader
            .instantiate(&guest, limits)
            .expect("component instantiates")
    }

    fn call(instance: &mut GuestInstance, name: &str, results: &mut [Val]) -> wasmtime::Result<()> {
        let (store, inst) = instance.parts();
        let func = inst.get_func(&mut *store, name).expect("export exists");
        func.call(&mut *store, &[], results)
    }

    fn spin_limits(fuel: u64, deadline_ticks: Option<u64>) -> Limits {
        Limits {
            fuel,
            memory_bytes: PAGE_BYTES,
            deadline_ticks,
        }
    }

    #[test]
    fn limits_refuse_zero_fuel_and_unaligned_memory() {
        let no_fuel = Limits {
            fuel: 0,
            memory_bytes: PAGE_BYTES,
            deadline_ticks: None,
        };
        let unaligned = Limits {
            fuel: 1,
            memory_bytes: 100,
            deadline_ticks: None,
        };
        assert!(matches!(no_fuel.validate(), Err(LimitsError::Invalid(_))));
        assert!(matches!(unaligned.validate(), Err(LimitsError::Invalid(_))));
    }

    #[test]
    fn fuel_exhaustion_without_a_deadline_maps_to_internal() {
        let limits = spin_limits(10_000, None);
        let mut instance = start(SPIN, &limits);
        let err = call(&mut instance, "spin", &mut []).expect_err("spin exhausts fuel");
        assert_eq!(classify(&err, false), ErrorCode::Internal);
    }

    #[test]
    fn fuel_exhaustion_with_a_deadline_maps_to_timeout() {
        let limits = spin_limits(10_000, Some(1));
        let mut instance = start(SPIN, &limits);
        let err = call(&mut instance, "spin", &mut []).expect_err("spin exhausts fuel");
        assert_eq!(classify(&err, true), ErrorCode::Timeout);
    }

    #[test]
    fn epoch_deadline_maps_to_timeout() {
        let limits = spin_limits(u64::MAX / 2, Some(1));
        let mut instance = start(SPIN, &limits);
        {
            let (store, _) = instance.parts();
            store.engine().increment_epoch();
        }
        let err = call(&mut instance, "spin", &mut []).expect_err("spin passes the deadline");
        assert_eq!(classify(&err, true), ErrorCode::Timeout);
    }

    #[test]
    fn every_limits_error_maps_to_input() {
        assert_eq!(LimitsError::Invalid("x").error_code(), ErrorCode::Input);
    }

    #[test]
    fn deadline_ticks_never_trap_before_the_deadline() {
        let slice = EPOCH_SLICE.as_nanos();
        let durations = [
            Duration::ZERO,
            Duration::from_nanos(1),
            Duration::from_millis(1),
            Duration::from_millis(499),
            Duration::from_millis(500),
            Duration::from_micros(1_000_900),
            Duration::from_secs(1),
            Duration::from_secs(3),
        ];
        let swept = (0..2_000u32).map(|step| Duration::from_micros(u64::from(step) * 1_003));
        for deadline in durations.into_iter().chain(swept) {
            let ticks = u128::from(deadline_ticks_for(deadline));
            let elapsed = deadline.as_nanos();
            assert!(ticks * slice >= elapsed, "{deadline:?} gets {ticks} ticks");
            assert!(
                ticks.saturating_sub(1) * slice >= elapsed,
                "{deadline:?} gets {ticks} ticks, one of which may trap early"
            );
        }
    }

    #[test]
    fn ticker_stops_a_spinning_guest_at_its_deadline() {
        // Fuel is far above what a few seconds of spinning burns, so only the epoch deadline can
        // stop the call in time. Without the ticker the call would run out of fuel instead.
        let deadline = deadline_ticks_for(Duration::from_secs(1));
        let limits = spin_limits(100_000_000_000, Some(deadline));
        let mut instance = start(SPIN, &limits);
        let started = std::time::Instant::now();
        let err = call(&mut instance, "spin", &mut []).expect_err("the deadline stops the spin");
        let elapsed = started.elapsed();
        assert!(
            matches!(err.downcast_ref::<Trap>(), Some(Trap::Interrupt)),
            "the epoch ticker must interrupt the call, got {err}"
        );
        assert!(
            elapsed < Duration::from_secs(3),
            "stopped after {elapsed:?}"
        );
    }

    #[test]
    fn growth_past_the_cap_traps_as_capacity_refused() {
        let loader = crate::wasmtime_module_load::GuestLoader::new().expect("loader builds");
        let mut store = wasmtime::Store::new(
            loader.engine(),
            GuestState {
                limits: GuestLimiter::default(),
            },
        );
        apply(
            &mut store,
            &Limits {
                fuel: 1_000_000,
                memory_bytes: 2 * PAGE_BYTES,
                deadline_ticks: None,
            },
        )
        .expect("limits apply");
        let wasm = wat::parse_str(
        r#"(module (memory 1) (func (export "grow") (param i32) (result i32) (memory.grow (local.get 0))))"#,
    )
    .expect("wat parses");
        let module = wasmtime::Module::new(loader.engine(), &wasm).expect("module compiles");
        let instance =
            wasmtime::Instance::new(&mut store, &module, &[]).expect("module instantiates");
        let grow = instance
            .get_typed_func::<i32, i32>(&mut store, "grow")
            .expect("grow is exported");
        assert_eq!(
            grow.call(&mut store, 1)
                .expect("growth to the cap succeeds"),
            1
        );
        let error = grow
            .call(&mut store, 1)
            .expect_err("growth past the cap is refused");
        assert!(
            error.downcast_ref::<CapacityRefused>().is_some(),
            "{error:?}"
        );
        assert_eq!(classify(&error, false), ErrorCode::Capacity);
    }

    fn table_component(elements: usize) -> String {
        format!(
            r#"(component
        (core module $m (table (export "t") {elements} funcref))
        (core instance $i (instantiate $m)))"#
        )
    }

    #[test]
    fn tables_past_the_element_cap_are_refused_at_instantiation() {
        let loader = GuestLoader::new().expect("loader builds");
        let limits = spin_limits(1_000_000, None);
        for (elements, fits) in [(16, true), (MAX_TABLE_ELEMENTS + 1, false)] {
            let text = table_component(elements);
            let bytes = wat::parse_str(&text).expect("test WAT parses");
            let manifest = describe(&bytes, MODULE_NAME, SEMVER);
            let guest = loader
                .load_verified(&bytes, &manifest, SEMVER)
                .expect("verified component loads");
            let instantiated = loader.instantiate(&guest, &limits).is_ok();
            assert_eq!(instantiated, fits, "a table of {elements} elements");
        }
    }
}
