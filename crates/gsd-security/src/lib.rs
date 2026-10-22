//! Security Sandbox for GSD-Core
//!
//! Thread isolation, capability tokens, audit logging, PII redaction.

use dashmap::DashMap;
use jsonwebtoken::{decode, encode, DecodingKey, EncodingKey, Header, Validation};
use parking_lot::Mutex;
use std::io::Write;

/// Security configuration
#[derive(Debug, Clone)]
pub struct SecurityConfig {
    pub enable_sandbox: bool,
    pub jwt_secret: String,
    pub jwt_expiry_hours: u64,
    pub audit_log_path: String,
    pub pii_redaction_rules: Vec<PiiRule>,
    pub capability_token_expiry_hours: u64,
}

impl Default for SecurityConfig {
    fn default() -> Self {
        Self {
            enable_sandbox: true,
            jwt_secret: "gsd-core-secret-change-in-production".to_string(),
            jwt_expiry_hours: 24,
            audit_log_path: "./audit.log".to_string(),
            pii_redaction_rules: Vec::new(),
            capability_token_expiry_hours: 1,
        }
    }
}

#[derive(Debug, Clone)]
pub struct PiiRule {
    pub pattern: String,
    pub replacement: String,
    pub description: String,
}

fn now_secs() -> SecurityResult<u64> {
    let since_epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| SecurityError::Other(format!("system clock is before UNIX epoch: {e}")))?;
    Ok(since_epoch.as_secs())
}

/// Capability token for thread authorization
pub struct CapabilityToken {
    token: String,
    claims: CapabilityClaims,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CapabilityClaims {
    pub sub: String,            // thread_id
    pub iss: String,            // issuer
    pub aud: String,            // audience
    pub exp: u64,               // expiration
    pub iat: u64,               // issued at
    pub scope: Vec<String>,     // capabilities
    pub resources: Vec<String>, // allowed resources
}

impl CapabilityToken {
    pub fn issue(
        config: &SecurityConfig,
        thread_id: &str,
        scope: Vec<String>,
        resources: Vec<String>,
    ) -> SecurityResult<String> {
        let now = now_secs()?;
        let claims = CapabilityClaims {
            sub: thread_id.to_string(),
            iss: "gsd-core".to_string(),
            aud: "gsd-core".to_string(),
            exp: now.saturating_add(config.capability_token_expiry_hours.saturating_mul(3600)),
            iat: now,
            scope,
            resources,
        };

        let token = encode(
            &Header::default(),
            &claims,
            &EncodingKey::from_secret(config.jwt_secret.as_bytes()),
        )?;

        Ok(token)
    }

    pub fn validate(config: &SecurityConfig, token: &str) -> SecurityResult<CapabilityClaims> {
        let validation = Validation::default();
        let token_data = decode::<CapabilityClaims>(
            token,
            &DecodingKey::from_secret(config.jwt_secret.as_bytes()),
            &validation,
        )?;
        Ok(token_data.claims)
    }
}

/// Capability token manager
pub struct CapabilityTokenManager {
    config: SecurityConfig,
    issued_tokens: DashMap<String, CapabilityClaims>,
    revoked_tokens: DashMap<String, u64>,
}

impl CapabilityTokenManager {
    pub fn new(config: SecurityConfig) -> Self {
        Self {
            config,
            issued_tokens: DashMap::new(),
            revoked_tokens: DashMap::new(),
        }
    }

    pub fn issue(
        &self,
        thread_id: &str,
        scope: Vec<String>,
        resources: Vec<String>,
    ) -> SecurityResult<String> {
        CapabilityToken::issue(&self.config, thread_id, scope, resources)
    }

    pub fn validate(&self, token: &str) -> SecurityResult<CapabilityClaims> {
        let claims = CapabilityToken::validate(&self.config, token)?;

        // Check if revoked
        if self.revoked_tokens.contains_key(token) {
            return Err(SecurityError::TokenRevoked);
        }

        Ok(claims)
    }

