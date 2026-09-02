import os
import re
import difflib
import subprocess
from combinecopy.utils import safe_read_file, detect_newline
from combinecopy.vcs_tfs import tfs_checkout, tfs_add, tfs_delete, tfs_checkin


def write_text_preserving(path: str, text: str, original_newline: str | None = None) -> None:
    """Writes text to path preserving line endings and surrogate bytes losslessly."""
    if original_newline is None:
        original_newline = "\n"
    if original_newline and original_newline != "\n":
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        text = normalized.replace("\n", original_newline)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def find_partial_matches(search_text: str, file_text: str) -> list:
    """Finds candidate partial-match blocks for an unmatching search string."""
    search_lines = search_text.splitlines()
    file_lines = file_text.splitlines()
    if len(search_lines) <= 1:
        return []
    search_norm = [line.strip() for line in search_lines]
    file_norm = [line.strip() for line in file_lines]

    matcher = difflib.SequenceMatcher(None, search_norm, file_norm)
    blocks = matcher.get_matching_blocks()
    candidates = []
    for block in blocks:
        if block.size > 0:
            matched_text = "".join(search_norm[block.a : block.a + block.size])
            if not matched_text:
                continue

            start_line = max(1, block.b - block.a + 1)
            end_line = min(len(file_lines), block.b - block.a + len(search_lines))

            candidates.append({
                "start_line": start_line,
                "end_line": end_line,
                "matched_lines": block.size,
                "search_lines": len(search_lines),
                "coverage": block.size / len(search_lines)
            })

    candidates.sort(key=lambda x: (x["matched_lines"], x["coverage"]), reverse=True)
    unique_cands = {}
    for c in candidates:
        key = (c["start_line"], c["end_line"])
        if key not in unique_cands:
            unique_cands[key] = c
        if len(unique_cands) >= 5:
            break

    return list(unique_cands.values())


def normalize_text(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().split('\n') if line.strip())


