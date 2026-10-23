//! Per-call resource limits for the guest store: fuel, memory and the epoch deadline (D-P5-12).
//!
//! Fuel exhaustion maps to `E-INTERNAL` when no deadline is configured and to `E-TIMEOUT` when
//! one is. An epoch deadline trap maps to `E-TIMEOUT`. Memory-grow refusal is not mapped here.

use gsd_failure::ErrorCode;
use wasmtime::{StoreLimitsBuilder, Trap};

use crate::wasmtime_module_load::GuestState;

/// WebAssembly linear memory page size in bytes.
const PAGE_BYTES: usize = 65536;

/// Resource limits applied to one guest store before instantiation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Fuel units available to the guest; must be at least 1.
    pub fuel: u64,
    /// Linear memory cap in bytes; must be a positive multiple of 65536.
    pub memory_bytes: usize,
    /// Epoch ticks until the deadline traps; `None` means no deadline; when `Some`, at least 1.
    pub deadline_ticks: Option<u64>,
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
        if self.memory_bytes == 0 || self.memory_bytes % PAGE_BYTES != 0 {
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

/// Applies fuel, the memory cap and the epoch deadline to `store`.
///
/// The epoch deadline is always set: a store with epoch interruption traps immediately unless a
/// deadline is given. `None` uses a deadline that is never reached.
pub fn apply(store: &mut wasmtime::Store<GuestState>, limits: &Limits) -> wasmtime::Result<()> {
    store.set_fuel(limits.fuel)?;
    store.data_mut().limits = StoreLimitsBuilder::new()
        .memory_size(limits.memory_bytes)
        .build();
    store.limiter(|state| &mut state.limits);
    store.set_epoch_deadline(limits.deadline_ticks.unwrap_or(u64::MAX));
    Ok(())
}

/// Maps a guest call error to its error code without carrying the trap message.
///
/// `OutOfFuel` is `E-TIMEOUT` when a deadline is configured and `E-INTERNAL` otherwise. An epoch
/// `Interrupt` is `E-TIMEOUT`. Every other error is `E-INTERNAL`.
pub fn classify(error: &wasmtime::Error, deadline_configured: bool) -> ErrorCode {
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

    const GROW: &str = r#"(component
        (core module $m
            (memory (export "memory") 1)
            (func (export "grow") (result i32) (memory.grow (i32.const 100))))
        (core instance $i (instantiate $m))
        (func (export "grow") (result s32) (canon lift (core func $i "grow"))))"#;

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
    fn memory_growth_beyond_the_cap_is_refused() {
        let limits = Limits {
            fuel: 1_000_000,
            memory_bytes: 2 * PAGE_BYTES,
            deadline_ticks: None,
        };
        let mut instance = start(GROW, &limits);
        let mut results = [Val::S32(0)];
        call(&mut instance, "grow", &mut results).expect("grow call returns");
        assert!(matches!(results[0], Val::S32(-1)));
    }

    #[test]
    fn every_limits_error_maps_to_input() {
        assert_eq!(LimitsError::Invalid("x").error_code(), ErrorCode::Input);
    }
}
