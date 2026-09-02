# Use Case Walkthroughs

The root [README](../../README.md) documents what every flag does. These pages do something different. Each one takes a single realistic situation and walks it from the first command to the final commit, showing which flags to reach for and, more importantly, why.

## The Walkthroughs

| # | Scenario | Flags | The situation |
| :--- | :--- | :--- | :--- |
| 1 | [Working in a large repository](01-large-repo-culling.md) | `--file-cull` `-js` | The repo is far too big to paste, and you do not know which files matter yet |
| 2 | [Splitting a sweeping change](02-dividing-large-tasks.md) | `--divide` | One change touches a dozen files and a single payload keeps falling apart |
| 3 | [Switching a flaky model to XML](03-xml-payloads.md) | `--xml` `--system-only` | A fast, cheap model keeps producing unparseable JSON |
| 4 | [Practising instead of pasting](04-rehab-mode.md) | `--rehab` | You want the refactor done, but you also want to still be able to write it yourself |
| 5 | [Letting the agent run commands](05-cli-commands.md) | `--cli` | The change needs a dependency installed and a test run, not just file edits |
| 6 | [Reviewing your own uncommitted work](06-reviewing-your-own-diff.md) | `-d` | You want a second pair of eyes on a working tree before you push it |
| 7 | [Non-code workspaces](07-zip-and-writing.md) | `.zip` `-f tex bib` | A thesis, a manuscript, or anything else that is not source code |
| 8 | [Salvaging a mostly-good response](08-salvaging-a-response.md) | `p` `h` `m` | Four changes landed perfectly and the fifth missed |

## Conventions

Every walkthrough assumes the tool is already installed and on your PATH. See [Installation](../../README.md#installation) if it is not.

Where you see `app`, that is the built-in shortcut for `combineCopy --apply`. The two are interchangeable.

Commands are written with the flags spelled out in full the first time they appear in a walkthrough, so you can tell at a glance what each run is doing. Aliases are noted where they exist.

> [!TIP]
> Any flag you find yourself typing every single run belongs in your settings file. Run `combineCopy --settings` to set persistent defaults, and see [Settings](../../README.md#settings) for the precedence rules.
