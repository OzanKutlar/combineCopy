# Salvaging a Mostly-Good Response

> Keys: `p` (Partial Add), `h` (Human Correct), `m` (Meld)

## The situation

The payload arrives. Four of the five files are exactly right. The fifth has a search block that does not match, or a change that is 90% what you wanted with one line you disagree with.

The instinct is to discard everything and ask again. That throws away four good changes, costs a full round trip, and often produces a new payload with a different problem. The apply TUI has three tools for keeping what is good and fixing what is not.

---

## Recognising which tool you need

![The apply TUI showing one file with a validation error alongside successful ones](../images/usecases/uc8-01-error-state.png)

| Symptom | Tool | Key |
| :--- | :--- | :--- |
| The change is right, but you want only part of it | Partial Add | `p` |
| A search block did not match anything | Human Correct | `h` |
| The replacement itself needs editing | Meld | `m` |

Files carrying an error are marked in red in the sidebar, with the reason printed above the diff. Files where the tool already corrected something for you carry a yellow marker such as `(Fuzzy Match)` or `(Path Corrected)`, which is worth a glance even when nothing failed.

---

## Partial Add: taking some of a change

Press `p` on a modified file.

![The Partial Add screen with individual words toggled](../images/usecases/uc8-02-partial-add.png)

The left pane is a word-level diff where every red and green run is independently toggleable. The right pane is a live preview of the file you will actually get.

| Key | Does |
| :--- | :--- |
| `a` / `d` | Move between changes |
| `Space` | Toggle the current change |
| Click | Toggle any change directly |

Rejected additions are shown struck through, and rejected deletions reappear in red, so at all times you can see both what you are taking and what you are leaving.

**Accept All** and **Reject All** are there for the common case of wanting almost everything or almost nothing. **Apply Custom** writes the file as previewed.

This is the tool for a change that is structurally correct but contains something you did not ask for: a helpful rename, an added comment you do not want, a defensive check that duplicates one three lines up.

---

## Human Correct: pointing at the right lines

When a search block does not match, the tool does not give up immediately. It tries an exact match, then a whitespace-normalised match, and finally looks for the closest partial match it can find. If that last step finds something, the error tells you so and invites you to press `h`.

![The Human Correct screen with candidate regions listed](../images/usecases/uc8-03-human-correct.png)

The screen has four panes:

- **Left:** candidate regions, ranked by how much of the search block they matched, with a coverage percentage
- **Top centre:** a diff of the model's search text against the selected candidate, so you can see what drifted
- **Top right:** the replacement code, so you know what will land
- **Bottom:** the actual file, with the selected candidate highlighted

Moving through the candidates scrolls the file pane to each one. When you find the right region, select it in the file pane and press **Confirm Selection**.

> [!IMPORTANT]
> Select the *whole* region to be replaced, not just the line the model got wrong. If your selection is much shorter than the search block, you get a warning and have to confirm twice, because the usual result of a short selection is duplicated code.

The corrected block is revalidated immediately, and the file is marked `(Human Corrected)` in the sidebar.

---

## Meld: editing the replacement itself

Press `m` on any pending file to open the current version and the proposed version side by side.

![Meld showing the pending change with the right pane edited](../images/usecases/uc8-04-meld.png)

The right-hand pane is editable. Anything you change and save there is folded back into the pending change when Meld closes, and the file is marked `(Meld Edited)`.

This is the tool for when the model's approach is right but its execution is not quite. Fix the variable name, correct the off-by-one, tighten the error message, then save and close. You have the model's structure with your corrections, and it applies as a single change.

> [!NOTE]
> Editing in Meld converts the change to a full-content replacement, since the search and replace blocks no longer describe what is happening. This is expected and the diff view updates to match.

> [!TIP]
> No Meld installed? The tool falls back to an inline unified diff rather than erroring out. You lose the editing, not the review.

---

## When to give up and ask again

These tools are for a response that is mostly right. When you find yourself using all three on the same payload, the model has misunderstood something structural, and correcting the output is treating the symptom.

Press `e` on the failing file to copy a formatted error report, paste it back, and let the model correct itself. The report names the file, the failure, and where the partial match landed, which is usually enough for it to see what it got wrong.

---

## Tips

> [!TIP]
> Fix everything before committing anything. Corrections revalidate in place, so you can work through the whole payload and then press `Shift+A` and `c` once at the end.

> [!NOTE]
> Discarding is not permanent within a session. `d` marks a file discarded and skips it, but the payload is still on your clipboard. Press `r` to reload it and start over.

> [!TIP]
> A file marked `(Fuzzy Match)` applied successfully, but only after the tool normalised whitespace to find the target. Worth a look at the diff before applying, especially in a whitespace-sensitive language.
