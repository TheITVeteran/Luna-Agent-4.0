"""
Luna Security — local safety and malware check for Luna-created code.

Scans code in Luna's creations and absorbed tools for:
- Network calls to non-local hosts (data leaving your machine)
- Dangerous eval/exec/base64-decode+exec (remote code execution)
- Subprocess with shell=True and user input
- Writes to sensitive system paths
- Common malware/miner patterns

Everything is designed to stay LOCAL. If something looks like it could
send data out or run untrusted code, we flag it so you can review.
"""
import os
import re
from typing import Optional

# Severity: high = likely bad, medium = suspicious, low = review
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# Patterns that are allowed (local only)
LOCAL_HOST_PATTERN = re.compile(
    r"(127\.0\.0\.1|localhost|::1|0\.0\.0\.0)\b",
    re.I
)

# Suspicious patterns: (regex, severity, rule_id, description)
RULES = [
    # Network to non-local URL
    (re.compile(r"urlopen\s*\(\s*[^)]*http[s]?://(?!127\.0\.0\.1|localhost)[^)\s'\"]+", re.I), SEVERITY_HIGH, "network_url", "Network request to non-local URL (data could leave your machine)"),
    (re.compile(r"requests?\.(get|post|put|patch|delete)\s*\(\s*[^)]*['\"]https?://(?!127\.0\.0\.1|localhost)[^'\"]+", re.I), SEVERITY_HIGH, "network_requests", "HTTP request to non-local URL"),
    (re.compile(r"socket\.(create_connection|connect)\s*\([^)]+\)", re.I), SEVERITY_MEDIUM, "socket_connect", "Socket connection (verify it is only to localhost)"),
    (re.compile(r"urllib\.request\.Request\s*\(\s*['\"]https?://(?!127\.0\.0\.1|localhost)", re.I), SEVERITY_HIGH, "network_request_obj", "Request to non-local URL"),
    # eval/exec with variable (could run arbitrary code)
    (re.compile(r"\beval\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)"), SEVERITY_HIGH, "eval_var", "eval() on a variable (could run arbitrary code)"),
    (re.compile(r"\bexec\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)"), SEVERITY_HIGH, "exec_var", "exec() on a variable (could run arbitrary code)"),
    (re.compile(r"compile\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*,"), SEVERITY_HIGH, "compile_var", "compile() on variable (could run arbitrary code)"),
    # base64 decode then exec/eval
    (re.compile(r"base64\.b64decode\s*\([^)]+\)\s*\)?\s*\)?\s*[.,\s]*\s*(?:exec|eval)\s*\("), SEVERITY_HIGH, "b64_exec", "Base64 decode followed by exec/eval (common in malware)"),
    # subprocess with shell=True (command injection risk)
    (re.compile(r"subprocess\.(run|call|Popen)\s*\([^)]*shell\s*=\s*True"), SEVERITY_MEDIUM, "shell_true", "subprocess with shell=True (command injection risk if input is used)"),
    (re.compile(r"os\.system\s*\([^)]+\)"), SEVERITY_MEDIUM, "os_system", "os.system() (verify arguments are not user-controlled)"),
    # __import__ with variable
    (re.compile(r"__import__\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)"), SEVERITY_MEDIUM, "import_var", "__import__ with variable (could load arbitrary module)"),
    # Write to system/sensitive paths
    (re.compile(r"open\s*\(\s*['\"](/etc/|/boot/|C:\\\\Windows|\\\\windows\\\\)"), SEVERITY_HIGH, "write_system_path", "File open to system path"),
    (re.compile(r"open\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*,\s*['\"]w"), SEVERITY_LOW, "write_var_path", "File write with variable path (verify path is safe)"),
    # Crypto miner / coin pattern
    (re.compile(r"(stratum|mining|miner|hashrate|getwork|submitwork)"), SEVERITY_HIGH, "miner_like", "Possible crypto miner keyword"),
    # Credential / key exfiltration
    (re.compile(r"(api[_-]?key|apikey|secret|password|token|credential)\s*[=:]\s*[a-zA-Z_][a-zA-Z0-9_]*\s*[,\s]*(?:urlopen|requests?\.|socket)"), re.I | re.DOTALL, "creds_send", "Possible credential sent over network"),
    # Obfuscation
    (re.compile(r"\\x[0-9a-f]{2}\s*\\x[0-9a-f]{2}\s*\\x[0-9a-f]{2}"), SEVERITY_LOW, "hex_string", "Hex-encoded string (verify it is benign)"),
]

