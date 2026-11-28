//! Host routes for the gate scripts. Each stays opt-in: the selector's default row is Python.
pub mod qa_gate;
pub mod rel_plan;
pub mod rev_gate;
pub mod sec_gate;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

/// `shutil.which`'s test for one PATH candidate: a link-following entry that is not a directory and has an execute bit.
/// A metadata error is no match.
fn is_executable_candidate(candidate: &Path) -> bool {
    fs::metadata(candidate)
        .map(|meta| !meta.is_dir() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::is_executable_candidate;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixListener;

    #[test]
    fn a_candidate_is_an_executable_non_directory() {
        let dir = tempfile::tempdir().expect("tempdir");
        let tool = dir.path().join("tool");
        fs::write(&tool, "#!/bin/sh\n").expect("write");
        fs::set_permissions(&tool, fs::Permissions::from_mode(0o755)).expect("chmod");
        let plain = dir.path().join("plain");
        fs::write(&plain, "text").expect("write");
        fs::set_permissions(&plain, fs::Permissions::from_mode(0o644)).expect("chmod");
        assert!(is_executable_candidate(&tool));
        assert!(!is_executable_candidate(&plain));
        assert!(!is_executable_candidate(dir.path()));
        assert!(!is_executable_candidate(&dir.path().join("missing")));
    }

    #[test]
    fn a_socket_with_an_execute_bit_is_a_candidate_as_shutil_which_has_it() {
        let dir = tempfile::tempdir().expect("tempdir");
        let socket = dir.path().join("npm");
        let _listener = UnixListener::bind(&socket).expect("bind");
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o755)).expect("chmod");
        assert!(is_executable_candidate(&socket));
    }
}
