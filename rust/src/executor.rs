use std::thread;
use std::time::Duration;

pub fn execute_parallel(tasks: usize) -> usize {
    let mut handles = vec![];
    for i in 0..tasks.min(30) {
        handles.push(thread::spawn(move || {
            // simulate work
            thread::sleep(Duration::from_millis(1));
            i
        }));
    }
    handles.into_iter().map(|h| h.join().unwrap()).count()
}
