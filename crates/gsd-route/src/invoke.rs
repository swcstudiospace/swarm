//! The guest entry of the write-enabled build: [`core_invoke`] dispatches one request by its `mode`.
//!
//! The read modes (`list`, `history`) belong to [`orch_status_invoke`] and are answered by it on the original request bytes, so their
//! replies and errors are the ones that function gives, byte for byte. Only a request that parses as a JSON object whose `mode` is
//! the string [`MODE`](crate::transition_wire::MODE) goes to [`transition_invoke`]. Everything else, including text that is not
//! UTF-8 or not JSON, a missing `mode` and an unknown mode, is handed to [`orch_status_invoke`] unchanged, so it keeps its existing
//! error.
//!
//! Dispatching here, and not inside `orch_status_reply`, leaves `orch_status_wire.rs` untouched. The price is one extra parse of a
//! read request in the write-enabled guest (the request is parsed once to read its `mode` and again by the read route); a read
//! request is never parsed in the default guest, which does not call this function.

use crate::transition_wire::{transition_invoke, MODE};
use crate::{obj_get, orch_status_invoke, parse_json, Json, SeamError};

/// Answers one request: a transition request by [`transition_invoke`], any other by [`orch_status_invoke`].
pub fn core_invoke(input: &[u8]) -> Result<Vec<u8>, SeamError> {
    if asks_for_transition(input) {
        transition_invoke(input)
    } else {
        orch_status_invoke(input)
    }
}

/// Whether `input` is a JSON object whose `mode` is the transition mode.
fn asks_for_transition(input: &[u8]) -> bool {
    let Ok(text) = std::str::from_utf8(input) else {
        return false;
    };
    matches!(
        parse_json(text),
        Ok(Json::Obj(fields)) if matches!(obj_get(&fields, "mode"), Some(Json::Str(mode)) if mode == MODE)
    )
}
