# Learning From Every Change

> Flags: `--rehab`, `--rehab-level`, `--rehab-journal`, `--rehab-review`, and the `t` `n` `k` `g` keys in the apply listener

## The situation

You can still read code fluently, but when you sit down to write a non-trivial change from scratch, you reach for the model before you reach for the keyboard. You do not want to give the model up, and you do not want every change to crawl either. You want to come out of each one understanding it.

Rehab Mode never stands between the model's code and your files. The code lands exactly as it would without it. What Rehab adds is a small teaching layer that rides in the same EXECUTION payload, so there are no extra prompts and no extra round trips.

---

## 1. Pick a level

| Level | What rides along with the code | What it costs you |
| :--- | :--- | :--- |
| `explain` | A short lesson on every block that carries an idea | Nothing. Read it or skip it |
| `quiz` | Lessons, plus up to three questions about the change, asked right after the file lands | About thirty seconds per payload |
| `cloze` | Lessons and quiz, plus up to three key lines left for you to write | Only the lines that were blanked |

Each level includes the ones above it, and `quiz` is the default. Set your usual level once with `combineCopy --settings` (turn `rehab` on and pick `rehab_level`), or choose one for a single run:

```bash
combineCopy -f py -s --rehab-level cloze --apply --system
```

`--rehab-level` implies `--rehab`. The system prompt only carries the instructions for the level you chose, so the lighter levels cost fewer tokens.

## 2. Read the lessons

Every level starts here. Each block that holds a real idea carries a lesson, printed in magenta above the diff:

```
LESSON  Block 1: Lazy evaluation with a generator
    Why: summarise() walks the rows once, so they never need to sit in memory together.
    Watch out: A generator is single-pass, so a second loop over rows sees nothing.
```

The model is told to skip lessons on boilerplate, so imports and renames stay quiet. Apply as normal with `a` or `Shift+A`. Nothing waits for you.

## 3. Answer the quiz

At the `quiz` and `cloze` levels, the moment a file with questions is applied, its quiz opens. The change you just applied sits on the left and the question sits on the right.

| Key | Does |
| :--- | :--- |
| `1`-`4` | Answer |
| Enter | Next question, once you have answered |
| Esc | Skip the rest of the quiz |

Questions are about the code in front of you: what happens if a guard is removed, if an input is empty, if the function runs twice. You are shown the right answer and a one-line explanation either way. With `Shift+A`, the next file is only applied once the current quiz closes, so every question is asked while its change is fresh.

In the CLI listener the quiz runs inline after `a` or `A`. Type `v` at any question to print the change again. Press `t` on an applied file to retake its quiz.

## 4. Write the blanks

At the `cloze` level, the model also marks up to three key lines in its change. The change is applied with those lines swapped for markers:

```python
def load_report(handle):
    # TODO(rehab-1): Stop holding every parsed row in memory at once.
    pass  # <- rehab: replace this line
    # END(rehab-1)
    return summarise(rows)
```

The TODO text is the first hint. In Python a stand-in `pass` keeps the file importable while you work. Open the file in your own editor, replace the stand-in with your version, save, and come back to the listener with the file selected:

| Key | Does |
| :--- | :--- |
| `k` | Check your attempt. A match removes the markers and keeps your code. If it differs, you can keep your version anyway, since there is usually more than one right answer |
| `n` | Reveal the next hint: the approach, then pseudocode |
| `g` | Fill in the AI version, shown as a diff against what you wrote |

> [!IMPORTANT]
> Stubs are never committed by accident. Committing with blanks still open asks first, and fills them with the AI version before the commit goes through.

Blanks you leave open survive the session in `.cc_rehab_pending.json` in your repository root. Run `combineCopy --rehab-review` later to check them or fill them all at once.

## 5. Keep a journal

Turn `rehab_journal` on in the settings, or pass `--rehab-journal`, and every quiz answer and blank outcome is appended to `~/.cc_rehab/journal.jsonl`. The journal is off by default.

```bash
combineCopy --rehab-review
```

With a journal, the review re-asks every question you missed or skipped, using the stored answers and explanations, so it costs no prompts at all. It also lists the concepts you most often needed a hint or a reveal for, which is a good guide to what to practise deliberately.

---

## Tips

> [!TIP]
> Add `.cc_rehab_pending.json` to your `.gitignore`. It only exists while blanks are open and is never staged by the listener, but it will show up in `git status`.

> [!TIP]
> `cloze` pairs badly with `--divide`. Use `quiz` across a split, and switch to `cloze` for the one sub-task that actually holds an idea.

> [!NOTE]
> Web macro mode (`--web-apply`) cannot blank lines in a browser IDE, so `cloze` runs as `quiz` there. `--revert` ignores Rehab entirely.

> [!NOTE]
> Rehab no longer needs Meld, so every level works on Termux, in the TUI and in the CLI listener alike.
