# Letting the Agent Run Commands

> Flags: `--cli`

## The situation

The change is not purely textual. Adding a new dependency means editing `pyproject.toml` *and* installing it. Adding a test means writing the file *and* running the suite. Scaffolding a component means creating files *and* running a generator.

By default the tool refuses to emit terminal commands at all, and that default is correct: most changes do not need them, and a model that can run commands is a much larger surface to trust. `--cli` opens that door deliberately, for the runs where it earns its keep.

---

## 1. Launch with CLI mode on

```bash
combineCopy -f py toml -s --cli --apply --system
```

`--cli` swaps the EXECUTION schema for a variant with a fourth action type alongside create, modify, and delete:

```json
{
  "action": "command",
  "command": "pytest tests/test_retry.py -v"
}
```

Write your request as usual, and be explicit about the command side of it:

> Add exponential backoff to the HTTP client using tenacity. Add tenacity to the dependencies, install it, and add a test that asserts three retries happen before giving up.

## 2. Review the plan, paying attention to the commands

The implementation plan now includes a verification section naming the exact commands the model intends to run.

Read these properly. This is the point where you catch a command that would install into the wrong environment, run against the wrong config, or touch something outside the repository. It is far easier to correct in planning than to interrupt mid-execution.

## 3. Catch the mixed payload

![The EXECUTION payload containing both file edits and command actions](../images/usecases/uc5-01-payload.png)

File edits and commands arrive in one payload, in the order the model wants them run. Copy it and the listener picks it up.

## 4. Read the command entries in the file list

Commands appear in the sidebar alongside the files, labelled in magenta. Selecting one shows the command instead of a diff.

![A command entry selected in the apply TUI](../images/usecases/uc5-02-command-item.png)

Everything you can do to a file, you can do to a command. Press `d` to discard one you do not want run, which is the natural response to a command that looks slightly off but whose accompanying file edits are fine.

> [!IMPORTANT]
> Ordering is yours to manage. Applying a test command before the file it tests is created will simply fail. Work down the list, or use `Shift+A` and let the payload's own order stand.

## 5. Execute and watch the output

Press `a` on a command entry to run it. A dedicated screen opens and streams the output live.

![The command execution screen streaming output](../images/usecases/uc5-03-execution-output.png)

The command runs in your repository root with output and errors interleaved as they arrive. The exit code is printed when it finishes, and the Close button unlocks.

If a test fails, you have the output right there. Copy it back into the conversation and the model can correct itself in the next payload.

## 6. Commit

Commands are recorded in the session summary but are not staged, since they produced no tracked file changes of their own. Press `c` to commit the file changes as normal.

![The final summary showing both modified files and executed commands](../images/usecases/uc5-04-summary.png)

Commands appear in the summary with a magenta marker, so the run is legible after the fact.

---

## Tips

> [!CAUTION]
> Commands run with your shell, your permissions, and your working directory. Read every one before pressing `a`. This mode is designed so that nothing runs without an explicit keystroke from you, and that guarantee is only worth anything if you actually read what you are approving.

> [!TIP]
> Turning `cli` on permanently in `combineCopy --settings` is convenient but changes the default posture of every run. Consider leaving it off and passing the flag on the runs that need it, so the capability is always a conscious choice.

> [!NOTE]
> Commands cannot be reverted. `--revert` inverts file modifications, but there is no general way to undo an arbitrary shell command, and the tool does not pretend otherwise.
