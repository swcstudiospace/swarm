//! Security Sandbox for GSD-Core
//!
//! Thread isolation, capability tokens, audit logging, PII redaction.

use dashmap::DashMap;
use gsd_failure::ErrorCode;
use jsonwebtoken::errors::ErrorKind;
use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use parking_lot::Mutex;
use std::io::Write;

/// Issuer claim written by [`CapabilityToken::issue`] and required when decoding.
const ISSUER: &str = "gsd-core";
/// Audience claim written by [`CapabilityToken::issue`] and required when decoding.
const AUDIENCE: &str = "gsd-core";
/// Placeholder secret that shipped as the default before F-02; never accepted.
const HISTORICAL_DEFAULT_SECRET: &str = "gsd-core-secret-change-in-production";
/// Minimum JWT secret length in bytes.
const MIN_JWT_SECRET_BYTES: usize = 32;
/// Capability token lifetime in seconds used by [`SecurityConfig::new`].
const DEFAULT_CAPABILITY_TOKEN_TTL_SECS: u64 = 600;
/// Longest documented call deadline in seconds (`plan/wasmer-spike.md` T-04, `R:206-208`).
/// A capability token must never outlive the call it serves, so longer TTLs are refused.
const MAX_CAPABILITY_TOKEN_TTL_SECS: u64 = 1860;

/// Security configuration.
///
/// Build with [`SecurityConfig::new`]. Fields are public, so consumers re-run
/// [`SecurityConfig::validate`] on construction ([`CapabilityTokenManager::new`],
/// [`AuditLogger::new`]) and on issuance ([`CapabilityToken::issue`]).
#[derive(Clone)]
pub struct SecurityConfig {
    pub enable_sandbox: bool,
    pub jwt_secret: String,
    pub jwt_expiry_hours: u64,
    pub audit_log_path: String,
    pub pii_redaction_rules: Vec<PiiRule>,
    /// Capability token lifetime in seconds; must be within `1..=1860`.
    pub capability_token_ttl_secs: u64,
}

impl SecurityConfig {
    /// Builds a validated configuration; refuses a weak or default JWT secret.
    pub fn new(
        jwt_secret: impl Into<String>,
        audit_log_path: impl Into<String>,
    ) -> SecurityResult<Self> {
        let config = Self {
            enable_sandbox: true,
            jwt_secret: jwt_secret.into(),
            jwt_expiry_hours: 24,
            audit_log_path: audit_log_path.into(),
            pii_redaction_rules: Vec::new(),
            capability_token_ttl_secs: DEFAULT_CAPABILITY_TOKEN_TTL_SECS,
        };
        config.validate()?;
        Ok(config)
    }

    /// Refuses a JWT secret shorter than 32 bytes or equal to the historical default,
    /// and a capability token TTL of 0 or above 1860 seconds.
    pub fn validate(&self) -> SecurityResult<()> {
        if self.jwt_secret == HISTORICAL_DEFAULT_SECRET {
            return Err(SecurityError::InsecureSecret(
                "JWT secret is the historical default value",
            ));
        }
        if self.jwt_secret.len() < MIN_JWT_SECRET_BYTES {
            return Err(SecurityError::InsecureSecret(
                "JWT secret is shorter than 32 bytes",
            ));
        }
        if self.capability_token_ttl_secs == 0
            || self.capability_token_ttl_secs > MAX_CAPABILITY_TOKEN_TTL_SECS
        {
            return Err(SecurityError::InvalidConfig(format!(
                "capability_token_ttl_secs must be between 1 and {MAX_CAPABILITY_TOKEN_TTL_SECS}, got {}",
                self.capability_token_ttl_secs
            )));
        }
        Ok(())
    }
}