    pub fn revoke(&self, token: &str) {
        self.revoked_tokens
            .insert(token.to_string(), now_secs().unwrap_or_default());
    }
}

/// Audit log entry
#[derive(Debug, Clone, serde::Serialize)]
pub struct AuditEntry {
    pub id: String,
    pub session_id: String,
    pub thread_id: String,
    pub action: String,
    pub resource: String,
    pub decision: String,
    pub timestamp: u64,
    pub metadata: std::collections::HashMap<String, String>,
}

/// Audit logger
pub struct AuditLogger {
    config: SecurityConfig,
    file: Mutex<std::fs::File>,
    merkle_tree: Mutex<MerkleTree>,
}

impl AuditLogger {
    pub fn new(config: SecurityConfig) -> SecurityResult<Self> {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&config.audit_log_path)?;

        Ok(Self {
            config,
            file: Mutex::new(file),
            merkle_tree: Mutex::new(MerkleTree::new()),
        })
    }

    pub fn log(&self, entry: AuditEntry) -> SecurityResult<()> {
        let json = serde_json::to_string(&entry)?;
        let mut file = self.file.lock();
        writeln!(file, "{}", json)?;
        file.flush()?;

        // Add to merkle tree
        self.merkle_tree.lock().add_entry(&json);

        Ok(())
    }
}

struct MerkleTree {
    leaves: Vec<String>,
    root: Option<String>,
}

impl MerkleTree {
    fn new() -> Self {
        Self {
            leaves: Vec::new(),
            root: None,
        }
    }

    fn add_entry(&mut self, json: &str) {
        let hash = blake3::hash(json.as_bytes());
        self.leaves.push(hash.to_hex().to_string());
        self.recompute_root();
    }

    fn recompute_root(&mut self) {
        // Simplified - in production use proper Merkle tree
        if !self.leaves.is_empty() {
            let combined = self.leaves.join("");
            self.root = Some(blake3::hash(combined.as_bytes()).to_hex().to_string());
        }
    }
}

/// A single PII match replaced during redaction
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Redaction {
    pub start: i32,
    pub end: i32,
    pub type_: String,
    pub reason: String,
}

/// Output of a redaction pass
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct RedactionResult {
    pub output: String,
    pub redactions: Vec<Redaction>,
}

/// PII redaction
pub struct Redactor {
    rules: Vec<PiiRule>,
}

impl Redactor {
    pub fn new(rules: Vec<PiiRule>) -> Self {
        Self { rules }
    }

    pub fn redact(&self, input: &str) -> RedactionResult {
        let mut output = input.to_string();
        let mut redactions = Vec::new();

        for rule in &self.rules {
            let Ok(regex) = regex::Regex::new(&rule.pattern) else {
                continue;
            };
            let mut start = 0;
            while start <= output.len() {
                let Some(mat) = regex.find_at(&output, start) else {
                    break;
                };
                let start_pos = mat.start();
                let end_pos = mat.end();
                if start_pos == end_pos {
                    // Empty match: step over one character so the scan always advances.
                    start =
                        start_pos + output[start_pos..].chars().next().map_or(1, char::len_utf8);
                    continue;
                }
                redactions.push(Redaction {
                    start: start_pos as i32,
                    end: end_pos as i32,
                    type_: rule.description.clone(),
                    reason: "PII redaction".to_string(),
                });
                output.replace_range(start_pos..end_pos, &rule.replacement);
                start = start_pos + rule.replacement.len();
            }
        }

        RedactionResult { output, redactions }
    }
}

/// Security result type
pub type SecurityResult<T> = std::result::Result<T, SecurityError>;

#[derive(Debug, thiserror::Error)]
pub enum SecurityError {
    #[error("Token expired")]
    TokenExpired,
    #[error("Token revoked")]
    TokenRevoked,
    #[error("Invalid token: {0}")]
    InvalidToken(String),
    #[error("JWT error: {0}")]
    Jwt(#[from] jsonwebtoken::errors::Error),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Serialization error: {0}")]
    Serialization(#[from] serde_json::Error),
    #[error("Crypto error: {0}")]
    Crypto(String),
    #[error("Unauthorized: {0}")]
    Unauthorized(String),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for SecurityError {
    fn from(e: anyhow::Error) -> Self {
        SecurityError::Other(e.to_string())
    }
}
