"""Reads the user request from stdin for --prompt-pipe.

A piped run behaves as if the user had passed --system, typed this text into
the request area and pressed send. The request area never opens.
"""

import sys

MAX_PROMPT_BYTES = 2_000_000

_UTF8_BOM = b'\xef\xbb\xbf'
_UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')
_NOTHING_PIPED = (
    'Nothing was piped into stdin. '
    'Example: cat request.txt | combineCopy -f py --prompt-pipe'
)


class PromptPipeError(Exception):
    """Raised when stdin cannot supply a usable request."""


def stdin_is_piped() -> bool:
    """True when stdin is a pipe or file rather than an interactive terminal."""
    stream = sys.stdin
    if stream is None:
        return False
    try:
        return not stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _read_bytes(limit: int) -> bytes:
    """Reads at most limit bytes, failing loudly when the input is larger."""
    stream = getattr(sys.stdin, 'buffer', None)
    if stream is None:
        raise PromptPipeError('stdin has no binary stream to read from.')
    try:
        data = stream.read(limit + 1)
    except (OSError, ValueError) as error:
        raise PromptPipeError(f'Could not read from stdin: {error}') from error
    if len(data) > limit:
        raise PromptPipeError(f'The piped request is larger than {limit:,} bytes.')
    return data


def _decode(data: bytes) -> str:
    """Decodes UTF-8, or UTF-16 when a BOM says so, and unifies line endings."""
    if data.startswith(_UTF16_BOMS):
        text = data.decode('utf-16', errors='replace')
    else:
        if data.startswith(_UTF8_BOM):
            data = data[len(_UTF8_BOM):]
        text = data.decode('utf-8', errors='replace')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def read_piped_prompt(max_bytes: int = MAX_PROMPT_BYTES) -> str:
    """Returns the request piped into stdin, stripped of surrounding whitespace.

    Raises PromptPipeError when nothing was piped, the input is empty or too
    large, or stdin cannot be read.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError(f'max_bytes must be a whole number of 1 or more, got {max_bytes!r}.')
    if not stdin_is_piped():
        raise PromptPipeError(_NOTHING_PIPED)
    request = _decode(_read_bytes(max_bytes)).strip()
    if not request:
        raise PromptPipeError('The piped request is empty.')
    return request
