#[derive(Debug, Clone, Copy, PartialEq)]
pub enum TaskState {
    Pending,
    Running,
    Done,
    Error,
}

#[derive(Debug, Clone)]
pub struct SubTask {
    pub id: String,
    pub description: String,
    pub parent: Option<String>,
    pub state: TaskState,
}
 
 pub fn decompose_task(task: &str) -> Vec<SubTask> {
     // Simple phase-based decomposition for SDLC tasks (as per module goal)
     if task.contains("phases") || task.contains(',') {
         task.split(',')
             .map(|p| p.trim())
             .filter(|p| !p.is_empty())
             .enumerate()
             .map(|(i, p)| SubTask {
                 id: format!("sub-{}", i),
                 description: p.to_string(),
                parent: if i == 0 { None } else { Some(format!("sub-{}", i-1)) },
                state: TaskState::Pending,
             })
             .collect()
     } else {
        vec![SubTask { id: "single".into(), description: task.into(), parent: None, state: TaskState::Pending }]
     }
 }

pub fn mark_done(task: &mut SubTask) {
    task.state = TaskState::Done;
}
