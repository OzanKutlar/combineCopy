# Working in a Large Repository

> Flags: `--file-cull`, `-s`, `-js`, `--system`

## The situation

The repository is far too large to paste into a chat window, and you do not yet know which files are involved. You have a symptom, a rough idea of the area, and nothing more precise than that.

Pasting everything is not an option. Guessing at a handful of files usually means the model asks for more halfway through its plan, and you end up restarting the conversation. File culling solves this by sending the *shape* of the codebase first and letting the model ask for exactly what it needs.

---

## 1. Send the map, not the code

```bash
combineCopy --file-cull -f py --system
```

`--file-cull` builds an Abstract Syntax Tree map of the workspace: every file, and beneath each file every class and function signature inside it. It also implies `--select`, so the file selector opens on its own without you passing `-s`.

![The file selector showing AST signature children listed under each file](../images/usecases/uc1-01-selector-ast.png)

## 2. Mark your anchors

Everything in the tree contributes its signatures by default. Walk to the two or three files you already suspect and press `i` to promote them to `[FULL]`. Those get their complete contents sent. Everything else stays a set of signatures.

![Two files marked FULL in magenta in the selector](../images/usecases/uc1-02-full-marked.png)

> [!TIP]
> Resist the urge to mark ten files. The entire point of this mode is to leave the model room to ask, and every file you promote is context it did not have to request.

Press `q` to confirm the selection.

## 3. Write a deliberately open request

Because the model can ask for more, you do not have to do its detective work for you. Something like this is enough:

> The retry counter double-counts failures somewhere in the worker path. Find where and fix it.

## 4. Paste, and let it look around

The model receives the directory AST map plus your anchor files. Instead of an implementation plan, its first reply should be a `SELECT` payload, requesting the context it needs to write one.

![The model replying with a SELECT payload instead of a plan](../images/usecases/uc1-03-select-payload.png)

## 5. Understand the three request types

A `SELECT` payload can mix all three freely:

```json
{
  "phase": "SELECT",
  "files": [
    "combinecopy/tui/apply.py"
  ],
  "functions": [
    {
      "path": "combinecopy/utils.py",
      "names": ["compute_new_text", "apply_diff_patch"]
    }
  ],
  "search": [
    {
      "path": "combinecopy/cli.py",
      "query": "resolve_selection_payload(",
      "regex": false,
      "case_sensitive": true
    }
  ]
}
```

| Type | Use it when |
| :--- | :--- |
| `files` | The whole file is relevant |
| `functions` | You know the exact symbol name you want |
| `search` | You know what the code *does* or how it is *called*, but not where it lives |

Search is the one worth understanding properly. It is the right tool for tracing call sites, finding every use of a config key, or locating a literal string that appears in an error message.

A search `path` can be a folder as well as a file. Every scanned file beneath the folder is searched, and only the files with a match come back. A partial path such as `tui` resolves to `combinecopy/tui`.

## 6. Feed the selection back

Copy the payload and run:

```bash
combineCopy -js -f py --system
```

`-js` reads the payload straight off your clipboard and assembles exactly what was asked for. It also accepts XML `SELECT` payloads, so this works unchanged if you are running in XML mode.

![The -js run resolving paths and reporting search hit counts](../images/usecases/uc1-04-js-resolution.png)

Three prompts can appear here, all of them recoverable:

- **Ambiguous paths.** If a requested path matches several files, you are shown the candidates and can pick `A` for all, or comma-separated numbers.
- **Missing files.** Anything that cannot be resolved is listed, and you are asked whether to continue without it. The model is told what was missing, so it can correct itself.
- **An overly broad search.** If a single query matches 100 or more files, or 50,000 or more tokens, whichever comes first, you are shown the totals and must choose. There is no default, so a stray Enter cannot accept it for you.

| Key | Does |
| :--- | :--- |
| `A` | Accept every matched file |
| `S` | Narrow the scope in the file selector, which lists only the matched files |
| `R` | Reject the query as too broad |

Narrowing or rejecting adds a note to the prompt, so the model knows its search was cut down or refused. Rejecting one gigantic file is often the best move: the model can then ask for only the functions it needs. The limits are `search_max_files` and `search_max_tokens` in `combineCopy --settings`.

Within each file, oversized results are handled automatically. When the context windows would expose 80% or more of a file, the whole file is sent instead; otherwise every match is kept as its own block. Turn `search_confirm` on to be asked per file instead, with options to truncate to the first 25 matches or skip the file. If you truncate, the model is explicitly told how many matches were withheld, so it can narrow the query and search again rather than assuming it saw everything.

## 7. Read the search note

The assembled prompt carries a `SYSTEM NOTE: SEARCH RESULTS` block reporting what each query found. Every match arrives with 50 lines of context above and below it, and matches close enough for their windows to overlap are merged into one contiguous block.

This is why a search reporting four matches can render as a single block. Skipped regions are marked with their line ranges, so the model can always tell where a block sits in the original file.

## 8. Plan, approve, apply

With the right context in hand, the model produces a plan grounded in code it has actually read.

![The narrowed implementation plan](../images/usecases/uc1-06-plan.png)

Approve it, then finish as normal:

```bash
combineCopy --apply
```

---

## Tips

> [!TIP]
> Add `--prune` when a conversation starts running long. It asks the model to declare which files it no longer needs, replacing each dropped file with a one-line reason instead of its contents.

> [!NOTE]
> The 50-line search context is fixed and deliberately not exposed to the model. It cannot ask for more, which stops a single sloppy query from swallowing the workspace.
