"""
Input validation and sanitization for inbound email content and attachments.

SOW requirement: "Input validation and attachment sanitization for inbound
email and document processing workflows."

What this module guards against
--------------------------------
1. Oversized payloads that could exhaust memory or trigger quota limits.
2. Disallowed attachment MIME types (executable, script, archive files).
3. Null bytes, control characters, and Unicode bidi-override tricks in text.
4. Basic script-tag injection in HTML email bodies.

Usage
-----
    from src.security.input_validator import validate_email_body, validate_attachment

    issues = validate_email_body(body_text)
    if issues:
        logger.warning("Email body validation issues: %s", issues)

    ok, reason = validate_attachment(filename, mime_type, size_bytes)
    if not ok:
        raise ValueError(f"Attachment rejected: {reason}")
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import List, Optional, Tuple

logger = logging.getLogger("shepherd_ai.security.input_validator")

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_BODY_CHARS       = 500_000   # ~500 KB of text
MAX_ATTACHMENT_BYTES = 26_214_400  # 25 MB per attachment

# Allowed MIME types for attachments processed by the pipeline
ALLOWED_MIME_TYPES: set[str] = {
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-outlook",
    "text/plain",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/tiff",
    "image/webp",
}

# Blocked extension fragments (case-insensitive)
BLOCKED_EXTENSIONS: tuple[str, ...] = (
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js", ".msi",
    ".dll", ".com", ".scr", ".pif", ".jar", ".py", ".rb",
    ".zip", ".tar", ".gz", ".rar", ".7z",       # Archives bypass AV scans
    ".docm", ".xlsm", ".pptm",                  # Macro-enabled Office
    ".html", ".htm", ".svg",                     # Active web content
)

# Bidi override / homoglyph characters that can disguise text
_BIDI_CHARS: frozenset[str] = frozenset([
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",  # LRE, RLE, PDF, LRO, RLO
    "\u2066", "\u2067", "\u2068", "\u2069",             # LRI, RLI, FSI, PDI
    "\u200b", "\u200c", "\u200d",                       # Zero-width spaces/joiners
    "\ufeff",                                            # BOM / ZWNBSP
])

_SCRIPT_RE    = re.compile(r"<\s*script[\s>]", re.IGNORECASE)
_IFRAME_RE    = re.compile(r"<\s*iframe[\s>]",  re.IGNORECASE)
_ON_EVENT_RE  = re.compile(r"\bon\w+\s*=",      re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_email_body(text: str) -> List[str]:
    """Validate and report issues with an email body string.

    Returns a (possibly empty) list of human-readable issue strings.
    The caller decides whether to reject, log, or flag-for-review.
    """
    issues: List[str] = []

    if not isinstance(text, str):
        return ["body is not a string"]

    # 1. Size
    if len(text) > MAX_BODY_CHARS:
        issues.append(
            f"body too large ({len(text):,} chars > {MAX_BODY_CHARS:,} limit)"
        )

    # 2. Null bytes and control characters
    if "\x00" in text:
        issues.append("body contains null bytes")
    ctrl = [c for c in text if unicodedata.category(c) == "Cc" and c not in ("\n", "\r", "\t")]
    if ctrl:
        issues.append(f"body contains {len(ctrl)} control character(s)")

    # 3. Bidi override characters
    bad_bidi = [c for c in text if c in _BIDI_CHARS]
    if bad_bidi:
        issues.append(f"body contains {len(bad_bidi)} bidi-override character(s)")

    # 4. Script injection
    if _SCRIPT_RE.search(text):
        issues.append("body contains <script> tag")
    if _IFRAME_RE.search(text):
        issues.append("body contains <iframe> tag")
    if _ON_EVENT_RE.search(text):
        issues.append("body contains inline event handler (onXxx=)")

    return issues


def sanitize_email_body(text: str) -> str:
    """Return a sanitized copy of *text* safe to pass to AI models.

    - Strips null bytes and dangerous control characters.
    - Removes bidi override characters.
    - Strips <script> and <iframe> tags (keeps their text content).
    - Truncates to MAX_BODY_CHARS.
    """
    if not isinstance(text, str):
        return ""

    # Remove null bytes and control chars (keep newline/tab)
    text = "".join(
        c for c in text
        if c in ("\n", "\r", "\t") or unicodedata.category(c) != "Cc"
    )

    # Remove bidi overrides
    text = "".join(c for c in text if c not in _BIDI_CHARS)

    # Strip dangerous HTML tags
    text = re.sub(r"<\s*script[^>]*>.*?</\s*script\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*iframe[^>]*>.*?</\s*iframe\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)

    # Truncate
    if len(text) > MAX_BODY_CHARS:
        logger.warning("Email body truncated from %d to %d chars", len(text), MAX_BODY_CHARS)
        text = text[:MAX_BODY_CHARS]

    return text


def validate_attachment(
    filename: str,
    mime_type: Optional[str],
    size_bytes: int,
) -> Tuple[bool, str]:
    """Check whether an attachment is safe to process.

    Returns:
        (True, "")            — attachment is acceptable.
        (False, reason_str)   — attachment should be rejected.
    """
    fname_lower = (filename or "").lower()

    # 1. Blocked extension
    for ext in BLOCKED_EXTENSIONS:
        if fname_lower.endswith(ext):
            reason = f"blocked file extension '{ext}' in '{filename}'"
            logger.warning("Attachment rejected: %s", reason)
            return False, reason

    # 2. MIME type allowlist (only if mime_type is provided)
    if mime_type:
        mt = mime_type.split(";")[0].strip().lower()  # strip charset= etc.
        if mt and mt not in ALLOWED_MIME_TYPES:
            reason = f"disallowed MIME type '{mt}' for '{filename}'"
            logger.warning("Attachment rejected: %s", reason)
            return False, reason

    # 3. Size
    if size_bytes > MAX_ATTACHMENT_BYTES:
        reason = (
            f"attachment '{filename}' too large "
            f"({size_bytes:,} bytes > {MAX_ATTACHMENT_BYTES:,} limit)"
        )
        logger.warning("Attachment rejected: %s", reason)
        return False, reason

    return True, ""


def validate_sender_email(email: str) -> bool:
    """Return True if the sender email passes basic format validation."""
    if not email or not isinstance(email, str):
        return False
    # Simple RFC-compliant check — email-validator library does deep validation
    parts = email.strip().split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if not local or not domain or "." not in domain:
        return False
    return True
