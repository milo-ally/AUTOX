"""Bash tool — execute shell commands."""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any


def run_bash(input_data: dict[str, Any]) -> dict[str, Any]:
    """Execute a shell command and return the output."""
    command = input_data.get("command", "")
    if not command:
        return {"error": "missing required field 'command'"}

    timeout = input_data.get("timeout", 120)
    run_in_background = input_data.get("run_in_background", False)

    cwd = os.getcwd()

    if run_in_background:
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {
                "stdout": "",
                "stderr": "",
                "background_task_id": str(proc.pid),
                "no_output_expected": True,
                "interrupted": False,
            }
        except Exception as e:
            return {"error": str(e), "stdout": "", "stderr": "", "interrupted": False}

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="ignore",
        )

        stdout = result.stdout
        stderr = result.stderr

        # If stdout is very large, persist to a temp file
        persisted_path = None
        persisted_size = None
        max_inline = 100_000
        if len(stdout) > max_inline:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, prefix="microcode_bash_",
                encoding="utf-8"
            ) as f:
                f.write(stdout)
                persisted_path = f.name
                persisted_size = len(stdout)
            stdout = stdout[:max_inline] + f"\n... [output truncated; full output at {persisted_path}]"

        return_code_interpretation = None
        if result.returncode == 0:
            return_code_interpretation = "success"
        elif result.returncode == 1:
            return_code_interpretation = "one_line_output"
        elif result.returncode != 0:
            return_code_interpretation = "nonzero"

        return {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": False,
            "return_code_interpretation": return_code_interpretation,
            "persisted_output_path": persisted_path,
            "persisted_output_size": persisted_size,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": f"command timed out after {timeout}s",
            "stdout": "",
            "stderr": "",
            "interrupted": True,
        }
    except Exception as e:
        return {"error": str(e), "stdout": "", "stderr": "", "interrupted": False}