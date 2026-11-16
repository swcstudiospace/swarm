pub const MAX: usize = 30;

/// Basic input validation + size cap enforcement.
pub fn validate_pool_size(requested: usize) -> Result<usize, String> {
    if requested > MAX {
        return Err(format!("exceeds {} subagent cap", MAX));
    }
    if requested == 0 {
        return Err("pool size must be > 0".into());
    }
    Ok(requested)
}