impl std::fmt::Debug for SecurityConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SecurityConfig")
            .field("enable_sandbox", &self.enable_sandbox)
            .field("jwt_secret", &"<redacted>")
            .field("jwt_expiry_hours", &self.jwt_expiry_hours)
            .field("audit_log_path", &self.audit_log_path)
            .field("pii_redaction_rules", &self.pii_redaction_rules)
            .field("capability_token_ttl_secs", &self.capability_token_ttl_secs)
            .finish()
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
    /// Mints an HS256 token for `thread_id` expiring `capability_token_ttl_secs` seconds from now.
    pub fn issue(
        config: &SecurityConfig,
        thread_id: &str,
        scope: Vec<String>,
        resources: Vec<String>,
    ) -> SecurityResult<String> {
        config.validate()?;
        let now = now_secs()?;
        let claims = CapabilityClaims {
            sub: thread_id.to_string(),
            iss: ISSUER.to_string(),
            aud: AUDIENCE.to_string(),
            exp: now.saturating_add(config.capability_token_ttl_secs),
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
}

/// Verifies the signature, `exp`, `iss`, `aud` and required claims of `token`.
///
/// Private: callers go through [`CapabilityTokenManager::authorize`], which also
/// enforces subject, scope, resource and revocation checks.
fn decode_claims(config: &SecurityConfig, token: &str) -> SecurityResult<CapabilityClaims> {
    let mut validation = Validation::new(Algorithm::HS256);
    validation.leeway = 0;
    validation.set_issuer(&[ISSUER]);
    validation.set_audience(&[AUDIENCE]);
    validation.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
    let token_data = decode::<CapabilityClaims>(
        token,
        &DecodingKey::from_secret(config.jwt_secret.as_bytes()),
        &validation,
    )
    .map_err(|e| {
        if matches!(e.kind(), ErrorKind::ExpiredSignature) {
            SecurityError::TokenExpired
        } else {
            SecurityError::Jwt(e)
        }
    })?;
    Ok(token_data.claims)
}

/// Capability token manager
pub struct CapabilityTokenManager {
    config: SecurityConfig,
    issued_tokens: DashMap<String, CapabilityClaims>,
    revoked_tokens: DashMap<String, u64>,
}

impl CapabilityTokenManager {
    /// Creates a manager; refuses a configuration that fails [`SecurityConfig::validate`].
    pub fn new(config: SecurityConfig) -> SecurityResult<Self> {
        config.validate()?;
        Ok(Self {
            config,
            issued_tokens: DashMap::new(),
            revoked_tokens: DashMap::new(),
        })
    }

    pub fn issue(
        &self,
        thread_id: &str,
        scope: Vec<String>,
        resources: Vec<String>,
    ) -> SecurityResult<String> {
        CapabilityToken::issue(&self.config, thread_id, scope, resources)
    }

    /// Authorizes `token` for `thread_id`, `required_scope` and, when `Some`, `required_resource`.
    ///
    /// Checks the signature, `exp`, `iss` and `aud`, then revocation, then that
    /// `claims.sub == thread_id`, that `claims.scope` contains `required_scope`, and
    /// that `claims.resources` contains `required_resource` when it is `Some`.
    pub fn authorize(
        &self,
        token: &str,
        thread_id: &str,
        required_scope: &str,
        required_resource: Option<&str>,
    ) -> SecurityResult<CapabilityClaims> {
        let claims = decode_claims(&self.config, token)?;

        if self.revoked_tokens.contains_key(token) {
            return Err(SecurityError::TokenRevoked);
        }
        if claims.sub != thread_id {
            return Err(SecurityError::Unauthorized(
                "token was not issued for this thread".to_string(),
            ));
        }
        if !claims.scope.iter().any(|scope| scope == required_scope) {
            return Err(SecurityError::Unauthorized(format!(
                "token lacks required scope `{required_scope}`"
            )));
        }
        if let Some(resource) = required_resource {
            if !claims.resources.iter().any(|allowed| allowed == resource) {
                return Err(SecurityError::Unauthorized(format!(
                    "token lacks required resource `{resource}`"
                )));
            }
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
    /// Opens the audit log; refuses a configuration that fails [`SecurityConfig::validate`]
    /// before the file is touched.
    pub fn new(config: SecurityConfig) -> SecurityResult<Self> {
        config.validate()?;
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
    #[error("Insecure JWT secret: {0}")]
    InsecureSecret(&'static str),
    #[error("Invalid security configuration: {0}")]
    InvalidConfig(String),
    #[error("Other: {0}")]
    Other(String),
}

impl SecurityError {
    /// Maps this error to its `gsd-failure` taxonomy code.
    ///
    /// Refusals (token, authorization, secret and configuration policy) map to
    /// `E-POLICY`. `Jwt` covers signature, claim, and expiry refusals. `Io`,
    /// `Serialization`, `Crypto` and `Other` are internal faults, not refusals, and
    /// map to `E-INTERNAL`. `Other` carries untyped messages (for example an
    /// `anyhow` error or a clock read failure), so no refusal class applies to it.
    pub fn error_code(&self) -> ErrorCode {
        match self {
            Self::TokenExpired
            | Self::TokenRevoked
            | Self::InvalidToken(_)
            | Self::Jwt(_)
            | Self::Unauthorized(_)
            | Self::InsecureSecret(_)
            | Self::InvalidConfig(_) => ErrorCode::Policy,
            Self::Io(_) | Self::Serialization(_) | Self::Crypto(_) | Self::Other(_) => {
                ErrorCode::Internal
            }
        }
    }
}

impl From<anyhow::Error> for SecurityError {
    fn from(e: anyhow::Error) -> Self {
        SecurityError::Other(e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_SECRET: &str = "unit-test-signing-secret-0123456789abcdef";
    const THREAD: &str = "thread-1";
    const SCOPE: &str = "read";
    const RESOURCE: &str = "repo:gsd-core";

    fn test_config() -> SecurityResult<SecurityConfig> {
        SecurityConfig::new(TEST_SECRET, "./audit.log")
    }

    fn new_manager() -> SecurityResult<CapabilityTokenManager> {
        CapabilityTokenManager::new(test_config()?)
    }

    /// Signs arbitrary claims with the test secret, bypassing `issue`.
    fn sign(claims: &CapabilityClaims) -> SecurityResult<String> {
        Ok(encode(
            &Header::default(),
            claims,
            &EncodingKey::from_secret(TEST_SECRET.as_bytes()),
        )?)
    }

    fn claims_with(iss: &str, aud: &str, exp: u64) -> SecurityResult<CapabilityClaims> {
        Ok(CapabilityClaims {
            sub: THREAD.to_string(),
            iss: iss.to_string(),
            aud: aud.to_string(),
            exp,
            iat: now_secs()?,
            scope: vec![SCOPE.to_string()],
            resources: vec![RESOURCE.to_string()],
        })
    }

    fn future_exp() -> SecurityResult<u64> {
        Ok(now_secs()? + 600)
    }

    fn assert_policy_refusal<T>(result: SecurityResult<T>) {
        match result {
            Err(err) => assert_eq!(
                err.error_code(),
                ErrorCode::Policy,
                "unexpected refusal: {err}"
            ),
            Ok(_) => panic!("token was accepted but must be refused"),
        }
    }

    #[test]
    fn token_with_foreign_audience_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = sign(&claims_with(ISSUER, "other", future_exp()?)?)?;
        assert_policy_refusal(manager.authorize(&token, THREAD, SCOPE, Some(RESOURCE)));
        Ok(())
    }

    #[test]
    fn token_with_foreign_issuer_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = sign(&claims_with("other", AUDIENCE, future_exp()?)?)?;
        assert_policy_refusal(manager.authorize(&token, THREAD, SCOPE, Some(RESOURCE)));
        Ok(())
    }

    #[test]
    fn issued_token_authorizes_for_its_thread_and_scope() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = manager.issue(THREAD, vec![SCOPE.to_string()], vec![RESOURCE.to_string()])?;
        let claims = manager.authorize(&token, THREAD, SCOPE, Some(RESOURCE))?;
        assert_eq!(claims.sub, THREAD);
        assert_eq!(claims.iss, ISSUER);
        assert_eq!(claims.aud, AUDIENCE);
        manager.authorize(&token, THREAD, SCOPE, None)?;
        Ok(())
    }

    #[test]
    fn token_for_another_thread_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = manager.issue(THREAD, vec![SCOPE.to_string()], vec![RESOURCE.to_string()])?;
        assert_policy_refusal(manager.authorize(&token, "thread-2", SCOPE, Some(RESOURCE)));
        Ok(())
    }

    #[test]
    fn token_without_required_scope_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = manager.issue(THREAD, vec![SCOPE.to_string()], vec![RESOURCE.to_string()])?;
        assert_policy_refusal(manager.authorize(&token, THREAD, "write", Some(RESOURCE)));
        Ok(())
    }

    #[test]
    fn token_without_required_resource_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = manager.issue(THREAD, vec![SCOPE.to_string()], vec![RESOURCE.to_string()])?;
        assert_policy_refusal(manager.authorize(&token, THREAD, SCOPE, Some("repo:other")));
        Ok(())
    }

    #[test]
    fn expired_token_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let expired = claims_with(ISSUER, AUDIENCE, now_secs()?.saturating_sub(60))?;
        let token = sign(&expired)?;
        let result = manager.authorize(&token, THREAD, SCOPE, Some(RESOURCE));
        assert!(matches!(result, Err(SecurityError::TokenExpired)));
        assert_policy_refusal(result);
        Ok(())
    }

    #[test]
    fn token_that_is_revoked_is_refused() -> SecurityResult<()> {
        let manager = new_manager()?;
        let token = manager.issue(THREAD, vec![SCOPE.to_string()], vec![RESOURCE.to_string()])?;
        manager.authorize(&token, THREAD, SCOPE, Some(RESOURCE))?;
        manager.revoke(&token);
        let result = manager.authorize(&token, THREAD, SCOPE, Some(RESOURCE));
        assert!(matches!(result, Err(SecurityError::TokenRevoked)));
        assert_policy_refusal(result);
        Ok(())
    }

    #[test]
    fn default_secret_refuses_init() -> SecurityResult<()> {
        assert!(matches!(
            SecurityConfig::new(HISTORICAL_DEFAULT_SECRET, "./audit.log"),
            Err(SecurityError::InsecureSecret(_))
        ));

        let empty = SecurityConfig {
            jwt_secret: String::new(),
            ..test_config()?
        };
        assert!(matches!(
            CapabilityTokenManager::new(empty),
            Err(SecurityError::InsecureSecret(_))
        ));

        let thirty_one = SecurityConfig {
            jwt_secret: "x".repeat(31),
            ..test_config()?
        };
        assert!(matches!(
            CapabilityTokenManager::new(thirty_one.clone()),
            Err(SecurityError::InsecureSecret(_))
        ));
        assert!(matches!(
            AuditLogger::new(thirty_one),
            Err(SecurityError::InsecureSecret(_))
        ));

        let exact = SecurityConfig {
            jwt_secret: "x".repeat(MIN_JWT_SECRET_BYTES),
            ..test_config()?
        };
        assert!(CapabilityTokenManager::new(exact).is_ok());
        Ok(())
    }

    #[test]
    fn ttl_is_seconds_granular() -> SecurityResult<()> {
        let config = SecurityConfig {
            capability_token_ttl_secs: 90,
            ..test_config()?
        };
        let token = CapabilityToken::issue(&config, THREAD, vec![SCOPE.to_string()], Vec::new())?;
        let claims = decode_claims(&config, &token)?;
        assert_eq!(claims.exp - claims.iat, 90);
        Ok(())
    }

    #[test]
    fn ttl_above_call_deadline_is_refused() -> SecurityResult<()> {
        let base = test_config()?;
        for ttl in [0, MAX_CAPABILITY_TOKEN_TTL_SECS + 1] {
            let config = SecurityConfig {
                capability_token_ttl_secs: ttl,
                ..base.clone()
            };
            assert!(
                matches!(config.validate(), Err(SecurityError::InvalidConfig(_))),
                "ttl {ttl} must be refused by validate"
            );
            assert!(
                CapabilityToken::issue(&config, THREAD, Vec::new(), Vec::new()).is_err(),
                "ttl {ttl} must be refused by issue"
            );
        }
        let longest = SecurityConfig {
            capability_token_ttl_secs: MAX_CAPABILITY_TOKEN_TTL_SECS,
            ..base
        };
        longest.validate()?;
        Ok(())
    }

    #[test]
    fn debug_output_redacts_secret() -> SecurityResult<()> {
        let config = test_config()?;
        let rendered = format!("{config:?}");
        assert!(!rendered.contains(TEST_SECRET));
        assert!(rendered.contains("<redacted>"));
        Ok(())
    }
}
