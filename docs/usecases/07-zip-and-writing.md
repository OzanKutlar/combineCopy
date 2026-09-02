# Non-Code Workspaces

> Flags: `.zip` paths, `-f tex bib`, and the rule catalog

## The situation

The workspace is not a codebase. It is a LaTeX thesis, a manuscript with a chapter per file, or a folder of course materials that arrived as a zip attachment.

None of that changes how the tool works. It scans a directory, filters by extension, and posts the contents into a chat. Whether those contents are Python or prose is not something it cares about.

---

## 1. Point it at the archive

```bash
combineCopy .\FENS401_402_2026_FS2.zip -f tex bib
```

Zip archives are handled natively. The archive is extracted to a temporary directory, and if it contains a single top-level folder that folder becomes the root, so you do not end up with an extra level of nesting in every path.

![The tool extracting a zip archive and scanning inside it](../images/usecases/uc7-01-extract.png)

The extension filter applies inside the archive exactly as it would on disk, so `-f tex bib` gives you the source and the bibliography while leaving out the compiled PDF, the auxiliary files, and the figures.

> [!NOTE]
> The temporary directory is cleaned up automatically when the run ends. At the end you are asked whether to delete the original archive as well, which is convenient when it came out of a downloads folder.

## 2. Select the chapters that matter

```bash
combineCopy .\thesis.zip -f tex -s --system
```

With `-s`, the selector opens over the extracted contents.

![The file selector over the extracted archive contents](../images/usecases/uc7-02-selector.png)

For a thesis, this usually means the chapter you are working on plus the two around it for continuity, rather than all fourteen. Press `x` on the directories you never want to see again, such as `figures/` or `build/`, and the ignore list persists for this workspace across future runs.

## 3. Load a writing rule set

The system prompt carries a `<user_rules>` block, and the shipped catalog includes a rule set built from Orwell's rules for clear writing. Press `F3` in the request TUI, or type `/rules` in the CLI request area.

![The rules catalog with Orwell Writing selected](../images/usecases/uc7-03-rules-tui.png)

Select **Orwell Writing** to load it. Among other things it forbids tired figures of speech, insists on the active voice, and requires the model to show you which lines it plans to replace before it replaces them.

You can save it three ways: to `.ccrules` in the workspace so it applies to every future run there, back to `default_rules.json` so it is available everywhere, or neither, using it for this run only.

> [!TIP]
> Rules are just text. Editing the catalog to add your supervisor's formatting requirements, your publisher's style guide, or a list of terms you have been told to stop using takes a minute and applies to every run afterwards.

## 4. Make your request

The request area works exactly as it does for code:

> Chapter 3 restates the argument from Chapter 2 in three separate places. Consolidate it into one passage in Chapter 2, and rewrite the Chapter 3 openings to reference it instead of repeating it.

![The plan showing which passages will be moved and rewritten](../images/usecases/uc7-04-plan.png)

With the Orwell rules loaded, the plan comes with line replacement intentions attached, so you can see exactly what is going before it goes.

## 5. Apply and review

```bash
combineCopy --apply
```

The diff TUI is where this workflow really pays off. Prose changes are word-level changes, and the word diff shows precisely which phrases moved.

Press `m` for Meld if you want to edit the replacement text before accepting it, or `p` for Partial Add to click individual words in and out. Accepting the structural change while rejecting a word choice you disagree with is a normal and expected thing to do here. See [Salvaging a mostly-good response](08-salvaging-a-response.md) for both in detail.

![The archive cleanup prompt at the end of the run](../images/usecases/uc7-05-cleanup.png)

---

## Tips

> [!TIP]
> Point at a single directory to keep the scope tight without a filter:
> ```bash
> combineCopy ".\Creative writing\Untitled Book\"
> ```

> [!NOTE]
> The AST map is built for source languages, so `--file-cull` adds little to a folder of prose. There are no function signatures to summarise. Use `-s` and `-b` to manage size instead.

> [!TIP]
> For a very large manuscript, `-b 3` splits the context across three batches. You are prompted between each one, so you paste, wait, and press Enter for the next.

> [!WARNING]
> Commit before you apply. Prose has no test suite to tell you something went wrong, and a bad replacement in the middle of a chapter is much harder to spot than a broken build.
