import os
import re
import difflib
import subprocess
import threading
from flask import Flask, request, jsonify
from rich.panel import Panel

from combinecopy.utils import (
    get_files_recursive, safe_read_file, intelligent_json_fix, 
    generate_tree_string, console, print_auto_summary, compute_new_text
)
from combinecopy.prompts import build_prompt
from combinecopy.apply_core import commit_git

app = Flask(__name__)

# Globals configured at startup
ROOT_DIR = ""
MAX_DEPTH = 100
EXTENSIONS = None
EXCLUDE_DIRS = None
APPLIED_FILES = []

def count_tokens_local(text: str, model_name: str = "cl100k_base") -> dict:
    """Calculates token counts, using tiktoken if available, with character-based fallback."""
    try:
        import tiktoken
        try:
            if model_name in ["cl100k_base", "p50k_base", "r50k_base", "o200k_base"]:
                encoding = tiktoken.get_encoding(model_name)
            else:
                encoding = tiktoken.encoding_for_model(model_name or "gpt-4")
            count = len(encoding.encode(text))
            return {
                "count": count,
                "method": "tiktoken",
                "encoding": encoding.name
            }
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")
            count = len(encoding.encode(text))
            return {
                "count": count,
                "method": "tiktoken_fallback",
                "encoding": "cl100k_base"
            }
    except ImportError:
        char_count = len(text)
        estimated = max(1, int(char_count / 4))
        return {
            "count": estimated,
            "method": "heuristic",
            "encoding": "chars_div_4"
        }

@app.route('/')
def index():
    return jsonify({
        "service": "CombineCopy Web API Server",
        "status": "running",
        "endpoints": {
            "GET /": "Service info",
            "GET /api/scan": "Scan root directory for files",
            "POST /api/generate": "Generate prompt payload based on files and request",
            "POST /api/log_copy": "Log prompt copying to terminal",
            "POST /api/preview": "Preview modifications and generate unified diffs",
            "POST /api/apply": "Apply modifications or execute commands",
            "POST /api/commit": "Commit changes via VCS and shut down",
            "POST /api/tokens/count": "Calculate token count for a given text"
        }
    })

@app.route('/api/scan')
def scan():
    files = get_files_recursive(ROOT_DIR, 0, MAX_DEPTH, EXTENSIONS, EXCLUDE_DIRS)
    rel_files = [os.path.relpath(f, ROOT_DIR) for f in files]
    return jsonify({"files": rel_files})

@app.route('/api/generate', methods=['POST'])
def generate():
    req_data = request.json or {}
    paths = req_data.get("files", [])
    user_req = req_data.get("request", "")
    
    all_files = get_files_recursive(ROOT_DIR, 0, MAX_DEPTH, EXTENSIONS, EXCLUDE_DIRS)
    ast_map = generate_tree_string(all_files, ROOT_DIR)

    file_context_buffer = []
    sep = "-" * 35
    for p in paths:
        full_path = os.path.join(ROOT_DIR, p)
        file_context_buffer.append(sep)
        file_context_buffer.append(f"FILE: {p}")
        file_context_buffer.append(sep)
        _, ext = os.path.splitext(p)
        lang = ext.lstrip('.').lower()
        file_context_buffer.append(f"```{lang}")
        if os.path.exists(full_path):
            try:
                file_context_buffer.append(safe_read_file(full_path))
            except Exception as e:
                file_context_buffer.append(f"[Error reading file: {e}]")
        else:
            file_context_buffer.append("[File not found]")
        file_context_buffer.append("```\n")

    prompt = build_prompt(
        user_request=user_req,
        file_context="\n".join(file_context_buffer),
        ast_map=ast_map,
        file_cull=True,
        system_prompt="",
        agent_type="default"
    )

    return jsonify({"prompt": prompt})

@app.route('/api/log_copy', methods=['POST'])
def log_copy():
    files = request.json.get("files", [])
    console.print(Panel(
        f"[bold green]Prompt copied to clipboard via Web UI![/bold green]\n"
        f"Contains {len(files)} files.",
        border_style="green"
    ))
    return jsonify({"success": True})

@app.route('/api/preview', methods=['POST'])
def preview():
    payload_str = request.json.get("payload", "")
    data, error_str = intelligent_json_fix(payload_str)
    
    if not data or "files" not in data:
        import json
        try:
            json.loads(payload_str)
            return jsonify({"error": "JSON parsed but 'files' key is missing or schema is incorrect."}), 400
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Invalid JSON syntax: {e.msg} at line {e.lineno} col {e.colno}"}), 400

    results = []
    for file_obj in data["files"]:
        action = file_obj.get("action", "modify").lower()
        if action == "command":
            results.append({
                "path": file_obj.get("command", "Unknown Command"),
                "action": action,
                "diff": "This is a CLI Command. Clicking Apply will execute it in the terminal.",
                "file_obj": file_obj
            })
            continue

        path = file_obj.get("path", "")
        full_path = os.path.join(ROOT_DIR, path)
        old_text = ""
        if os.path.exists(full_path):
            try:
                old_text = safe_read_file(full_path)
            except:
                old_text = "[Error reading existing file]"

        if action == "delete":
            new_text = ""
        else:
            new_text = compute_new_text(file_obj, old_text)

        diff = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=3
        ))
        diff_text = "".join(diff) if diff else "No changes detected."

        results.append({
            "path": path,
            "action": action,
            "diff": diff_text,
            "new_text": new_text,
            "file_obj": file_obj
        })

    return jsonify({"files": results})

