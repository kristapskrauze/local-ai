#!/usr/bin/env python3
"""Pre-commit triage: find secrets, personal info, and filler comments.

Run from the project you intend to migrate:

    python triage_scan.py C:\\Users\\you\\your_app

Writes triage_report.md next to wherever you run it. Secret values are
redacted in the report, so the report itself is safe to read and paste.
Nothing is modified or deleted -- this only reports.
"""

import argparse
import os
import re
import sys
from collections import defaultdict

SKIP_DIRS = {
    ".git", ".hg", ".svn", "venv", ".venv", "env", ".env.d", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".idea", ".vscode", "site-packages", ".next", "target",
    "bin", "obj", ".gradle", "coverage", ".cache", "eggs", ".eggs",
}

SKIP_EXT = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".jar", ".whl",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".ogg", ".webm",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3", ".pkl", ".pickle", ".npy", ".npz",
    ".pt", ".pth", ".onnx", ".h5", ".safetensors", ".gguf", ".bin",
}

# secrets: high confidence, provider-specific shapes
SECRET_PATTERNS = [
    ("AWS access key id",        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("sk- API key",              re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("GitHub token",             re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT",  re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Google API key",           re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token",              re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("Slack webhook",            re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("Discord webhook",          re.compile(r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[\w\-]+")),
    ("Stripe live key",          re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("SendGrid API key",         re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b")),
    ("Telegram bot token",       re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b")),
    ("Twilio account sid",       re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("Hugging Face token",       re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("Private key block",        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("JWT",                      re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("URL with credentials",     re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@[^\s/]+")),
]

# assignment of a secret-ish name to a literal value
ASSIGN_RE = re.compile(
    r"""(?ix)
    \b(pass|passwd|password|pwd|secret|token|api[_\-]?key|apikey|
       access[_\-]?key|secret[_\-]?key|private[_\-]?key|client[_\-]?secret|
       auth[_\-]?token|bearer|credential|conn(?:ection)?[_\-]?string|dsn)
    \b\s*[:=]\s*
    (?P<q>["'])(?P<val>[^"'\n]{4,})(?P=q)
    """
)

# values that are obviously not real secrets
PLACEHOLDER_RE = re.compile(
    r"(?i)^(|none|null|true|false|changeme|change_me|your[_\-]?|xxx+|\.\.\.|"
    r"<.*>|\{\{.*\}\}|\$\{.*\}|%s|%\(.*\)s|example|test|dummy|placeholder|"
    r"fake|sample|todo|redacted|insert|replace|secret|password|yourpassword|unset|"
    r"os\.environ.*|getenv.*)$"
)
FORMAT_RE = re.compile(r"%[-#0 +]*\d*(?:\.\d+)?[sdifr]|\{[^{}]*\}")

ENV_FILE_RE = re.compile(
    r"(?i)^(\.env(\..*)?|.*\.env|env\.(local|dev|prod|production|staging)|"
    r"secrets?\.(ya?ml|json|toml|ini|cfg|txt)|credentials?(\.\w+)?|"
    r"\.netrc|\.pgpass|id_rsa|id_ed25519|.*\.pem|.*\.pfx|.*\.p12|.*\.key|"
    r"service[_\-]?account.*\.json)$"
)

# personal info
PERSONAL_PATTERNS = [
    ("Windows user path", re.compile(r"(?i)[A-Z]:[\\/]Users[\\/]([A-Za-z0-9._\-]+)")),
    ("Unix home path",    re.compile(r"/(?:home|Users)/([A-Za-z0-9._\-]+)")),
    ("Email address",     re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("Private IP",        re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("MAC address",       re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
]

GENERIC_EMAIL_RE = re.compile(
    r"(?i)@(example\.(com|org|net)|test\.com|localhost|email\.com|domain\.com)$"
)

# filler comments
COMMENT_MARKERS = ("#", "//", "/*", "*", "<!--", "--", ";", "'''", '"""')

SLOP_PATTERNS = [
    ("Tool attribution",   re.compile(r"(?i)\b(generated\s+(by|with|using)\s+\w+|auto[\-\s]?generated|co-authored-by)\b")),
    ("Tutorial voice",     re.compile(r"(?i)\b(let'?s\s+(start|begin|create|define|now|go|make|add)|now\s+(we|let'?s|you)\b|here\s+we\s+(are|go|define|create)|as\s+you\s+can\s+see|in\s+this\s+(example|tutorial|snippet|code)|note\s+that\b|feel\s+free\s+to|hope\s+this\s+helps|happy\s+coding)")),
    ("Unfinished stub",    re.compile(r"(?i)\b(in\s+a\s+real\s+(world|app|application|system|scenario)|in\s+production(\s+you(\s+would|'d)?)?|for\s+(demo|demonstration|illustration|simplicity|brevity)\s+purposes|placeholder\s+(for|value|implementation)|implement\s+this|your\s+(logic|code|implementation)\s+here|dummy\s+(data|implementation)|mock(ed)?\s+for\s+now|simplified\s+(version|for))")),
    ("Marketing adjective",re.compile(r"(?i)\b(comprehensive|robust|enhanced|cutting[\-\s]edge|state[\-\s]of[\-\s]the[\-\s]art|production[\-\s]ready|enterprise[\-\s]grade|blazing(ly)?\s+fast|seamless(ly)?|powerful|elegant(ly)?|beautiful(ly)?|leverag(e|ing)\s+the\s+power)\b")),
    ("Emoji decoration",   re.compile(r"[\U0001F300-\U0001FAFF\u2705\u274C\u26A0\u2728\u2B50\U0001F680\U0001F389]")),
    ("Banner divider",     re.compile(r"(?:[=\-*#_~]){12,}")),
]

# comments that only restate the code on the next line
RESTATE_RE = re.compile(
    r"""(?ix)^\s*(?:\#|//)\s*
    (import\s+(the\s+)?(necessary|required)\s+(libraries|modules|packages)|
     imports?$|
     (initialize|instantiate|create|define|set\s+up|setup)\s+(the\s+)?\w+\s*(object|instance|variable|class|function|list|dict|dictionary)?\s*$|
     (return|returns)\s+(the\s+)?(result|value|output|data|response)\s*$|
     main\s+(function|entry\s*point)\s*$|
     (start|end)\s+of\s+\w+\s*$|
     constructor\s*$|
     getter\s*$|setter\s*$|
     (loop|iterate)\s+(over|through)\s+(the\s+)?\w+\s*$|
     (print|log|display|show)\s+(the\s+)?\w+\s*$|
     (close|open)\s+(the\s+)?(file|connection|socket)\s*$|
     (try|catch|handle)\s+(the\s+)?(error|exception)s?\s*$|
     (check|validate)\s+(if|the)\s+\w+\s*$)
    """
)


def is_probably_text(path, probe=4096):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(probe)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    return True


def read_lines(path):
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
    return None


def redact(value):
    value = value.strip()
    if len(value) <= 8:
        return value[0] + "*" * (len(value) - 1) if value else ""
    return "%s...%s (%d chars)" % (value[:4], value[-2:], len(value))


def comment_text(line):
    """Return the comment portion of a line, or None."""
    stripped = line.strip()
    for marker in ("#", "//", "<!--", ";"):
        if stripped.startswith(marker):
            return stripped
    if stripped.startswith(('"""', "'''", "*", "/*")):
        return stripped
    # trailing comment
    for marker in (" #", " //"):
        idx = line.find(marker)
        if idx > 0 and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            return line[idx:].strip()
    return None


def scan(root):
    findings = defaultdict(list)
    stats = {"files": 0, "lines": 0, "skipped": 0}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]

        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ext = os.path.splitext(name)[1].lower()

            if ENV_FILE_RE.match(name):
                findings["env_files"].append((rel, 0, "Credential-bearing file", name))
                continue

            if ext in SKIP_EXT or not is_probably_text(full):
                stats["skipped"] += 1
                continue

            lines = read_lines(full)
            if lines is None:
                stats["skipped"] += 1
                continue

            stats["files"] += 1
            stats["lines"] += len(lines)

            for n, line in enumerate(lines, 1):
                if len(line) > 2000:
                    line = line[:2000]

                for label, rx in SECRET_PATTERNS:
                    m = rx.search(line)
                    if m:
                        findings["secrets"].append((rel, n, label, redact(m.group(0))))

                m = ASSIGN_RE.search(line)
                if m:
                    val = m.group("val")
                    # prose and format strings are not credentials
                    if (not PLACEHOLDER_RE.match(val.strip()) and not re.search(r"\s", val)
                            and not FORMAT_RE.search(val)):
                        findings["secrets"].append(
                            (rel, n, "Hardcoded %s" % m.group(1).lower(), redact(val))
                        )

                for label, rx in PERSONAL_PATTERNS:
                    m = rx.search(line)
                    if m:
                        hit = m.group(0)
                        if label == "Email address" and GENERIC_EMAIL_RE.search(hit):
                            continue
                        findings["personal"].append((rel, n, label, hit))

                c = comment_text(line)
                if c:
                    for label, rx in SLOP_PATTERNS:
                        if rx.search(c):
                            findings["slop"].append((rel, n, label, c[:120]))
                            break
                    else:
                        if RESTATE_RE.match(c):
                            findings["slop"].append((rel, n, "Restates the code", c[:120]))

    return findings, stats


def section(out, title, rows, note=None):
    out.append("## %s (%d)\n" % (title, len(rows)))
    if note:
        out.append("%s\n" % note)
    if not rows:
        out.append("None found.\n")
        return
    by_file = defaultdict(list)
    for rel, n, label, detail in rows:
        by_file[rel].append((n, label, detail))
    for rel in sorted(by_file):
        out.append("**`%s`**\n" % rel)
        for n, label, detail in sorted(by_file[rel]):
            loc = ("L%d" % n) if n else "file"
            out.append("- %s - %s: `%s`" % (loc, label, detail.replace("`", "'")))
        out.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="project directory to scan")
    ap.add_argument("-o", "--out", default="triage_report.md")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit("not a directory: %s" % root)

    findings, stats = scan(root)

    out = []
    out.append("# Pre-commit triage: %s\n" % root)
    out.append("Scanned %d text files, %d lines (%d binary/skipped).\n"
               % (stats["files"], stats["lines"], stats["skipped"]))

    section(out, "Credential files -- DO NOT COMMIT", findings["env_files"],
            "Add these to .gitignore. Commit a `.env.example` with empty values instead.")
    section(out, "Secrets in source -- must be removed", findings["secrets"],
            "Values are redacted here. Replace each with `os.environ[...]` before committing.")
    section(out, "Personal information", findings["personal"],
            "Usernames, home paths, emails, private IPs, MACs. Replace with config or placeholders.")
    section(out, "Filler comments", findings["slop"],
            "Review each. 'Restates the code' and 'Tutorial voice' are usually safe to delete outright.")

    blockers = len(findings["env_files"]) + len(findings["secrets"])
    out.append("## Verdict\n")
    if blockers:
        out.append("**%d blocking item(s).** Resolve before the first commit.\n" % blockers)
    else:
        out.append("No hardcoded secrets or credential files detected.\n")

    report = "\n".join(out)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(report)
    print("\n[written to %s]" % os.path.abspath(args.out))
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
