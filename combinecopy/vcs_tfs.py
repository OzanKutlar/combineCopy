import os
import subprocess
import re


def _run_tf(args: list[str], root_dir: str) -> tuple[int, str, str]:
    """Run a tf.exe command and return (returncode, stdout, stderr)."""
    cmd = ["tf"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=root_dir,
            capture_output=True,
            text=True,
            timeout=120
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        raise RuntimeError(
            "tf.exe not found in PATH. Ensure Visual Studio or TEE is installed and tf.exe is accessible."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tf.exe command timed out: {' '.join(cmd)}")


def tfs_checkout(root_dir: str, paths: list[str]) -> list[str]:
    """Pend edit (checkout) on existing files. Returns list of errors."""
    if not paths:
        return []
    errors = []
    for path in paths:
        abs_path = os.path.join(root_dir, path) if not os.path.isabs(path) else path
        rc, stdout, stderr = _run_tf(["checkout", abs_path], root_dir)
        if rc != 0:
            # Ignore if already checked out
            combined = (stdout + stderr).lower()
            if "already checked out" in combined or "is already pending" in combined:
                continue
            errors.append(f"tf checkout failed for '{path}': {stderr.strip() or stdout.strip()}")
    return errors


def tfs_add(root_dir: str, paths: list[str]) -> list[str]:
    """Pend add for new files. Returns list of errors."""
    if not paths:
        return []
    errors = []
    for path in paths:
        abs_path = os.path.join(root_dir, path) if not os.path.isabs(path) else path
        rc, stdout, stderr = _run_tf(["add", abs_path], root_dir)
        if rc != 0:
            combined = (stdout + stderr).lower()
            if "already pending" in combined:
                continue
            errors.append(f"tf add failed for '{path}': {stderr.strip() or stdout.strip()}")
    return errors


def tfs_delete(root_dir: str, paths: list[str]) -> list[str]:
    """Pend delete for files. Returns list of errors."""
    if not paths:
        return []
    errors = []
    for path in paths:
        abs_path = os.path.join(root_dir, path) if not os.path.isabs(path) else path
        rc, stdout, stderr = _run_tf(["delete", abs_path], root_dir)
        if rc != 0:
            errors.append(f"tf delete failed for '{path}': {stderr.strip() or stdout.strip()}")
    return errors


def tfs_checkin(root_dir: str, paths: list[str], message: str) -> tuple[str | None, str | None]:
    """Check in pending changes. Returns (changeset_number, error_message)."""
    if not paths:
        return None, "No files to check in."

    abs_paths = [
        os.path.join(root_dir, p) if not os.path.isabs(p) else p
        for p in paths
    ]

    # Sanitize message for command line
    safe_message = message.replace('"', "'")

    args = ["checkin", f"/comment:{safe_message}", "/noprompt"] + abs_paths
    rc, stdout, stderr = _run_tf(args, root_dir)

    if rc != 0:
        return None, f"tf checkin failed: {stderr.strip() or stdout.strip()}"

    # Try to extract changeset number from output
    match = re.search(r"Changeset #?(\d+)", stdout, re.IGNORECASE)
    changeset = match.group(1) if match else None
    return changeset, None


def tfs_diff(root_dir: str, path: str = ".") -> tuple[str | None, str | None]:
    """Capture unified diff and pending changes for path and its subfolders.

    Returns (diff_text, error_message).
    """
    try:
        # Check pending status in the directory and subfolders
        rc_stat, out_stat, err_stat = _run_tf(["status", path, "/recursive", "/noprompt"], root_dir)
        if rc_stat != 0:
            return None, err_stat.strip() or out_stat.strip() or "tf status command failed."

        stat_clean = out_stat.strip()
        if not stat_clean or "there are no pending changes" in stat_clean.lower():
            return None, None

        # Query unified diff for edits
        rc_diff, out_diff, err_diff = _run_tf(["diff", path, "/recursive", "/format:Unified", "/noprompt"], root_dir)
        diff_clean = out_diff.strip() if out_diff else ""

        parts = [
            f"--- TFS Pending Changes Status ({path}) ---",
            stat_clean
        ]
        if diff_clean:
            parts.extend([
                "\n--- TFS Unified Diff ---",
                diff_clean
            ])

        return "\n".join(parts), None
    except RuntimeError as e:
        return None, str(e)
