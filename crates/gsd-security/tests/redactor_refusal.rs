use gsd_security::{PiiRule, Redactor, SecurityError, SecurityResult};

#[test]
fn redactor_new_refuses_uncompilable_pattern() {
    let rules = vec![PiiRule {
        pattern: "(?i)[0-9]{3}-[0-9]{2}-[0-9]{4".to_string(),
        replacement: "[SSN]".to_string(),
        description: "ssn".to_string(),
    }];
    let result: SecurityResult<Redactor> = Redactor::new(rules);
    assert!(matches!(result, Err(SecurityError::InvalidConfig(_))));
}