def validate_file_obj(file_obj: dict, root_dir: str, known_files: list[str] | None = None, web_mode: bool = False, status_callback=None) -> None:
    """Validates a file object from an execution payload, identifying errors, fuzzy matches, or partial matches."""
    action = file_obj.get("action", "modify").upper()
    if action == "COMMAND":
        if "command" not in file_obj:
            file_obj["_errors"] = ["Missing 'command' key for COMMAND action."]
        else:
            file_obj["_errors"] = []
        return

    path = file_obj.get("path", "unknown")
    full_path = os.path.join(root_dir, path)
    errors = []
    if "_revert_error" in file_obj:
        errors.append(file_obj["_revert_error"])
    if not os.path.exists(full_path) and action != "CREATE":
        filename = os.path.basename(path)
        if known_files:
            matches = [f for f in known_files if os.path.basename(f) == filename]
            if len(matches) == 1:
                correct_path_rel = os.path.relpath(matches[0], root_dir)
                warn_msg = f"Path corrected from '{path}' to '{correct_path_rel}'."
                if warn_msg not in file_obj.setdefault("_warnings", []):
                    file_obj["_warnings"].append(warn_msg)
                file_obj["path"] = correct_path_rel
                path = correct_path_rel
                full_path = os.path.join(root_dir, path)
            elif len(matches) > 1:
                if web_mode:
                    file_obj.setdefault("_warnings", []).append(f"Ambiguous file: '{filename}' found in multiple locations.")
                else:
                    errors.append(f"Ambiguous file: '{filename}' found in multiple locations.")
            else:
                if web_mode:
                    file_obj.setdefault("_warnings", []).append(f"Target file '{path}' does not exist locally.")
                else:
                    errors.append(f"Target file '{path}' does not exist and was not found in context.")
        else:
            if web_mode:
                file_obj.setdefault("_warnings", []).append(f"Target file '{path}' does not exist locally.")
            else:
                errors.append(f"Target file '{path}' does not exist.")

    if action == "MODIFY" and not errors:
        if "regex_replace" in file_obj and os.path.exists(full_path):
            if status_callback:
                status_callback(f"Evaluating regex replacements for {path}...")
            old_text = safe_read_file(full_path)
            for b_idx, block in enumerate(file_obj.get("regex_replace", [])):
                pattern = block.get("pattern", "")
                if pattern:
                    try:
                        compiled = re.compile(pattern)
                        if not compiled.search(old_text):
                            warn_msg = f"Regex pattern '{pattern}' found no matches."
                            if warn_msg not in file_obj.setdefault("_warnings", []):
                                file_obj["_warnings"].append(warn_msg)
                    except re.error as e:
                        errors.append(f"Invalid regex pattern '{pattern}': {e}")

        if "search_replace" in file_obj and os.path.exists(full_path):
            try:
                if status_callback:
                    status_callback(f"Reading {path}...")
                old_text = safe_read_file(full_path)
                for b_idx, block in enumerate(file_obj.get("search_replace", [])):
                    if status_callback:
                        status_callback(f"Checking match {b_idx + 1}/{len(file_obj.get('search_replace', []))} in {path}...")
                    block.pop("_candidates", None)
                    if "replace" not in block:
                        errors.append(f"No replacement found for search block {b_idx + 1}.")
                    search_text = block.get("search", "")
                    if search_text and search_text not in old_text:
                        if status_callback:
                            status_callback(f"Searching fuzzy match {b_idx + 1} in {path}...")
                        normalized_old = normalize_text(old_text)
                        normalized_search = normalize_text(search_text)
                        if normalized_search in normalized_old:
                            source_lines = old_text.split('\n')
                            found_exact = False
                            for i in range(len(source_lines)):
                                for j in range(i, len(source_lines)):
                                    window = '\n'.join(source_lines[i : j + 1])
                                    nw = normalize_text(window)
                                    if nw == normalized_search:
                                        block['search'] = window
                                        warn_msg = f"Used fuzzy matching for search block {b_idx + 1}."
                                        if warn_msg not in file_obj.setdefault("_warnings", []):
                                            file_obj["_warnings"].append(warn_msg)
                                        found_exact = True
                                        break
                                    elif len(nw) > len(normalized_search):
                                        break
                                if found_exact:
                                    break
                            if not found_exact:
                                errors.append(f"Fuzzy match found but couldn't map to original text for block {b_idx+1}.")
                        else:
                            if status_callback:
                                status_callback(f"Searching partial matches {b_idx + 1} in {path} (this can take a while)...")
                            candidates = find_partial_matches(search_text, old_text)
                            if candidates:
                                block["_candidates"] = candidates
                                block["_original_search"] = search_text
                                best_cand = candidates[0]
                                cov_pct = int(best_cand["coverage"] * 100)
                                errors.append(f"Search block {b_idx + 1} not found. Found partial match covering {best_cand['matched_lines']}/{best_cand['search_lines']} lines ({cov_pct}%) near lines {best_cand['start_line']}-{best_cand['end_line']}. Press 'h' to resolve.")
                            else:
                                errors.append(f"Search block {b_idx + 1} not found. Fuzzy match and partial match also failed.")
            except Exception as e:
                errors.append(f"Error reading file: {e}")
    file_obj["_errors"] = errors


def commit_git(root_dir: str, msg: str, paths_to_stage: list[str]) -> tuple[str | None, str | None]:
    """Stages and commits files via Git. Returns (commit_hash, error_msg)."""
    try:
        subprocess.run(["git", "add"] + paths_to_stage, cwd=root_dir, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=root_dir, check=True)
        commit_hash = ""
        try:
            commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root_dir, text=True).strip()
        except Exception:
            pass
        return commit_hash, None
    except subprocess.CalledProcessError as e:
        return None, f"Git error: {e}"


def commit_tfs(root_dir: str, msg: str, applied_files: list[dict], paths_to_stage: list[str]) -> tuple[str | None, list[str], str | None]:
    """Stages and checks in files via TFS. Returns (changeset, warnings, error_msg)."""
    add_paths = [f.get("path") for f in applied_files if f.get("action", "").lower() == "create" and f.get("path")]
    warnings = tfs_add(root_dir, add_paths) if add_paths else []
    changeset, error = tfs_checkin(root_dir, paths_to_stage, msg)
    return changeset, warnings, error
