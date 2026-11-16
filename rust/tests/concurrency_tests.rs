use gsd_orchestrator::pool::{SubagentPool, PoolState};

#[test]
fn workflow_30_subagent_and_scaling() {
    let p = SubagentPool::new(30).expect("cap");
    assert_eq!(p.state(), PoolState::Idle);
    p.create();
    assert_eq!(p.state(), PoolState::Active);
    assert_eq!(p.size(), 30);

    let new_size = p.scale(25).expect("scale");
    assert_eq!(new_size, 25);

    // Error reachability
    p.force_error();
    assert_eq!(p.state(), PoolState::Error);
}

#[test]
fn cap_enforced() {
    assert!(SubagentPool::new(31).is_err());
}
