//! The GSD-Core guest: the `gsd:core@0.1.0` world, exporting `invoke` over the pure route logic.
//!
//! The guest has no filesystem, network or clock access. Its only input is the request bytes the
//! host passes to `invoke`; every failure is returned as an `invoke-error`, never a trap.

wit_bindgen::generate!({
    path: "wit",
    world: "gsd-core",
});

struct GsdCore;

impl Guest for GsdCore {
    fn invoke(input: Vec<u8>) -> Result<Vec<u8>, InvokeError> {
        // Default build: the read-only `orch_status` route answers every request. `write-routes` build: `core_invoke` first
        // sends a `transition` request to the transition decision and hands every other request to the same read route.
        #[cfg(not(feature = "write-routes"))]
        let reply = gsd_route::orch_status_invoke(&input);
        #[cfg(feature = "write-routes")]
        let reply = gsd_route::core_invoke(&input);
        reply.map_err(|error| InvokeError {
            code: wit_code(error.code()),
            message: error.message().to_owned(),
        })
    }
}

/// Maps one taxonomy code to its WIT counterpart; every code has exactly one arm.
fn wit_code(code: gsd_codes::ErrorCode) -> ErrorCode {
    match code {
        gsd_codes::ErrorCode::Input => ErrorCode::Input,
        gsd_codes::ErrorCode::Timeout => ErrorCode::Timeout,
        gsd_codes::ErrorCode::Dep => ErrorCode::Dep,
        gsd_codes::ErrorCode::Capacity => ErrorCode::Capacity,
        gsd_codes::ErrorCode::Contract => ErrorCode::Contract,
        gsd_codes::ErrorCode::Policy => ErrorCode::Policy,
        gsd_codes::ErrorCode::Internal => ErrorCode::Internal,
    }
}

export!(GsdCore);