# Allow list: if the line or context matches, skip the rule (e.g. comment or localhost)
ALLOWED_PATTERNS = [
    re.compile(r"#.*$"),  # full line is comment
    re.compile(r"127\.0\.0\.1|localhost"),
    re.compile(r"urlopen.*127\.0\.0\.1|urlopen.*localhost"),
]


def _line_matches_allow_list(line: str) -> bool:
    stripped = line.strip()
    for pat in ALLOWED_PATTERNS:
        if pat.search(stripped):
            return True
    return False


def scan_code(code: str, path_hint: str = "") -> list[dict]:
    """
    Scan source code for suspicious patterns. Returns list of findings.
    Each finding: {severity, rule_id, message, line_no, snippet}
    """
    findings = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        if _line_matches_allow_list(line):
            continue
        for pattern, severity, rule_id, message in RULES:
            m = pattern.search(line)
            if m:
                snippet = line.strip()[:120]
                if len(line.strip()) > 120:
                    snippet += "..."
                findings.append({
                    "severity": severity,
                    "rule_id": rule_id,
                    "message": message,
                    "line_no": i,
                    "snippet": snippet,
                    "path": path_hint,
                })
    return findings


def scan_file(filepath: str) -> list[dict]:
    """Scan a single file. Returns list of findings."""
    if not os.path.isfile(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception:
        return []
    return scan_code(code, path_hint=os.path.basename(filepath))


def scan_directory(dirpath: str, extensions: tuple = (".py",)) -> list[dict]:
    """Scan all files in directory (non-recursive by default for creations). Returns flat list of findings."""
    all_findings = []
    if not os.path.isdir(dirpath):
        return all_findings
    try:
        for name in os.listdir(dirpath):
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            if os.path.isfile(path) and any(name.endswith(ext) for ext in extensions):
                all_findings.extend(scan_file(path))
            elif os.path.isdir(path) and not name.startswith("."):
                # one level down (e.g. Luna's creations/agents)
                for subname in os.listdir(path):
                    if subname.startswith("."):
                        continue
                    subpath = os.path.join(path, subname)
                    if os.path.isfile(subpath) and subname.endswith(".py"):
                        all_findings.extend(scan_file(subpath))
    except Exception:
        pass
    return all_findings


def run_full_scan(creations_dir: str, absorbed_dir: str) -> dict:
    """
    Run security scan on all Luna-created and absorbed code.
    Returns: {
        ok: bool (True if no high/medium findings),
        findings: list of findings,
        scanned: list of paths scanned,
        last_scan_ts: float,
    }
    """
    import time
    findings = []
    scanned = []
    # Scan Luna's creations (including agents subdir)
    if os.path.isdir(creations_dir):
        for name in os.listdir(creations_dir):
            if name.startswith(".") or name.endswith(".txt"):
                continue
            path = os.path.join(creations_dir, name)
            if os.path.isfile(path) and name.endswith(".py"):
                scanned.append(path)
                findings.extend(scan_file(path))
            elif os.path.isdir(path):
                for sub in os.listdir(path):
                    if sub.endswith(".py"):
                        subpath = os.path.join(path, sub)
                        scanned.append(subpath)
                        findings.extend(scan_file(subpath))
    if os.path.isdir(absorbed_dir):
        for name in os.listdir(absorbed_dir):
            if name.startswith(".") or not name.endswith(".py"):
                continue
            path = os.path.join(absorbed_dir, name)
            scanned.append(path)
            findings.extend(scan_file(path))
    high_or_medium = [f for f in findings if f["severity"] in (SEVERITY_HIGH, SEVERITY_MEDIUM)]
    return {
        "ok": len(high_or_medium) == 0,
        "findings": findings,
        "scanned": scanned,
        "last_scan_ts": time.time(),
        "count_high": len([f for f in findings if f["severity"] == SEVERITY_HIGH]),
        "count_medium": len([f for f in findings if f["severity"] == SEVERITY_MEDIUM]),
        "count_low": len([f for f in findings if f["severity"] == SEVERITY_LOW]),
    }
