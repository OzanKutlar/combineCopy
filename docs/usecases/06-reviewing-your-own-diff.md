# Reviewing Your Own Uncommitted Work

> Flags: `-d`, `--diff`

## The situation

You have been working for two hours. The feature works, the tests pass, and you are about to push. You would like someone to look it over first, and nobody is around.

This is the one workflow here where you are not asking for changes at all. You want a reviewer.

---

## 1. Capture the diff alongside the files

```bash
combineCopy -f py -s -d --system
```

`-d` runs `git diff HEAD` and injects the result directly into the prompt under its own heading, separate from the file context.

![The tool confirming it captured the uncommitted git diff](../images/usecases/uc6-01-diff-captured.png)

This pairing is what makes the review useful. The diff shows what you *changed*; the selected files show what you changed it *within*. A reviewer needs both, and a reviewer given only a diff will invent surrounding code that does not exist.

> [!NOTE]
> If there is nothing uncommitted, you get a warning and the run continues without a diff section. Nothing breaks.

## 2. Ask for a review, not a plan

Be explicit that you do not want an implementation. The system prompt pushes hard toward planning and executing, so say so plainly:

> Do not plan or write any code. Review the uncommitted diff below as you would a pull request. Look for logic errors, unhandled edge cases, and anything that does not match the conventions in the surrounding files. Tell me what you find and rank it by severity.

## 3. Read the review

![The model returning a prioritised review of the diff](../images/usecases/uc6-02-review-response.png)

Asking for severity ranking is worth doing. Without it you tend to get a flat list where a genuine null-dereference sits between two style opinions.

This is a conversation, so push on anything that looks wrong. A model that has misread your intent will usually admit it when challenged, and a finding that survives a challenge is one worth acting on.

## 4. Turn findings into changes, if you want

If the review turns up something real, you are already in the right conversation to fix it:

> Point 2 is right, the empty-list case does throw. Fix that one only, leave the rest alone.

The model switches to planning, then emits a payload. Catch it as normal:

```bash
combineCopy --apply
```

![Applying the fix that came out of the review](../images/usecases/uc6-03-apply.png)

Because the model has both your diff and your files in context, its search blocks match your working tree rather than the last commit, which is exactly what you need when the change is against uncommitted code.

---

## Variations

**Pre-commit sanity check.** Same run, but ask only: *does this diff do what a commit message saying X would claim it does?* Good for catching the stray debug print or the unrelated change that crept in.

**Explaining a diff you did not write.** Check out someone else's branch and run the same command. `-d` captures whatever is uncommitted, so with a stash or a working checkout you can ask for a walkthrough of unfamiliar work.

**Writing the commit message.** Ask for a message describing the diff. You get something grounded in the actual change rather than in your recollection of it.

---

## Tips

> [!TIP]
> `-d` captures `git diff HEAD`, so it includes both staged and unstaged changes but not untracked files. If your change adds a new file, select it explicitly so the reviewer can see it.

> [!TIP]
> For a large diff, add `--file-cull` and mark only the touched files as `[FULL]`. The reviewer gets your full diff plus the shape of everything around it, and can request more with a `SELECT` payload if a finding needs it.

> [!WARNING]
> A model reviewing a diff will find something, because that is what it was asked to do. Treat low-severity findings as prompts to look again, not as defects to fix.
