# Switching a Flaky Model to XML

> Flags: `--xml`, `--system-only`

## The situation

You are using a fast, cheap model. It plans well and its logic is sound, but roughly every third EXECUTION payload fails to parse. The apply listener shows a JSON syntax error and you are back to copy-pasting error messages.

This is almost never a reasoning failure. It is an escaping failure.

---

## 1. Recognise the symptom

![The apply TUI showing a JSON parse error](../images/usecases/uc3-01-json-error.png)

The usual culprits are all the same shape:

- JSX and HTML attributes, where `className="flex"` must be written as `className=\"flex\"` inside a JSON string
- `href="#"`, which breaks in exactly the same way
- Regex patterns, where every backslash must be doubled

A model producing code that contains quotes has to escape them correctly on every single one, and smaller models simply do not do that reliably.

## 2. Try the local escape hatches first

Before changing anything, know that a one-off failure has two quick fixes in the listener:

| Key | Does |
| :--- | :--- |
| `f` | Opens the broken payload in your editor. Fix the escaping by hand, save, and it reloads |
| `e` | Copies a formatted error report to your clipboard to paste back to the model |

Both are fine once. Neither is a way to live. If you are pressing `f` several times a session, the format is the problem.

## 3. Understand why XML fixes it

In XML mode every piece of code goes inside a `CDATA` block:

```xml
<search><![CDATA[if (user.role === "admin") {]]></search>
```

Inside `CDATA`, quotes, backslashes, and angle brackets are all just characters. The model does not have to escape anything, so it cannot escape anything wrongly. The failure mode is designed out rather than corrected for.

## 4. Retrain the conversation you are already in

This is the key move. You do not need to restart, and you do not need to re-send the repository.

```bash
combineCopy --system-only --xml
```

`--system-only` copies just the system prompt to your clipboard and exits immediately. No scan, no file context, no request area. Combined with `--xml`, what lands on your clipboard is the full instruction set rewritten around the XML schema.

![The system-only success panel](../images/usecases/uc3-02-system-only.png)

Paste that into your existing conversation with a short note:

> Ignore the previous format instructions. Use these instead, and re-emit your last payload in this format.

The model keeps everything it has already read and learned about your codebase. Only the output format changes.

## 5. Confirm the switch

The next payload should be wrapped in `<antigravity_payload>` with `CDATA` blocks around every piece of code.

![The model replying with an XML payload](../images/usecases/uc3-03-xml-payload.png)

## 6. Apply as normal

```bash
combineCopy --xml --apply
```

The listener parses XML and JSON through the same pipeline, so from here everything looks and behaves identically. Same diff view, same keys, same Meld integration.

![The apply TUI diffing an XML payload](../images/usecases/uc3-04-apply-xml.png)

## 7. Make it stick

If this model is your daily driver, stop typing the flag:

```bash
combineCopy --settings
```

Select `xml` to toggle it on, then press `s` to save.

![The settings editor with xml toggled on](../images/usecases/uc3-05-settings.png)

When a saved setting is active, a dim banner names it at the start of every run, so a surprising run always explains itself.

For the occasional run where you want JSON back, any of these work:

```bash
combineCopy --xml off
combineCopy --xml=off
combineCopy --no-xml
```

The `off` word is only consumed when it appears on its own immediately after the flag, so `combineCopy --xml src/main.py` still treats the path as a path.

---

## Tips

> [!IMPORTANT]
> One naming quirk worth knowing. The search selector uses `<searches><query_item>` rather than `<search>`, because `<search>` is already reserved by the EXECUTION schema inside `<search_replace>`. The system prompt tells the model this, but it is useful to recognise when reading a payload yourself.

> [!TIP]
> If the chat box chokes on a very long paste, add `--file`. The prompt is written to a temporary file and the file object is copied to your clipboard instead of the text.

> [!NOTE]
> XML costs slightly more output tokens than JSON because of the tag overhead. On a model that handles JSON escaping reliably, there is no reason to switch. This is a fix for a specific failure, not a general upgrade.
