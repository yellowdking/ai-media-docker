"""Pure-stdlib HTML sanitization (allowlist) and UTF-8 character truncation for WeChat drafts."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Official WeChat draft field character limits
MAX_TITLE_CHARS = 64
MAX_AUTHOR_CHARS = 8
MAX_DIGEST_CHARS = 120

_ALLOWED_TAGS = frozenset(
    {
        "section",
        "div",
        "p",
        "br",
        "hr",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "sub",
        "sup",
        "mark",
        "small",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "code",
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "figure",
        "figcaption",
        "dl",
        "dt",
        "dd",
    }
)

_VOID_TAGS = frozenset({"br", "hr", "img"})

# Tags whose inner content must be discarded completely
_DROP_CONTENT_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "noscript",
        "template",
        "svg",
        "math",
        "textarea",
        "title",
        "head",
    }
)

_GLOBAL_ALLOWED_ATTRS = frozenset({"style", "title", "align", "dir", "lang", "class"})

_TAG_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "target", "rel"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "width", "height", "valign"}),
    "th": frozenset({"colspan", "rowspan", "width", "height", "valign"}),
    "table": frozenset({"width", "border", "cellpadding", "cellspacing"}),
    "ol": frozenset({"start", "type"}),
}

_CTRL_AND_WS_RE = re.compile(r"[\x00-\x20\x7f]+")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_UNSAFE_CSS_RE = re.compile(
    r"(?:expression\s*\(|javascript\s*:|vbscript\s*:|url\s*\(|behavior\s*:|-moz-binding|@import)",
    re.IGNORECASE,
)


def truncate_utf8_chars(text: str, max_chars: int) -> str:
    """Safely normalize whitespace and truncate to `max_chars` Unicode characters (UTF-8 clean)."""
    cleaned = re.sub(r"[\r\n\t]+", " ", (text or "")).strip()
    if max_chars <= 0:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars == 1:
        return cleaned[:1]
    return cleaned[: max_chars - 1].rstrip() + "…"


def _is_safe_url(value: str, *, allow_hash: bool = False, allow_mailto: bool = False) -> bool:
    if not value:
        return False
    # Decode nested entities (e.g. &#106;avascript:, &#x6a;avascript:) and strip control/whitespace
    decoded = html.unescape(html.unescape(value)).strip()
    compact = _CTRL_AND_WS_RE.sub("", decoded).lower()
    if not compact:
        return False
    if allow_hash and compact.startswith("#"):
        return True
    if compact.startswith("http://") or compact.startswith("https://"):
        return True
    if allow_mailto and compact.startswith("mailto:"):
        return True
    # Allow root-relative paths like /api/articles/... (disallow protocol-relative //)
    if compact.startswith("/") and not compact.startswith("//"):
        return True
    return False


def _sanitize_style(style_val: str) -> str:
    if not style_val:
        return ""
    decoded = html.unescape(style_val)
    cleaned = _CSS_COMMENT_RE.sub("", decoded)
    safe_decls: list[str] = []
    for part in cleaned.split(";"):
        decl = part.strip()
        if not decl or ":" not in decl:
            continue
        if _UNSAFE_CSS_RE.search(decl):
            continue
        safe_decls.append(decl)
    return ";".join(safe_decls)


class _WeChatHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_stack: list[str] = []
        self._open_tags: list[str] = []

    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        allowed = _GLOBAL_ALLOWED_ATTRS | _TAG_ALLOWED_ATTRS.get(tag, frozenset())
        cleaned: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw_name, raw_val in attrs:
            name = (raw_name or "").strip().lower()
            if not name or name.startswith("on") or name not in allowed or name in seen:
                continue
            val = "" if raw_val is None else str(raw_val)
            if name == "href":
                if not _is_safe_url(val, allow_hash=True, allow_mailto=True):
                    continue
            elif name == "src":
                if not _is_safe_url(val, allow_hash=False, allow_mailto=False):
                    continue
            elif name == "style":
                val = _sanitize_style(val)
                if not val:
                    continue
            elif name == "target":
                if val.strip().lower() not in ("_blank", "_self"):
                    continue
            seen.add(name)
            cleaned.append((name, val))
        return cleaned

    def _format_start_tag(self, tag: str, attrs: list[tuple[str, str]]) -> str:
        if not attrs:
            return f"<{tag}>"
        attr_str = " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs)
        return f"<{tag} {attr_str}>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = (tag or "").strip().lower()
        if t in _DROP_CONTENT_TAGS:
            self._skip_stack.append(t)
            return
        if self._skip_stack:
            return
        if t not in _ALLOWED_TAGS:
            return
        safe_attrs = self._clean_attrs(t, attrs)
        self._out.append(self._format_start_tag(t, safe_attrs))
        if t not in _VOID_TAGS:
            self._open_tags.append(t)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = (tag or "").strip().lower()
        if t in _DROP_CONTENT_TAGS or self._skip_stack:
            return
        if t not in _ALLOWED_TAGS:
            return
        safe_attrs = self._clean_attrs(t, attrs)
        if t in _VOID_TAGS:
            self._out.append(self._format_start_tag(t, safe_attrs))
        else:
            self._out.append(self._format_start_tag(t, safe_attrs) + f"</{t}>")

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").strip().lower()
        if self._skip_stack:
            if t == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if t not in _ALLOWED_TAGS or t in _VOID_TAGS:
            return
        if t in self._open_tags:
            while self._open_tags:
                top = self._open_tags.pop()
                self._out.append(f"</{top}>")
                if top == t:
                    break

    def handle_data(self, data: str) -> None:
        if self._skip_stack or not data:
            return
        self._out.append(html.escape(data, quote=False))

    def get_html(self) -> str:
        while self._open_tags:
            self._out.append(f"</{self._open_tags.pop()}>")
        return "".join(self._out).strip()


def sanitize_wechat_html(html_content: str) -> str:
    """Allowlist-sanitize HTML for WeChat draft upload and admin preview."""
    if not html_content:
        return ""
    sanitizer = _WeChatHTMLSanitizer()
    sanitizer.feed(html_content)
    sanitizer.close()
    return sanitizer.get_html()
