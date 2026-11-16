//! Throwaway Python-call shims under the clean-cutover rule.
//!
//! A shim is a temporary Rust-side call into the Python engine that exists only while its route is
//! being cut over. Each shim is registered with the caller it serves. Under the clean-cutover rule,
//! every caller migrates to the Rust route, then the shim is removed. No dual path survives a
//! cutover. A shim with no caller serves nothing, so [`LegacyShimRegistry::unowned`] reports it for
//! removal. The registry is a value type and never calls Python itself.

use std::collections::BTreeMap;

use gsd_failure::{ErrorCode, FailureError};

/// Registry of throwaway Python-call shims, keyed by shim name.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LegacyShimRegistry {
    shims: BTreeMap<String, Option<String>>,
}

impl LegacyShimRegistry {
    /// Creates an empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers `shim` as serving `caller`. A missing or blank caller registers the shim without an
    /// owner, so [`Self::unowned`] reports it. Returns `E-INPUT` for a blank or already registered
    /// shim name.
    pub fn register(&mut self, shim: &str, caller: Option<&str>) -> Result<(), FailureError> {
        let name = shim.trim();
        if name.is_empty() {
            return Err(FailureError::new(
                ErrorCode::Input,
                "legacy shim name must not be blank",
            ));
        }
        if self.shims.contains_key(name) {
            return Err(FailureError::new(
                ErrorCode::Input,
                format!("legacy shim {name} is already registered"),
            ));
        }
        let caller = caller
            .map(str::trim)
            .filter(|c| !c.is_empty())
            .map(str::to_owned);
        self.shims.insert(name.to_owned(), caller);
        Ok(())
    }

    /// Removes `shim` at its cutover. Returns true when it was registered.
    pub fn remove(&mut self, shim: &str) -> bool {
        self.shims.remove(shim.trim()).is_some()
    }

    /// Returns every registered shim name in sorted order.
    pub fn shims(&self) -> Vec<&str> {
        self.shims.keys().map(String::as_str).collect()
    }

    /// Returns the caller that `shim` serves, or `None` when the shim is unregistered or has no caller.
    pub fn caller_of(&self, shim: &str) -> Option<&str> {
        self.shims
            .get(shim.trim())
            .and_then(|caller| caller.as_deref())
    }

    /// Returns the registered shims that have no caller, in sorted order. Each must be removed.
    pub fn unowned(&self) -> Vec<&str> {
        self.shims
            .iter()
            .filter(|(_, caller)| caller.is_none())
            .map(|(name, _)| name.as_str())
            .collect()
    }

    /// Returns the number of registered shims.
    pub fn len(&self) -> usize {
        self.shims.len()
    }

    /// Returns true when no shim is registered.
    pub fn is_empty(&self) -> bool {
        self.shims.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_lists_every_shim_it_holds() {
        let mut registry = LegacyShimRegistry::new();
        assert!(registry
            .register("swarm_tool_catalog_py", Some("swarm_tool"))
            .is_ok());
        assert!(registry
            .register("orch_status_py", Some("orch_status"))
            .is_ok());
        assert_eq!(
            registry.shims(),
            vec!["orch_status_py", "swarm_tool_catalog_py"]
        );
        assert_eq!(registry.len(), 2);
        assert_eq!(registry.caller_of("orch_status_py"), Some("orch_status"));
        assert_eq!(registry.caller_of("missing_py"), None);
        assert!(registry.unowned().is_empty());
    }

    #[test]
    fn shim_without_caller_is_reported() {
        let mut registry = LegacyShimRegistry::new();
        assert!(registry.register("legacy_plan_py", None).is_ok());
        assert!(registry.register("legacy_gate_py", Some("   ")).is_ok());
        assert!(registry
            .register("legacy_assign_py", Some("orch_assign"))
            .is_ok());
        assert_eq!(registry.unowned(), vec!["legacy_gate_py", "legacy_plan_py"]);
        assert_eq!(registry.caller_of("legacy_gate_py"), None);
    }

    #[test]
    fn blank_or_duplicate_shim_name_is_rejected() {
        let mut registry = LegacyShimRegistry::new();
        let blank = registry.register("  ", Some("caller")).err();
        assert_eq!(blank.map(|e| e.code()), Some(ErrorCode::Input));
        assert!(registry.register("dup_py", Some("a")).is_ok());
        let dup = registry.register("dup_py", Some("b")).err();
        assert_eq!(dup.map(|e| e.code()), Some(ErrorCode::Input));
        assert_eq!(registry.caller_of("dup_py"), Some("a"));
    }

    #[test]
    fn cutover_removes_shim_and_leaves_registry_empty() {
        let mut registry = LegacyShimRegistry::new();
        assert!(registry.is_empty());
        assert!(registry
            .register("orch_status_py", Some("orch_status"))
            .is_ok());
        assert!(registry.register("swarm_tool_catalog_py", None).is_ok());
        assert!(registry.remove("orch_status_py"));
        assert!(!registry.remove("orch_status_py"));
        assert!(registry.remove("swarm_tool_catalog_py"));
        assert!(registry.is_empty());
        assert_eq!(registry.len(), 0);
        assert!(registry.unowned().is_empty());
    }
}
