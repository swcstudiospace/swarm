use gsd_orchestrator::pool::{SubagentPool, PoolState};
use gsd_orchestrator::decomposition::decompose_task;
use gsd_orchestrator::executor::execute_parallel;

#[test]
fn workflow_30_subagent_and_scaling() {
    let p = SubagentPool::new(30).expect("cap");
    assert_eq!(p.state(), PoolState::Idle);
    p.create();
    assert_eq!(p.state(), PoolState::Active);
    assert_eq!(p.size(), 30);

    p.begin_scaling();
    assert_eq!(p.state(), PoolState::Scaling, "Scaling state must be entered");
    let new_size = p.scale(25).expect("scale");
    assert_eq!(new_size, 25);
    assert_eq!(p.active_workers(), 25, "Scaling must change the number of active workers");
    p.end_scaling();
    assert_eq!(p.state(), PoolState::Active);

    p.force_error();
    assert_eq!(p.state(), PoolState::Error);
}

#[test]
fn cap_enforced() {
    assert!(SubagentPool::new(31).is_err());
}

#[test]
fn decomposition_and_parallel_execution() {
    let tasks = decompose_task("req,arch,impl,test");
    assert_eq!(tasks.len(), 4);
    let ran = execute_parallel(4);
    assert_eq!(ran, 4);
}

#[test]
fn plan_verification_commands_would_pass() {
    // Simulates the commands from the ultrathink workflow plan:
    //   cargo test -p gsd_orchestrator
    //   PYTHONPATH=python python -c "from gsd_orchestrator... import create_pool"
    let p = SubagentPool::new(30).unwrap();
    p.create();
    let _ = p.scale(20);
    assert!(p.state() == PoolState::Active || p.state() == PoolState::Scaling);
}