@app.route('/api/apply', methods=['POST'])
def apply_file():
    global APPLIED_FILES
    req = request.json or {}
    action = req.get("action", "modify").lower()
    file_obj = req.get("file_obj", {})
    
    if action == "command":
        cmd = file_obj.get("command", "")
        if not cmd:
            return jsonify({"error": "Missing command."}), 400
        try:
            subprocess.run(cmd, shell=True, cwd=ROOT_DIR, check=True)
            file_obj["_status"] = "applied"
            file_obj["action"] = "command"
            APPLIED_FILES = [f for f in APPLIED_FILES if f.get("command") != cmd]
            APPLIED_FILES.append(file_obj)
            console.print(f"  [bold magenta]>[/bold magenta] [magenta]Ran Command[/magenta] [bold]{cmd}[/bold]")
            return jsonify({"success": True})
        except subprocess.CalledProcessError as e:
            return jsonify({"error": f"Command failed with exit code {e.returncode}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    path = req.get("path")
    new_text = req.get("new_text", "")
    full_path = os.path.join(ROOT_DIR, path)

    try:
        old_text = ""
        if os.path.exists(full_path):
            try:
                old_text = safe_read_file(full_path)
            except Exception:
                pass

        if action == "delete":
            if os.path.exists(full_path):
                os.remove(full_path)
            console.print(f"  [bold red]✗[/bold red] [red]Deleted File[/red] [bold]{path}[/bold]")
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_text)
                
        if action == "modify":
            old_lines = old_text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            diff = list(difflib.unified_diff(old_lines, new_lines, n=0))
            added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
            removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
            file_obj["_added"] = added
            file_obj["_removed"] = removed
            diff_str = f" [bold green]+{added}[/bold green] [bold red]-{removed}[/bold red]" if (added > 0 or removed > 0) else ""
            console.print(f"  [bold yellow]✓[/bold yellow] [yellow]Modified File[/yellow] [bold]{path}[/bold]{diff_str}")
        elif action == "create":
            console.print(f"  [bold green]✓[/bold green] [green]Created File[/green] [bold]{path}[/bold]")
            
        file_obj["_status"] = "applied"
        file_obj["action"] = action
        file_obj["path"] = path
        
        APPLIED_FILES = [f for f in APPLIED_FILES if f.get("path") != path]
        APPLIED_FILES.append(file_obj)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/commit', methods=['POST'])
def commit_changes():
    msg = request.json.get("message", "Auto-commit from Web Agent")
    if not msg.strip():
        msg = "Auto-commit from Web Agent"
        
    paths_to_stage = [f.get("path") for f in APPLIED_FILES if f.get("path") and f.get("action", "").lower() != "command"]
    commit_hash = None
    
    try:
        warnings = []
        if paths_to_stage:
            commit_hash, warnings, git_error = commit_git(ROOT_DIR, msg, paths_to_stage)
            for w in warnings:
                console.print(f"[yellow]Git warning: {w}[/yellow]")
            if git_error:
                return jsonify({"error": git_error, "warnings": warnings}), 500
                
        result = {
            "commit_message": msg,
            "files": APPLIED_FILES,
            "commit_hash": commit_hash
        }
        
        print_auto_summary(result)
        
        def shutdown():
            import time
            time.sleep(0.5)
            os._exit(0)
            
        threading.Thread(target=shutdown, daemon=True).start()
        return jsonify({"success": True, "warnings": warnings})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tokens/count', methods=['POST'])
def count_tokens_endpoint():
    req_data = request.json or {}
    text = req_data.get("text", "")
    model = req_data.get("model", "cl100k_base")
    
    if not isinstance(text, str):
        return jsonify({"error": "The 'text' parameter must be a string."}), 400
        
    result = count_tokens_local(text, model)
    return jsonify(result)

def start_server(root_dir, max_depth, extensions, exclude_dirs):
    global ROOT_DIR, MAX_DEPTH, EXTENSIONS, EXCLUDE_DIRS
    ROOT_DIR = root_dir
    MAX_DEPTH = max_depth
    EXTENSIONS = extensions
    EXCLUDE_DIRS = exclude_dirs
    
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    app.run(host='127.0.0.1', port=5000)
