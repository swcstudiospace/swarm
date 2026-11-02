"""P2-11: one torn trailing line (a crash mid-append) must not make read_events raise for every caller."""


def test_read_events_skips_a_torn_trailing_line(swarm_dir):
    from swarm.runlog import emit, read_events
    emit("swarm.one", {"n": 1}, source="t", correlation_id="c", tee=False)
    emit("swarm.two", {"n": 2}, source="t", correlation_id="c", tee=False)
    with (swarm_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"type":')  # no closing brace, no newline: what a crash during append leaves behind
    events = read_events()
    assert [e["type"] for e in events] == ["swarm.one", "swarm.two"]
    assert [e["type"] for e in read_events(event_type="swarm.two")] == ["swarm.two"]
