# Practising Instead of Pasting

> Flags: `--rehab`, and the `t` key in the apply listener

## The situation

You have noticed something uncomfortable. You can still read code fluently, but when you sit down to write a non-trivial refactor from scratch, you reach for the model before you reach for the keyboard. The muscle memory is going.

Rehab Mode is the deliberate fix. The model still works out what needs to change and why, but it hands you the *intent* in plain English and hides the code until you have attempted it yourself.

---

## 1. Launch in rehab mode

```bash
combineCopy -f py -s --rehab --apply --system
```

This is the ordinary refactoring combination with `--rehab` added. The system prompt now instructs the model to attach an `instruction` to every change, plus up to three optional hints.

Write your request as normal. Rehab Mode changes the output format, not the conversation.

## 2. Catch the payload and open a change

When the EXECUTION payload arrives, the listener opens the rehab screen instead of applying directly. The left pane holds the instructions; the right pane is deliberately blank.

![The rehab screen showing plain-English instructions with the solution hidden](../images/usecases/uc4-01-instructions.png)

For a modification you get the target code and a description of what should happen to it, phrased as intent rather than implementation:

> Convert the list comprehension to a generator expression so the whole result set is never held in memory at once.

## 3. Use hints before you use the answer

Stuck is fine. Stuck for twenty minutes is not learning, it is just friction. Press `h` to reveal one hint at a time.

![A revealed hint appended to the instructions pane](../images/usecases/uc4-02-hint.png)

Hints are written to nudge rather than to solve, so the first one usually points at the technique rather than the syntax.

## 4. Write it yourself

Press `o` to open your own copy of the file in your editor, positioned at the first block being changed.

![The file open in an editor at the target line](../images/usecases/uc4-03-editor.png)

This is a scratch copy, not your real file. Nothing you do here touches the repository until you explicitly verify. Write the change as you think it should be, save, and close.

## 5. Verify against the model

Press `m` to open Meld with three panes: the model's version, the merge target in the middle, and your handwritten version.

![Meld comparing your version against the AI version](../images/usecases/uc4-04-meld-verify.png)

The centre pane is what actually gets written. That framing matters, because it means the outcome is not a pass or fail grade. If you got it right, the panes agree and you save. If you took a different but equally valid route, you can keep yours. If you missed something, you can pull the model's line across.

Save the centre pane and close Meld. The file is written and the change is marked as applied.

## 6. Reveal when you need to

If you want to see the answer, press `r`.

![The revealed AI solution in the right-hand pane](../images/usecases/uc4-05-revealed.png)

Press `r` again to hide it. There is no penalty and nothing is recorded. The hiding exists to make you attempt the change first, not to keep the answer from you.

---

## On-the-fly practice

You do not have to commit to a whole session. In an ordinary apply listener run, select any pending file and press `t`.

That opens the same rehab screen for just that one file. This is the more common way to use it in practice: apply the boilerplate normally, and practise the one change that actually contained an idea.

> [!NOTE]
> Without `--rehab` the model was never asked to write instructions, so a file opened with `t` shows the target code and the change but no plain-English explanation. It still works as a write-it-yourself exercise, just without the coaching.

---

## Tips

> [!TIP]
> Rehab pairs badly with `--divide`. Working through a twelve-task split by hand is a slog. Use it on the single interesting task instead.

> [!WARNING]
> Meld is required for the verification step. It has no Android build, so this mode is desktop-only in practice.

> [!TIP]
> If you find the instructions consistently too vague to act on, say so in the conversation. The model will write more detailed intent for subsequent payloads without slipping into writing the code out in prose.
