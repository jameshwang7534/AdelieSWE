"use strict";
// Educational examples only. No workflow submissions or provider calls.
const stages = [
  ["Import & synchronize", "GitHub + workspace", "Start with the issue and the code.", "The platform registers the repository, synchronizes its default branch, and imports the GitHub issue. Generated changes will live in an isolated execution workspace.", "WORKSPACE", "repository → example/calculator\nissue      → Fix add() returning the wrong result\nsource     → default branch checkout", "Start from a known revision, with the issue preserved in PostgreSQL."],
  ["Find relevant context", "Hybrid retrieval", "Find the code that matters.", "Source files become line-aware chunks. BM25 searches words and identifiers; vectors compare meaning. Rank fusion combines the lists into bounded issue context.", "RETRIEVED CONTEXT", "calculator.py · lines 1–2\ndef add(a, b):\n    return a - b\n\nBM25 rank: 1 · vector rank: 2\nRRF: 1/61 + 1/62 ≈ 0.0325", "Search stays inside the repository. Context limits keep prompts manageable."],
  ["Plan the dependencies", "PlannerAgent", "Make the order of work explicit.", "The planner proposes tasks, files, acceptance criteria, and tests. Validation rejects missing dependencies, duplicate keys, and cycles before a plan can be saved.", "VALIDATED TASK GRAPH", "TASK-1  Fix add()\n   ↓\nTASK-2  Add regression coverage\n   ↓\nTASK-3  Document expected behavior", "The scheduler recognizes independent ready tasks. The current full workflow applies tasks serially in its shared workspace."],
  ["Apply focused changes", "CodingAgent", "Propose a patch, not a shell script.", "The coding agent uses the task, issue, retrieved context, and dependency outcomes. Structured patches are validated; unsafe paths and protected files are rejected.", "PROPOSED DIFF · calculator.py", " def add(a, b):\n-    return a - b\n+    return a + b", "Applying a patch does not complete the task. Required tests must still pass."],
  ["Run trusted tests", "TestAgent + Docker", "Test the change in isolation.", "Operator-approved commands run in temporary Linux containers. Network is disabled by default; CPU, memory, process count, runtime, and output are limited. Only the task workspace is mounted.", "EXAMPLE TEST REPORT", "$ python -m unittest\nRan 3 tests\nOK\n\nexit_code: 0 · timed_out: false\nstdout, stderr and duration are recorded.", "Host secrets are not forwarded wholesale. A successful command needs meaningful coverage; zero discovered tests is not proof of correctness."],
  ["Recover when needed", "DebugAgent", "Use a failure to make the next fix.", "The debug agent receives the failing command, output, diff, context, and previous attempts. It proposes another validated patch, then relevant tests run again.", "EXAMPLE RECOVERY PATH", "Tests fail\n  → Debug attempt 1\n  → Apply validated fix\n  → Rerun tests\n  → Pass: continue / fail: retry within limit\n\nDefault maximum: 3 debug attempts", "Failure history is preserved. Exhaustion fails the task and blocks dependent work. Repeated identical patches are guarded against."],
  ["Review the evidence", "ReviewAgent", "A second look, with mechanical gates.", "The review agent checks the full diff, issue, plan, task outcomes, tests, and context. Findings have info, warning, or blocking severity.", "PUBLICATION CHECKS", "✓ Required tasks completed\n✓ Required tests passed\n✓ No blocking review findings\n✓ Workspace has actual changes\n\nLLM approval cannot override failed tests.", "AI review is combined with checks enforced by orchestration. Human review is still necessary."],
  ["Prepare the pull request", "Git + GitHub", "Deliver a change you can review.", "If publication was enabled at submission, the platform creates a deterministic branch, commits and pushes changes, then creates or reconciles a GitHub PR and saves its record.", "EXAMPLE PR CONTENTS", "Fix calculator addition\n\n• Original issue and execution ID\n• Plan and changed files\n• Test commands and results\n• Review summary and limitations\n\nYou review. You decide whether to merge.", "Publication is opt-in. When disabled, the workflow stops after review without creating a remote branch or PR."]
];
let selected = 0;
const list = document.getElementById("stage-list");
const previous = document.getElementById("previous");
const next = document.getElementById("next");
const buttons = stages.map((stage, index) => {
  const button = document.createElement("button");
  button.className = "stage-button";
  button.type = "button";
  button.setAttribute("aria-controls", "stage-title");
  const number = document.createElement("span");
  number.className = "number";
  number.textContent = String(index + 1).padStart(2, "0");
  const name = document.createElement("span");
  name.textContent = stage[0];
  const arrow = document.createElement("span");
  arrow.className = "arrow";
  arrow.textContent = "↗";
  arrow.setAttribute("aria-hidden", "true");
  button.append(number, name, arrow);
  button.addEventListener("click", () => selectStage(index));
  list.append(button);
  return button;
});
function selectStage(index) {
  selected = Math.max(0, Math.min(stages.length - 1, index));
  const stage = stages[selected];
  const fields = {"stage-counter": `STEP ${String(selected + 1).padStart(2, "0")} / 08`, "stage-owner": stage[1], "stage-title": stage[2], "stage-description": stage[3], "artifact-label": stage[4], "artifact-content": stage[5], "stage-reason": stage[6]};
  Object.entries(fields).forEach(([id, text]) => { document.getElementById(id).textContent = text; });
  buttons.forEach((button, position) => button.setAttribute("aria-pressed", String(position === selected)));
  previous.disabled = selected === 0;
  next.disabled = selected === stages.length - 1;
}
previous.addEventListener("click", () => selectStage(selected - 1));
next.addEventListener("click", () => selectStage(selected + 1));
selectStage(0);
