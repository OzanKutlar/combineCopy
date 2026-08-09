import os
import json
import difflib
import re
import subprocess
import hashlib
import time
import atexit
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich import box
from rich.rule import Rule
from rich.text import Text
from rich.style import Style
import pyperclip

# Initialize Rich Console
console = Console()

def extract_blocks(filepath: str, content: str) -> list[dict]:
    """Parses content to extract blocks (classes, functions, methods) with start and end line indices."""
    ext = os.path.splitext(filepath)[1].lower()
    lines = content.split('\n')
    blocks = []

    py_pattern = re.compile(r'^\s*(class|def|async def)\s+\w+')
    js_pattern = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:class\s+\w+|function\s+\w+\s*\(|(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>)')
    cs_method_pattern = re.compile(r'^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|abstract|sealed|new|final|synchronized|native|strictfp)\s+)*[\w<>\[\]\?]+\s+\w+\s*(?:<[^>]*>\s*)?\(')
    cs_ctor_pattern = re.compile(r'^\s*(?:(?:public|private|protected|internal|static)\s+)+\w+\s*\(')
    cs_class_pattern = re.compile(r'^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|partial|final|strictfp)\s+)*(?:class|struct|interface|record|enum)\s+\w+')

    cs_exclusions = {"if", "while", "for", "foreach", "switch", "catch", "using", "lock", "typeof", "sizeof", "default", "return", "throw", "new"}

    i = 0
    while i < len(lines):
        line = lines[i]
        matched = False
        name = ""
        is_brace_lang = False

        if ext in ['.py', '.pyw']:
            if py_pattern.match(line):
                matched = True
                name = line.strip()
                is_brace_lang = False
        elif ext in ['.js', '.jsx', '.ts', '.tsx']:
            if js_pattern.match(line):
                matched = True
                name = line.strip()
                is_brace_lang = True
        elif ext in ['.cs', '.java']:
            if cs_class_pattern.match(line):
                matched = True
                name = line.strip()
                is_brace_lang = True
            else:
                m = cs_method_pattern.match(line) or cs_ctor_pattern.match(line)
                if m:
                    parts = line.split('(')[0].strip().split()
                    if parts and parts[-1] not in cs_exclusions:
                        matched = True
                        name = line.strip()
                        is_brace_lang = True

        if matched:
            start_line = i
            end_line = i
            if not is_brace_lang:
                base_indent = len(line) - len(line.lstrip())
                j = i + 1
                while j < len(lines):
                    curr_line = lines[j]
                    if curr_line.strip() and not curr_line.lstrip().startswith('#'):
                        curr_indent = len(curr_line) - len(curr_line.lstrip())
                        if curr_indent <= base_indent:
                            break
                    end_line = j
                    j += 1
            else:
                brace_count = 0
                found_start = False
                j = i
                while j < len(lines):
                    curr_line = lines[j]
                    clean_line = re.sub(r'".*?(?<!\\)"', '', curr_line)
                    clean_line = re.sub(r"'.*?(?<!\\)'", '', clean_line)
                    clean_line = re.sub(r'//.*', '', clean_line)
                    
                    if not found_start:
                        if ';' in clean_line and '{' not in clean_line:
                            end_line = j
                            break
                        if '{' in clean_line:
                            found_start = True
                            brace_count += clean_line.count('{')
                            brace_count -= clean_line.count('}')
                            if brace_count <= 0:
                                end_line = j
                                break
                    else:
                        brace_count += clean_line.count('{')
                        brace_count -= clean_line.count('}')
                        if brace_count <= 0:
                            end_line = j
                            break
                    end_line = j
                    j += 1

            blocks.append({
                "name": name[:100],
                "start": start_line,
                "end": end_line
            })
        i += 1
    return blocks

def extract_signatures(filepath: str, content: str) -> list[str]:
    """Uses block extraction to retrieve class and function definitions from source code."""
    blocks = extract_blocks(filepath, content)
    return [b["name"] for b in blocks]

# --- AST Caching System ---

_ast_cache = {}
_ast_cache_path = ""
_ast_cache_dirty = False

def init_ast_cache(root_dir: str) -> None:
    global _ast_cache, _ast_cache_path
    dir_path = os.path.expanduser("~/.configs/combineCopy")
    try:
        os.makedirs(dir_path, exist_ok=True)
    except Exception:
        pass
    cwd_hash = hashlib.md5(os.path.abspath(root_dir).encode('utf-8')).hexdigest()
    _ast_cache_path = os.path.join(dir_path, f"ast_cache_{cwd_hash}.json")
    if os.path.exists(_ast_cache_path):
        try:
            with open(_ast_cache_path, 'r', encoding='utf-8') as f:
                _ast_cache = json.load(f)
        except Exception:
            _ast_cache = {}

def save_ast_cache() -> None:
    global _ast_cache_dirty, _ast_cache_path
    if _ast_cache_dirty and _ast_cache_path:
        try:
            with open(_ast_cache_path, 'w', encoding='utf-8') as f:
                json.dump(_ast_cache, f)
            _ast_cache_dirty = False
        except Exception:
            pass

atexit.register(save_ast_cache)

def prime_ast_cache(root_dir: str, files: list[str]) -> None:
    global _ast_cache, _ast_cache_dirty
    init_ast_cache(root_dir)
    
    new_or_mod_count = 0
    loaded_count = 0
    start_time = time.time()
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task = progress.add_task("[cyan]Updating AST Cache...", total=len(files))
        for f in files:
            rel_path = os.path.relpath(f, root_dir).replace("\\", "/")
            try:
                mtime = os.path.getmtime(f)
            except Exception:
                mtime = 0
                
            cached = _ast_cache.get(rel_path)
            if cached and cached.get("mtime") == mtime:
                loaded_count += 1
            else:
                new_or_mod_count += 1
                content = safe_read_file(f)
                blocks = []
                if content and not content.startswith("(This is a binary"):
                    blocks = extract_blocks(f, content)
                _ast_cache[rel_path] = {"mtime": mtime, "blocks": blocks}
                _ast_cache_dirty = True
                
            progress.advance(task)
            
    save_ast_cache()
    elapsed = time.time() - start_time
    if new_or_mod_count > 0 or elapsed > 0.5:
        console.print(f"[bold green]✓[/bold green] AST Cache Ready ({elapsed:.1f}s) — [cyan]{new_or_mod_count}[/cyan] parsed, [cyan]{loaded_count}[/cyan] loaded from cache.")

def get_cached_blocks(filepath: str, root_dir: str) -> list[dict]:
    global _ast_cache, _ast_cache_dirty
    if not _ast_cache_path:
        init_ast_cache(root_dir)
        
    rel_path = os.path.relpath(filepath, root_dir).replace("\\", "/")
    try:
        mtime = os.path.getmtime(filepath)
    except Exception:
        mtime = 0
        
    cached = _ast_cache.get(rel_path)
    if cached and cached.get("mtime") == mtime:
        return cached.get("blocks", [])
        
    content = safe_read_file(filepath)
    blocks = []
    if content and not content.startswith("(This is a binary"):
        blocks = extract_blocks(filepath, content)
    
    _ast_cache[rel_path] = {"mtime": mtime, "blocks": blocks}
    _ast_cache_dirty = True
    return blocks

def get_cached_signatures(filepath: str, root_dir: str) -> list[str]:
    blocks = get_cached_blocks(filepath, root_dir)
    return [b["name"] for b in blocks]

def search_ast_for_functions(func_names: list[str], root_dir: str) -> dict:
    """Searches the AST cache for files containing the given function/class names."""
    global _ast_cache, _ast_cache_path
    if not _ast_cache_path:
        init_ast_cache(root_dir)

    results = {name: [] for name in func_names}
    for rel_path, data in _ast_cache.items():
        blocks = data.get("blocks", [])
        for b in blocks:
            for name in func_names:
                if name in b["name"]:
                    if rel_path not in results[name]:
                        results[name].append(rel_path)
    return results

def get_blocks_by_name(filepath: str, root_dir: str, name: str) -> list[dict]:
    blocks = get_cached_blocks(filepath, root_dir)
    return [b for b in blocks if name in b["name"]]

def is_binary_file(filepath: str) -> bool:
    """Check if a file is binary by extension or by looking for null bytes in the first 8192 bytes."""
    binary_extensions = {
        '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff',
        '.zip', '.tar', '.gz', '.7z', '.rar',
        '.exe', '.dll', '.so', '.dylib', '.bin',
        '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.mp3', '.mp4', '.avi', '.mkv', '.mov', '.wav', '.flac',
        '.pyc', '.pyd', '.o', '.a', '.class', '.jar'
    }
    _, ext = os.path.splitext(filepath)
    if ext.lower() in binary_extensions:
        return True
        
    try:
        with open(filepath, 'rb') as f:
            chunk = f.read(8192)
            if b'\0' in chunk:
                return True
            return False
    except Exception:
        return False

def safe_read_file(path: str) -> str:
    """Reads a file as UTF-8. Falls back to surrogateescape so invalid bytes
    can round-trip losslessly when written back. Ignores binary files."""
    if is_binary_file(path):
        return "(This is a binary file)"
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
                return f.read()
        except Exception:
            return "(This is a binary file)"

def detect_newline(path: str) -> str:
    """Peeks at a file in binary mode and returns its dominant line ending.
    Returns '\\r\\n', '\\n', '\\r', or '' (no newline found)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(65536)
        if b"\r\n" in chunk:
            return "\r\n"
        if b"\n" in chunk:
            return "\n"
        if b"\r" in chunk:
            return "\r"
        return ""
    except Exception:
        return ""

def extract_xml_from_text(text: str) -> list[str]:
    """Extracts XML antigravity payloads from text."""
    results = []
    import re
    code_blocks = re.findall(r'```(?:xml)?\s*(<antigravity_payload>.*?</antigravity_payload>)\s*```', text, re.DOTALL | re.IGNORECASE)
    if code_blocks:
        return code_blocks
        
    # Fallback heuristic
    start_idx = text.find('<antigravity_payload>')
    end_idx = text.rfind('</antigravity_payload>')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        results.append(text[start_idx:end_idx+22])
    return results
def find_line_number(full_text: str, search_text: str) -> int:
    """Finds the 1-based line number of the target block for editor launching."""
    if not search_text: return 1
    idx = full_text.find(search_text)
    if idx != -1:
        return full_text.count('\n', 0, idx) + 1
    
    norm_search = "\n".join(l.strip() for l in search_text.strip().split('\n') if l.strip())
    source_lines = full_text.split('\n')
    for i in range(len(source_lines)):
        for j in range(i, len(source_lines)):
            window = '\n'.join(source_lines[i : j + 1])
            nw = "\n".join(l.strip() for l in window.strip().split('\n') if l.strip())
            if nw == norm_search:
                return i + 1
            elif len(nw) > len(norm_search):
                break
    return 1

def parse_xml_to_dict(xml_str: str) -> dict:
    """Parses the strict <antigravity_payload> schema back into the equivalent JSON dict format."""
    import re
    
    def get_tag_val(xml_chunk, tag):
        m = re.search(f'<{tag}>(.*?)</{tag}>', xml_chunk, re.DOTALL)
        if not m: return None
        val = m.group(1)
        cdata_m = re.search(r'<!\[CDATA\[(.*?)\]\]>', val, re.DOTALL)
        if cdata_m: return cdata_m.group(1)
        return val.strip() if not val.isspace() else val
        
    data = {}
    data["phase"] = get_tag_val(xml_str, "phase")
    data["markdown"] = get_tag_val(xml_str, "markdown")
    data["commit_message"] = get_tag_val(xml_str, "commit_message")
    data["original_request"] = get_tag_val(xml_str, "original_request")
    data["prompt"] = get_tag_val(xml_str, "prompt")
    
    # Handle EXPLORATION req_files
    req_files_m = re.search(r'<request_files>(.*?)</request_files>', xml_str, re.DOTALL)
    if req_files_m:
        data["request_files"] = re.findall(r'<path>(.*?)</path>', req_files_m.group(1), re.DOTALL)
        
    # Handle files
    files_m = re.search(r'<files>(.*?)</files>', xml_str, re.DOTALL)
    if files_m:
        if data["phase"] in ("ORCHESTRATE", "SELECT"):
            data["files"] = [p.strip() for p in re.findall(r'<path>(.*?)</path>', files_m.group(1), re.DOTALL)]
        else:
            files = []
            for file_chunk in re.findall(r'<file>(.*?)</file>', files_m.group(1), re.DOTALL):
                f_obj = {}
                f_obj["action"] = get_tag_val(file_chunk, "action")
                f_obj["path"] = get_tag_val(file_chunk, "path")
                cmd = get_tag_val(file_chunk, "command")
                if cmd: f_obj["command"] = cmd
                content = get_tag_val(file_chunk, "content")
                if content is not None:
                    f_obj["content"] = content
                inst = get_tag_val(file_chunk, "instruction")
                if inst is not None:
                    f_obj["instruction"] = inst
                    
                hints_m = re.search(r'<hints>(.*?)</hints>', file_chunk, re.DOTALL)
                if hints_m:
                    f_obj["hints"] = [h.strip() for h in re.findall(r'<hint>(.*?)</hint>', hints_m.group(1), re.DOTALL) if h.strip()]
                
                sr_m = re.search(r'<search_replace>(.*?)</search_replace>', file_chunk, re.DOTALL)
                if sr_m:
                    sr_blocks = []
                    for block in re.findall(r'<block>(.*?)</block>', sr_m.group(1), re.DOTALL):
                        s = get_tag_val(block, "search")
                        r = get_tag_val(block, "replace")
                        i = get_tag_val(block, "instruction")
                        blk = {}
                        if i is not None:
                            blk["instruction"] = i
                            
                        b_hints_m = re.search(r'<hints>(.*?)</hints>', block, re.DOTALL)
                        if b_hints_m:
                            blk["hints"] = [h.strip() for h in re.findall(r'<hint>(.*?)</hint>', b_hints_m.group(1), re.DOTALL) if h.strip()]
                            
                        if s is not None and r is not None:
                            blk["search"] = s
                            blk["replace"] = r
                            sr_blocks.append(blk)
                    if sr_blocks:
                        f_obj["search_replace"] = sr_blocks
                        
                rr_m = re.search(r'<regex_replace>(.*?)</regex_replace>', file_chunk, re.DOTALL)
                if rr_m:
                    rr_blocks = []
                    for block in re.findall(r'<block>(.*?)</block>', rr_m.group(1), re.DOTALL):
                        p = get_tag_val(block, "pattern")
                        r = get_tag_val(block, "replacement")
                        if p is not None and r is not None:
                            rr_blocks.append({"pattern": p, "replacement": r})
                    if rr_blocks:
                        f_obj["regex_replace"] = rr_blocks
                        
                files.append(f_obj)
            data["files"] = files
    # Handle SELECT functions
    funcs_m = re.search(r'<functions>(.*?)</functions>', xml_str, re.DOTALL)
    if funcs_m:
        funcs = []
        for item in re.findall(r'<item>(.*?)</item>', funcs_m.group(1), re.DOTALL):
            path = get_tag_val(item, "path")
            names = re.findall(r'<name>(.*?)</name>', item, re.DOTALL)
            if path:
                funcs.append({"path": path, "names": names})
        data["functions"] = funcs
        
    # Handle TASK queries
    mega = get_tag_val(xml_str, "mega_task_name")
    if mega: data["mega_task_name"] = mega

    tasks_m = re.search(r'<tasks>(.*?)</tasks>', xml_str, re.DOTALL)
    if tasks_m:
        tasks_list = []
        for task_chunk in re.findall(r'<task>(.*?)</task>', tasks_m.group(1), re.DOTALL):
            t_obj = {}
            t_name = get_tag_val(task_chunk, "task_name")
            if t_name: t_obj["task_name"] = t_name
            sp = get_tag_val(task_chunk, "sub_prompt")
            if sp: t_obj["sub_prompt"] = sp
            
            files_m2 = re.search(r'<files>(.*?)</files>', task_chunk, re.DOTALL)
            if files_m2:
                t_obj["files"] = re.findall(r'<path>(.*?)</path>', files_m2.group(1), re.DOTALL)
                
            funcs_m2 = re.search(r'<functions>(.*?)</functions>', task_chunk, re.DOTALL)
            if funcs_m2:
                funcs = []
                for item in re.findall(r'<item>(.*?)</item>', funcs_m2.group(1), re.DOTALL):
                    path = get_tag_val(item, "path")
                    names = re.findall(r'<name>(.*?)</name>', item, re.DOTALL)
                    if path:
                        funcs.append({"path": path, "names": names})
                t_obj["functions"] = funcs
                
            tasks_list.append(t_obj)
        data["tasks"] = tasks_list

    # Handle CONSULT queries
    queries_m = re.search(r'<queries>(.*?)</queries>', xml_str, re.DOTALL)
    if queries_m:
        queries = []
        for q_chunk in re.findall(r'<query>(.*?)</query>', queries_m.group(1), re.DOTALL):
            q_id = get_tag_val(q_chunk, "id")
            q_text = get_tag_val(q_chunk, "question")
            if q_id and q_text:
                queries.append({"id": q_id, "question": q_text})
        data["queries"] = queries
        
    return data

def extract_json_from_text(text: str) -> list[str]:
    """Extracts JSON objects from text using multiple strategies."""
    results = []
    
    # Strategy A: Markdown code blocks
    import re
    code_blocks = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if code_blocks:
        return code_blocks
    # Strategy B: First { to last } (Heuristic for single payload)
    if '"phase"' in text and any(k in text for k in ['"EXECUTION"', '"ORCHESTRATE"', '"EXPLORATION"', '"SELECT"', '"CONSULT"', '"TASK"']):
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            results.append(text[start_idx:end_idx+1])
            return results

    # Strategy C: State machine fallback
    idx = 0
    while idx < len(text):
        start_idx = text.find('{', idx)
        if start_idx == -1:
            break
        depth = 0
        in_string = False
        escape_next = False
        end_idx = -1
        for i in range(start_idx, len(text)):
            char = text[i]
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        end_idx = i
                        break
        if end_idx != -1:
            results.append(text[start_idx:end_idx + 1])
            idx = end_idx + 1
        else:
            idx = start_idx + 1
    return results

def intelligent_json_fix(content: str) -> tuple[dict | None, str]:
    """Iteratively attempts to heal common LLM JSON syntax errors (like unescaped quotes)."""
    import re
    
    def escape_html_attr(m):
        attr = m.group(1)
        val = m.group(2)
        return f'{attr}=\\"{val}\\"'

    # Pre-pass: Fix common HTML/JSX unescaped attributes (e.g., className="flex" -> className=\"flex\")
    content = re.sub(r'([a-zA-Z0-9_\-]+)\s*=\s*"(.*?)(?<!\\)"', escape_html_attr, content)
    
    def escape_conditional(m):
        op = m.group(1)
        val = m.group(2)
        return f'{op} \\"{val}\\"'

    # Pre-pass: Fix common programming conditionals/returns (e.g., == "foo" -> == \"foo\")
    content = re.sub(r'(==|===|!=|!==|\+=|-=|\*=|/=|return)\s*"(.*?)(?<!\\)"', escape_conditional, content)

    lines = content.split('\n')
    for i, line in enumerate(lines):
        match = re.match(r'^(\s*"[^"]+"\s*:\s*")(.*)$', line)
        if match:
            prefix = match.group(1)
            rest = match.group(2)
            suffix_match = re.search(r'("\s*(?:,|},|})?\s*)$', rest)
            if suffix_match:
                suffix = suffix_match.group(1)
                value = rest[:-len(suffix)]
                fixed_value = re.sub(r'(?<!\\)"', r'\"', value)
                lines[i] = prefix + fixed_value + suffix
            else:
                fixed_value = re.sub(r'(?<!\\)"', r'\"', rest)
                lines[i] = prefix + fixed_value
                
    current = '\n'.join(lines)

    for _ in range(10000):
        try:
            data = json.loads(current)
            return data, current
        except json.JSONDecodeError as e:
            msg = e.msg
            pos = e.pos
        
            if "Invalid control character" in msg:
                if pos < len(current):
                    char = current[pos]
                    if char == '\n':
                        current = current[:pos] + '\\n' + current[pos+1:]
                    elif char == '\t':
                        current = current[:pos] + '\\t' + current[pos+1:]
                    elif char == '\r':
                        current = current[:pos] + '\\r' + current[pos+1:]
                    else:
                        current = current[:pos] + '\\u{:04x}'.format(ord(char)) + current[pos+1:]
                    continue
                    
            if "Invalid \\escape" in msg:
                if pos > 0 and current[pos-1] == '\\':
                    current = current[:pos-1] + '\\\\' + current[pos:]
                    continue
                
            if "Expecting ',' delimiter" in msg or "Expecting value" in msg or "Extra data" in msg:
                last_quote = current.rfind('"', 0, pos)
                if last_quote != -1 and current[last_quote-1] != '\\':
                    current = current[:last_quote] + '\\"' + current[last_quote+1:]
                    continue
                    
            break
    return None, content

def apply_diff_patch(original_text: str, search_text: str, replace_text: str) -> str:
    source_lines = original_text.splitlines(keepends=True)
    search_lines = search_text.splitlines(keepends=False)
    replace_lines = replace_text.splitlines(keepends=False)

    def _norm(t: str) -> str:
        return "\n".join(l.strip() for l in t.strip().split('\n') if l.strip())

    norm_search = _norm(search_text)
    
    # 1. Find the Source Window
    start_idx = -1
    end_idx = -1
    
    if len(search_lines) <= len(source_lines):
        for i in range(len(source_lines) - len(search_lines) + 1):
            window_text = "".join(source_lines[i:i+len(search_lines)])
            if _norm(window_text) == norm_search:
                start_idx = i
                end_idx = i + len(search_lines) - 1
                break
                
    if start_idx == -1:
        for i in range(len(source_lines)):
            window_text = ""
            for j in range(i, len(source_lines)):
                window_text += source_lines[j]
                nw = _norm(window_text)
                if nw == norm_search:
                    start_idx = i
                    end_idx = j
                    break
                elif len(nw) > len(norm_search):
                    break
            if start_idx != -1:
                break
                
    if start_idx == -1:
        return original_text # Cannot find patch location

    window_lines = source_lines[start_idx:end_idx+1]
    
    # 2. Calculate Indentation Delta
    source_indent = ""
    for line in window_lines:
        if line.strip():
            source_indent = line[:len(line) - len(line.lstrip())]
            break
    
    search_indent = ""
    for line in search_lines:
        if line.strip():
            search_indent = line[:len(line) - len(line.lstrip())]
            break
            
    def apply_delta(line: str) -> str:
        if not line.strip(): return line
        if search_indent and line.startswith(search_indent):
            return source_indent + line[len(search_indent):]
        return source_indent + line if not search_indent else line

    # 3. Align and Patch using Myers Diff
    diff_matcher = difflib.SequenceMatcher(None, search_lines, replace_lines)
    patched_window = []
    w_ptr = 0
    
    for tag, i1, i2, j1, j2 in diff_matcher.get_opcodes():
        if tag == 'equal':
            target_content = sum(1 for sl in search_lines[i1:i2] if sl.strip())
            if target_content > 0:
                matched_content = 0
                while w_ptr < len(window_lines):
                    w_line = window_lines[w_ptr]
                    patched_window.append(w_line)
                    w_ptr += 1
                    if w_line.strip():
                        matched_content += 1
                    if matched_content == target_content:
                        break
        elif tag == 'delete':
            target_content = sum(1 for sl in search_lines[i1:i2] if sl.strip())
            if target_content > 0:
                matched_content = 0
                while w_ptr < len(window_lines):
                    w_line = window_lines[w_ptr]
                    w_ptr += 1
                    if w_line.strip():
                        matched_content += 1
                    if matched_content == target_content:
                        break
        elif tag == 'insert':
            for r_line in replace_lines[j1:j2]:
                patched_line = apply_delta(r_line)
                nl = '\r\n' if '\r\n' in original_text else '\n'
                patched_window.append(patched_line + nl)
        elif tag == 'replace':
            target_content = sum(1 for sl in search_lines[i1:i2] if sl.strip())
            if target_content > 0:
                matched_content = 0
                while w_ptr < len(window_lines):
                    w_line = window_lines[w_ptr]
                    w_ptr += 1
                    if w_line.strip():
                        matched_content += 1
                    if matched_content == target_content:
                        break
            for r_line in replace_lines[j1:j2]:
                patched_line = apply_delta(r_line)
                nl = '\r\n' if '\r\n' in original_text else '\n'
                patched_window.append(patched_line + nl)

    while w_ptr < len(window_lines):
        patched_window.append(window_lines[w_ptr])
        w_ptr += 1
        
    prefix = source_lines[:start_idx]
    suffix = source_lines[end_idx+1:]
    return "".join(prefix + patched_window + suffix)

def compute_new_text(file_obj: dict, old_text: str) -> str:
    if "content" in file_obj:
        return file_obj["content"]
    new_text = old_text
    for block in file_obj.get("search_replace", []):
        search = block.get("search", "")
        replace = block.get("replace", "")
        if search and search in new_text:
            new_text = new_text.replace(search, replace, 1)
        else:
            if search:
                patched = apply_diff_patch(new_text, search, replace)
                if patched != new_text:
                    new_text = patched
                else:
                    # Fallback fuzzy match if diff patching fails to find window
                    search_norm = "\n".join(l.strip() for l in search.strip().split('\n') if l.strip())
                    source_lines = new_text.split('\n')
                    found = False
                    for i in range(len(source_lines)):
                        for j in range(i, len(source_lines)):
                            window = '\n'.join(source_lines[i : j + 1])
                            window_norm = "\n".join(l.strip() for l in window.strip().split('\n') if l.strip())
                            if window_norm == search_norm:
                                new_text = new_text.replace(window, replace, 1)
                                found = True
                                break
                            elif len(window_norm) > len(search_norm):
                                break
                        if found:
                            break
    for block in file_obj.get("regex_replace", []):
        pattern = block.get("pattern", "")
        replacement = block.get("replacement", "")
        if pattern:
            try:
                new_text = re.sub(pattern, replacement, new_text)
            except re.error:
                pass
    return new_text

def render_word_diff(old_text: str, new_text: str, diff_view) -> None:
    """Calculates word-level diffs and outputs them to the rich RichLog container."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            if i2 - i1 > 6:
                if i1 != 0:
                    for line in old_lines[i1:i1+3]:
                        diff_view.write(Text("  " + line.rstrip('\n'), style="dim"))
                diff_view.write(Text("...", style="bold dim"))
                if i2 != len(old_lines):
                    for line in old_lines[i2-3:i2]:
                        diff_view.write(Text("  " + line.rstrip('\n'), style="dim"))
            else:
                for line in old_lines[i1:i2]:
                    diff_view.write(Text("  " + line.rstrip('\n'), style="dim"))
        else:
            old_hunk = "".join(old_lines[i1:i2])
            new_hunk = "".join(new_lines[j1:j2])
            
            old_words = re.findall(r'\S+|\s+', old_hunk)
            new_words = re.findall(r'\S+|\s+', new_hunk)
            
            word_matcher = difflib.SequenceMatcher(None, old_words, new_words)
            
            diff_text = Text()
            for w_tag, wi1, wi2, wj1, wj2 in word_matcher.get_opcodes():
                if w_tag == 'equal':
                    diff_text.append("".join(old_words[wi1:wi2]))
                elif w_tag == 'delete':
                    diff_text.append("".join(old_words[wi1:wi2]), style="white on red")
                elif w_tag == 'insert':
                    diff_text.append("".join(new_words[wj1:wj2]), style="black on green")
                elif w_tag == 'replace':
                    diff_text.append("".join(old_words[wi1:wi2]), style="white on red strike")
                    diff_text.append("".join(new_words[wj1:wj2]), style="black on green")
            
            diff_view.write(diff_text)
def copy_to_clipboard(text: str) -> bool:
    """Copies text to the clipboard, routing through the Termux backend on mobile.

    Android's clipboard write path has a practical size ceiling, so oversized
    prompts are staged to a file rather than failing silently.
    """
    from combinecopy.mobile.clipboard import copy_text, stage_outbound, CLIPBOARD_SIZE_LIMIT
    from combinecopy.mobile.env import is_termux

    if copy_text(text):
        return True

    if is_termux():
        path = stage_outbound(text)
        if path:
            size_kb = len(text.encode("utf-8", errors="surrogateescape")) / 1024
            console.print(
                f"[yellow]Prompt is too large for the Android clipboard "
                f"({size_kb:.0f} KB > {CLIPBOARD_SIZE_LIMIT // 1024} KB).[/yellow]"
            )
            console.print(f"[bold cyan]Staged to:[/bold cyan] {path}")
            console.print(f"[dim]Share it out with:[/dim] termux-share -a send {path}")
            return False

    console.print("[bold red]Error copying to clipboard.[/bold red]")
    return False


def copy_file_to_clipboard(filepath: str) -> bool:
    """Copies a file object to the clipboard. Windows only - it relies on PowerShell."""
    if os.name != "nt":
        console.print(
            "[yellow]--file clipboard attachments are Windows-only; "
            "falling back to plain text.[/yellow]"
        )
        return False
    try:
        abs_path = os.path.abspath(filepath)
        subprocess.run(["powershell", "-command", f"Set-Clipboard -Path '{abs_path}'"], check=True)
        return True
    except Exception as e:
        console.print(f"[bold red]Error copying file to clipboard:[/bold red] {e}")
        return False

def get_default_rules_path(root_dir: str | None = None) -> str:
    """Returns the absolute path to default_rules.json in workspace or package root."""
    if root_dir:
        workspace_path = os.path.join(root_dir, "default_rules.json")
        if os.path.exists(workspace_path):
            return workspace_path
    
    pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    pkg_json_path = os.path.join(pkg_root, "default_rules.json")
    if os.path.exists(pkg_json_path):
        return pkg_json_path
        
    current_dir_path = os.path.join(os.path.dirname(__file__), "default_rules.json")
    if os.path.exists(current_dir_path):
        return current_dir_path
        
    return os.path.join(root_dir or os.getcwd(), "default_rules.json")

def load_default_rules(root_dir: str | None = None) -> list[dict]:
    """Loads default rules array from default_rules.json."""
    path = get_default_rules_path(root_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def save_default_rule(title: str, content: str, description: str = "", root_dir: str | None = None) -> bool:
    """Appends or updates a default rule in default_rules.json."""
    path = get_default_rules_path(root_dir)
    rules = load_default_rules(root_dir)
    
    clean_title = title.strip()
    if not clean_title:
        return False

    rule_id = clean_title.lower().replace(" ", "_")
    rule_id = re.sub(r'[^a-z0-9_]', '', rule_id)
    
    found = False
    for r in rules:
        if r.get("title", "").strip().lower() == clean_title.lower():
            r["content"] = content
            if description:
                r["description"] = description
            found = True
            break
            
    if not found:
        rules.append({
            "id": rule_id,
            "title": clean_title,
            "description": description or f"Custom rule for {clean_title}",
            "content": content
        })
        
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

def get_files_recursive(directory, current_depth, max_depth, extensions, exclude_dirs=None):
    """Recursively scans for files up to a certain depth."""
    file_list = []
    try:
        items = sorted(os.listdir(directory))
    except PermissionError:
        return []

    files = []
    dirs = []
    ignore_dirs = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode"}
    if exclude_dirs:
        ignore_dirs.update(exclude_dirs)
    for item in items:
        full_path = os.path.join(directory, item)
        if os.path.isdir(full_path) and item in ignore_dirs:
            continue
        if os.path.isfile(full_path):
            files.append(full_path)
        elif os.path.isdir(full_path):
            dirs.append(full_path)

    for f_path in files:
        if extensions:
            if not f_path.lower().endswith(tuple(extensions)):
                continue
        file_list.append(f_path)

    if current_depth < max_depth:
        for d_path in dirs:
            file_list.extend(get_files_recursive(d_path, current_depth + 1, max_depth, extensions, exclude_dirs))
            
    return file_list

def generate_tree_string(files, root_dir) -> str:
    """Generates a string representation of the directory tree, enriched with AST signatures."""
    tree_dict = {}
    for f in files:
        rel_path = os.path.relpath(f, root_dir).replace("\\", "/")
        parts = rel_path.split("/")
        current = tree_dict
        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]
            
    signatures_map = {}
    for f in files:
        sigs = get_cached_signatures(f, root_dir)
        if sigs:
            rel_path = os.path.relpath(f, root_dir).replace("\\", "/")
            signatures_map[rel_path] = sigs

    lines = []
    def _build_lines(node, prefix="", path_prefix=""):
        entries = sorted(list(node.keys()))
        for i, key in enumerate(entries):
            is_last = (i == len(entries) - 1)
            connector = "└── " if is_last else "├── "
            lines.append(prefix + connector + key)
            
            current_path = f"{path_prefix}/{key}" if path_prefix else key
            
            if not node[key]:
                sigs = signatures_map.get(current_path, [])
                extension = "    " if is_last else "│   "
                for j, sig in enumerate(sigs):
                    sig_is_last = (j == len(sigs) - 1)
                    sig_connector = "└── " if sig_is_last else "├── "
                    lines.append(prefix + extension + sig_connector + "[AST] " + sig)
            else:
                extension = "    " if is_last else "│   "
                _build_lines(node[key], prefix + extension, current_path)
    _build_lines(tree_dict)
    return "\n".join(lines)

def print_auto_summary(result: dict) -> None:
    """Prints a rich summary of modifications applied."""
    if not result:
        return
    console.print()
    console.print(Rule("[bold blue]AI Agent Execution Summary[/bold blue]"))
    files = result.get("files", [])
    commit_msg = result.get("commit_message", "No commit message")
    applied_files = [f for f in files if f.get("_status") == "applied"]
    
    if not applied_files:
        from rich.panel import Panel
        console.print(Panel("No files were applied.", title="Result", style="bold yellow"))
        return
        
    for f in applied_files:
        action = f.get("action", "modify").lower()
        if action == "command":
            command = f.get("command", "Unknown")
            console.print(f"  [bold magenta]>[/bold magenta] [magenta]Ran Command[/magenta] [bold]{command}[/bold]")
        elif action == "create":
            path = f.get("path", "Unknown")
            console.print(f"  [bold green]✓[/bold green] [green]Created File[/green] [bold]{path}[/bold]")
        elif action == "delete":
            path = f.get("path", "Unknown")
            console.print(f"  [bold red]✗[/bold red] [red]Deleted File[/red] [bold]{path}[/bold]")
        else:
            path = f.get("path", "Unknown")
            added = f.get("_added", 0)
            removed = f.get("_removed", 0)
            diff_str = f" [bold green]+{added}[/bold green] [bold red]-{removed}[/bold red]" if (added > 0 or removed > 0) else ""
            console.print(f"  [bold yellow]✓[/bold yellow] [yellow]Modified File[/yellow] [bold]{path}[/bold]{diff_str}")
            
    console.print()
    commit_hash = result.get("commit_hash")
    if commit_hash:
        console.print(f"  [bold cyan]Committed with message:[/bold cyan] {commit_msg}")
    else:
        console.print(f"  [bold yellow]Note: Changes were applied but not committed to git.[/bold yellow]")
    console.print(Rule("[bold green]Done[/bold green]"))

def display_summary(root_dir, max_depth, extensions, batch_count, total_files) -> None:
    """Prints a pretty table configuration summary."""
    table = Table(title="Job Configuration", box=box.ROUNDED)
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")
    
    ext_str = ", ".join(extensions) if extensions else "All (*.*)"
    table.add_row("Root Directory", root_dir)
    table.add_row("Max Depth", str(max_depth))
    table.add_row("File Extensions", ext_str)
    table.add_row("Batches", str(batch_count))
    table.add_row("Total Files Found", f"[bold green]{total_files}[/bold green]")
    console.print(table)

def resolve_paths(requested_paths: set, known_files: list[str], root_dir: str) -> tuple[dict, dict, list]:
    """Resolves requested paths using Exact, Suffix, and Basename matching."""
    resolved = {}
    ambiguous = {}
    missing = []
    
    known_rel = [os.path.relpath(f, root_dir).replace("\\", "/") for f in known_files]
    known_set = set(known_rel)
    
    for req in requested_paths:
        req_clean = req.replace("\\", "/")
        # 1. Exact Match
        if req_clean in known_set:
            resolved[req] = req_clean
            continue
            
        # 2. Suffix Match
        suffix_matches = [p for p in known_rel if p.endswith(req_clean)]
        if len(suffix_matches) == 1:
            resolved[req] = suffix_matches[0]
            continue
        elif len(suffix_matches) > 1:
            ambiguous[req] = suffix_matches
            continue
            
        # 3. Basename Match
        base = os.path.basename(req_clean)
        base_matches = [p for p in known_rel if os.path.basename(p) == base]
        if len(base_matches) == 1:
            resolved[req] = base_matches[0]
        elif len(base_matches) > 1:
            ambiguous[req] = base_matches
        else:
            missing.append(req)
            
    return resolved, ambiguous, missing

def extract_consult_answers(text: str) -> dict | None:
    """Extracts external LLM consultation results from clipboard text."""
    # Try JSON first
    json_blocks = extract_json_from_text(text)
    for j_str in json_blocks:
        data, _ = intelligent_json_fix(j_str)
        if data and isinstance(data, dict) and "answers" in data:
            answers = {}
            for item in data["answers"]:
                if isinstance(item, dict) and "id" in item and "answer" in item:
                    val = str(item["answer"]).strip()
                    if val and val != "Your detailed answer here":
                        answers[item["id"]] = val
            if answers:
                return answers

    # Fallback to XML
    if "<consultation_results>" not in text:
        return None
    m = re.search(r'<consultation_results>(.*?)</consultation_results>', text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    answers = {}
    # Standard strict format
    for ans in re.findall(r'<answer\s+id="(.*?)">(.*?)</answer>', m.group(1), re.DOTALL | re.IGNORECASE):
        val = ans[1].strip()
        if val and val != "Your detailed answer here":
            answers[ans[0]] = val
    # Fallback for LLMs that use single quotes or no quotes
    for ans in re.findall(r'<answer\s+id=(?:\"|\'|)(.*?)(?:\"|\'|)>\s*(.*?)</answer>', m.group(1), re.DOTALL | re.IGNORECASE):
        val = ans[1].strip()
        if ans[0] not in answers and val and val != "Your detailed answer here":
            answers[ans[0]] = val
    return answers if answers else None
