# Splitting a Sweeping Change

> Flags: `--divide`, `--file-cull`, `-s`, `--system`

## The situation

Your app has a TUI for every operation, and you want a CLI equivalent for each one. That is a dozen new files, a dozen entry points, and edits scattered across the argument parser, the settings module, and the docs.

Asking for all of it in one go fails in three predictable ways:

1. **Output tokens run out.** A payload touching a dozen files gets truncated partway through and never parses.
2. **Search blocks degrade.** By the eighth file the model is working from memory of code it read long ago, and its search strings stop matching.
3. **One bad block poisons everything.** A single mismatch means a round trip that re-sends the whole payload.

Large Task Mode splits the work up front, giving each piece its own conversation and its own narrow context.

---

## 1. Launch in divide mode

```bash
combineCopy --divide --file-cull -s --system
```

In the request area, describe the whole job rather than the first slice of it:

> Every operation in this app is TUI-only. Add a CLI equivalent for each one, sharing the existing business logic rather than duplicating it, and wire them into the argument parser.

## 2. Read the division plan

With `--divide` active, PLANNING produces a task breakdown instead of an implementation plan. The model proposes a set of sub-tasks and explains what each one accomplishes.

![The task division plan presented as inline markdown](../images/usecases/uc2-01-division-plan.png)

This is the moment to push back. It is far cheaper to merge two tasks or split an overloaded one now than to discover the problem three sub-tasks in. Ask for changes and stay in planning until the split looks right.

## 3. Approve and catch the payload

Once the split is agreed, the model emits a `TASK` payload naming the overall job and listing each sub-task with its own prompt and its own file requirements.

![The TASK payload in the chat](../images/usecases/uc2-02-task-payload.png)

Copy it and run the listener:

```bash
combineCopy --apply
```

The listener recognises a `TASK` payload, writes it to `.cc_tasks.json` in your repository root, and drops you straight into the task manager.

## 4. Work a single task

![The task manager showing sub-tasks with checkboxes](../images/usecases/uc2-03-task-list.png)

Enter a number to select a task. You are shown its sub-prompt and the exact files and functions it asked for.

![A selected task showing its sub-prompt and requested files](../images/usecases/uc2-04-task-detail.png)

When asked whether to modify the requested files, answer `y` to open the file selector pre-populated with the task's own selection. This is where you add the file the model forgot, or strip out one it does not really need.

## 5. Run the sub-task in a fresh chat

The assembled payload for that single task lands on your clipboard. Paste it into a **new conversation**, not the one you planned in.

This matters. Each sub-task carries only the context it needs, and that is the entire reason for splitting the work. Pasting it back into the planning conversation reintroduces exactly the context pressure you were trying to escape.

![A sub-task pasted into a fresh conversation](../images/usecases/uc2-05-subtask-chat.png)

The sub-task conversation behaves like any normal run: it plans, you approve, it emits an EXECUTION payload.

## 6. Apply, commit, return

```bash
combineCopy --apply
```

Review the diffs, press `Shift+A` to apply them all, then `c` to commit using the model's own commit message. Committing between sub-tasks is worth the small effort, because it gives you a clean point to return to if a later task goes wrong.

## 7. Mark it done and continue

Back in your repository:

```bash
combineCopy --divide
```

An unfinished mega task is detected and you are offered a resume prompt.

![The resume prompt on the next divide run](../images/usecases/uc2-06-resume.png)

In the task manager, `c 1` toggles task 1 as complete. Pressing Enter on an empty prompt jumps straight to the first incomplete task, so the normal rhythm is: Enter, work the task, come back, `c <num>`, Enter.

When every task is checked off, the manager tells you so and exits.

---

## Tips

> [!NOTE]
> `.cc_tasks.json` lives in your repository root and is plain JSON. It is safe to hand-edit if you want to reword a sub-prompt, and safe to delete if you want to abandon the split and start over.

> [!TIP]
> For a project you will be dividing repeatedly, run `combineCopy --settings` and turn `divide` on. A bare `combineCopy` will then always look for unfinished tasks first.

> [!WARNING]
> Sub-tasks are executed in whatever order you choose, but they are not always independent. If task 3 imports something task 1 creates, do them in that order or task 3 will be planning against code that does not exist yet.
