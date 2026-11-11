//! `check_tool_input` refuses an oversize tool input before it is serialised: the heap that the
//! call allocates stays far below the size of the input it rejects.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

use gsd_codes::ErrorCode;
use gsd_sdk::omp_tool_argv::check_tool_input;
use serde_json::Value;

/// Counts live heap bytes and the high-water mark since the last reset.
struct Counting;

static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = System.alloc(layout);
        if !ptr.is_null() {
            let live = LIVE.fetch_add(layout.size(), Ordering::SeqCst) + layout.size();
            PEAK.fetch_max(live, Ordering::SeqCst);
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout);
        LIVE.fetch_sub(layout.size(), Ordering::SeqCst);
    }
}

#[global_allocator]
static GLOBAL: Counting = Counting;

#[test]
fn oversize_tool_input_is_refused_without_serialising() {
    let input = Value::String("x".repeat(50_000_000));
    let baseline = LIVE.load(Ordering::SeqCst);
    PEAK.store(baseline, Ordering::SeqCst);
    let err = check_tool_input(&input).expect_err("50 MB is over the cap");
    let peak = PEAK.load(Ordering::SeqCst).saturating_sub(baseline);
    assert_eq!(err.code, ErrorCode::Input);
    assert!(
        peak < 4 * 1024 * 1024,
        "check_tool_input allocated a peak of {peak} bytes for a rejected 50 MB input"
    );
}
