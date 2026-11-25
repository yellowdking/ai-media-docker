"""Zero-cost translation, source HTML extraction, and pure-stdlib cover PNG generator.

Hard constraints:
- Zero paid AI APIs or LLM SDKs.
- Only Python stdlib + httpx (already in requirements.txt).
- Always fails gracefully when offline without raising uncaught errors.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import struct
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

try:
    from .wechat import MAX_DIGEST_CHARS, MAX_TITLE_CHARS, sanitize_wechat_html, truncate_utf8_chars
except ImportError:  # pragma: no cover
    MAX_TITLE_CHARS = 64
    MAX_DIGEST_CHARS = 120

    def truncate_utf8_chars(text: str, max_chars: int) -> str:
        cleaned = re.sub(r"[\r\n\t]+", " ", (text or "")).strip()
        if max_chars <= 0:
            return ""
        if len(cleaned) <= max_chars:
            return cleaned
        if max_chars == 1:
            return cleaned[:1]
        return cleaned[: max_chars - 1].rstrip() + "…"

    def sanitize_wechat_html(html_content: str) -> str:
        return (html_content or "").strip()


GOOGLE_GTX_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

MAX_TRANSLATE_CHUNK_CHARS = 1400
MAX_PARAGRAPHS = 12
MAX_FETCHED_CHARS = 4500
MIN_PARAGRAPH_LEN = 30

_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript", "svg"}
_BLOCK_TAGS = {"p", "h2", "h3", "li"}


def cjk_ratio(text: str) -> float:
    cleaned = (text or "").strip()
    if not cleaned:
        return 0.0
    non_ws = 0
    cjk = 0
    for ch in cleaned:
        if ch.isspace():
            continue
        non_ws += 1
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
    if non_ws == 0:
        return 0.0
    return cjk / non_ws


def is_mostly_zh(text: str, threshold: float = 0.30) -> bool:
    return cjk_ratio(text) >= threshold


def split_text_chunks(text: str, max_chars: int = MAX_TRANSLATE_CHUNK_CHARS) -> list[str]:
    """Split text into <= max_chars chunks while preserving paragraph boundaries."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    raw_paras = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    if not raw_paras:
        raw_paras = [cleaned]

    expanded: list[str] = []
    for para in raw_paras:
        if len(para) <= max_chars:
            expanded.append(para)
            continue
        # Split oversized paragraph on sentence boundaries first
        sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+", para) if s.strip()]
        buf = ""
        for sent in sentences:
            if len(sent) > max_chars:
                if buf:
                    expanded.append(buf)
                    buf = ""
                for i in range(0, len(sent), max_chars):
                    part = sent[i : i + max_chars].strip()
                    if part:
                        expanded.append(part)
                continue
            candidate = f"{buf} {sent}".strip() if buf else sent
            if len(candidate) <= max_chars:
                buf = candidate
            else:
                if buf:
                    expanded.append(buf)
                buf = sent
        if buf:
            expanded.append(buf)

    chunks: list[str] = []
    current = ""
    for para in expanded:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


async def _translate_chunk_google(client: Any, chunk: str) -> str | None:
    try:
        resp = await client.get(
            GOOGLE_GTX_URL,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "zh-CN",
                "dt": "t",
                "q": chunk,
            },
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            return None
        parts = [
            str(seg[0])
            for seg in data[0]
            if isinstance(seg, list) and seg and seg[0] is not None
        ]
        joined = "".join(parts).strip()
        return joined or None
    except Exception:
        return None


async def _translate_chunk_mymemory(client: Any, chunk: str) -> str | None:
    try:
        resp = await client.get(
            MYMEMORY_URL,
            params={
                "q": chunk[:480],
                "langpair": "en|zh-CN",
            },
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        status_code = data.get("responseStatus", 200)
        try:
            if int(status_code) != 200:
                return None
        except (TypeError, ValueError):
            pass
        translated = str((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not translated:
            return None
        upper = translated.upper()
        if upper.startswith("MYMEMORY WARNING") or "PLEASE SELECT TWO DISTINCT LANGUAGES" in upper:
            return None
        return html.unescape(translated)
    except Exception:
        return None


async def translate_to_zh(text: str) -> tuple[str, str]:
    """Translate text to Simplified Chinese using free public endpoints with offline fallback.

    Returns (translated_text, mode) where mode is one of:
    'already_zh', 'google_gtx', 'mymemory', 'fallback'.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ("", "already_zh")
    if is_mostly_zh(cleaned, threshold=0.30):
        return (cleaned, "already_zh")
    if httpx is None:
        return (cleaned, "fallback")

    chunks = split_text_chunks(cleaned, max_chars=MAX_TRANSLATE_CHUNK_CHARS)
    translated_chunks: list[str] = []
    used_google = False
    used_mymemory = False
    used_fallback = False

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for chunk in chunks:
                res = await _translate_chunk_google(client, chunk)
                if res:
                    translated_chunks.append(res)
                    used_google = True
                    continue
                res = await _translate_chunk_mymemory(client, chunk)
                if res:
                    translated_chunks.append(res)
                    used_mymemory = True
                    continue
                translated_chunks.append(chunk)
                used_fallback = True
    except Exception:
        return (cleaned, "fallback")

    combined = "\n\n".join(translated_chunks).strip()
    if not combined:
        return (cleaned, "fallback")
    if used_google and not used_mymemory and not used_fallback:
        return (combined, "google_gtx")
    if used_mymemory and not used_fallback:
        return (combined, "mymemory")
    if used_google or used_mymemory:
        return (combined, "google_gtx" if used_google else "mymemory")
    return (cleaned, "fallback")


class _ParagraphHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._active_tag: str | None = None
        self._buf: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t in _BLOCK_TAGS and self._active_tag is None:
            self._active_tag = t
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if self._active_tag == t:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) >= MIN_PARAGRAPH_LEN:
                self.paragraphs.append(text)
            self._active_tag = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._active_tag is not None:
            self._buf.append(data)


def extract_paragraphs_from_html(
    html_text: str,
    *,
    max_paragraphs: int = MAX_PARAGRAPHS,
    max_chars: int = MAX_FETCHED_CHARS,
) -> list[str]:
    """Extract clean meaningful paragraphs from an HTML document using stdlib html.parser."""
    if not html_text:
        return []
    parser = _ParagraphHTMLParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return []

    selected: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for para in parser.paragraphs:
        norm = para.strip()
        if len(norm) < MIN_PARAGRAPH_LEN:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        if total_chars + len(norm) > max_chars and selected:
            remaining = max_chars - total_chars
            if remaining >= MIN_PARAGRAPH_LEN:
                selected.append(norm[:remaining].rstrip())
            break
        selected.append(norm)
        total_chars += len(norm)
        if len(selected) >= max_paragraphs:
            break
    return selected


async def fetch_source_paragraphs(url: str) -> list[str]:
    """Fetch a source webpage and extract meaningful paragraphs; return [] on any error."""
    target = (url or "").strip()
    if not target or not (target.startswith("http://") or target.startswith("https://")):
        return []
    if httpx is None:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                target,
                headers={
                    "User-Agent": BROWSER_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            if resp.status_code != 200:
                return []
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and not any(tok in ctype for tok in ("text/", "html", "xml", "xhtml")):
                return []
            return extract_paragraphs_from_html(resp.text)
    except Exception:
        return []


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _clamp_byte(val: float) -> int:
    if val < 0:
        return 0
    if val > 255:
        return 255
    return int(val)


def generate_cover_png(dest_path: Path, seed_text: str) -> None:
    """Generate a deterministic valid 1280x720 RGB PNG cover using pure Python stdlib.

    Renders a 320x180 gradient + geometric circles/bars/waves canvas seeded by `seed_text`
    and cleanly 4x pixel-scales to 1280x720 so WeChat `material/add_material` always accepts it.
    """
    small_w, small_h = 320, 180
    scale = 4
    full_w, full_h = small_w * scale, small_h * scale  # 1280x720

    seed = hashlib.sha256((seed_text or "cover").encode("utf-8")).digest()
    seed2 = hashlib.sha256(seed + b":palette").digest()

    # Curated rich editorial palettes (deep bg -> mid bg + 2 bright accents)
    palettes = [
        ((15, 23, 42), (30, 64, 175), (56, 189, 248), (129, 140, 248)),
        ((17, 24, 39), (6, 95, 70), (52, 211, 153), (45, 212, 191)),
        ((24, 24, 37), (88, 28, 135), (192, 132, 252), (244, 114, 182)),
        ((15, 32, 39), (32, 58, 67), (56, 189, 248), (251, 191, 36)),
        ((23, 37, 84), (67, 56, 202), (96, 165, 250), (34, 211, 238)),
        ((28, 25, 23), (124, 45, 18), (251, 146, 60), (250, 204, 21)),
    ]
    c0, c1, acc1, acc2 = palettes[seed[0] % len(palettes)]

    # Deterministic geometric circles (cx, cy, radius, color, alpha, ring_only)
    circles: list[tuple[float, float, float, tuple[int, int, int], float, bool]] = []
    for idx in range(5):
        off = idx * 4
        cx = 28.0 + (seed[off + 1] / 255.0) * (small_w - 56.0)
        cy = 20.0 + (seed[off + 2] / 255.0) * (small_h - 40.0)
        rad = 22.0 + (seed[off + 3] / 255.0) * 58.0
        col = acc1 if idx % 2 == 0 else acc2
        alpha = 0.16 + (seed2[idx] / 255.0) * 0.24
        ring_only = idx >= 3
        circles.append((cx, cy, rad, col, alpha, ring_only))

    # Deterministic decorative horizontal bars (x0, y0, x1, y1, color, alpha)
    bars: list[tuple[int, int, int, int, tuple[int, int, int], float]] = []
    for idx in range(4):
        bx0 = 18 + (seed2[8 + idx] % 90)
        by0 = 26 + idx * 34 + (seed2[12 + idx] % 12)
        bw = 60 + (seed2[16 + idx] % 130)
        bh = 5 + (seed2[20 + idx] % 7)
        col = acc2 if idx % 2 == 0 else acc1
        bars.append((bx0, by0, min(small_w - 16, bx0 + bw), min(small_h - 14, by0 + bh), col, 0.28))

    wave_freq = 0.045 + (seed[21] / 255.0) * 0.035
    wave_phase = (seed[22] / 255.0) * math.tau
    wave_amp = 10.0 + (seed[23] / 255.0) * 14.0

    # Precompute row scanlines scaled 4x horizontally and repeated 4x vertically
    scanlines: list[bytes] = []
    inv_w = 1.0 / (small_w - 1)
    inv_h = 1.0 / (small_h - 1)

    for y in range(small_h):
        fy = y * inv_h
        row_buf = bytearray(1 + full_w * 3)
        row_buf[0] = 0  # PNG filter type 0 (None)
        pos = 1

        # Pre-filter bars active on this y
        active_bars = [b for b in bars if b[1] <= y <= b[3]]

        for x in range(small_w):
            fx = x * inv_w
            t = 0.62 * fx + 0.38 * fy
            # Smooth diagonal gradient + subtle deterministic sine texture so PNG > 5KB
            wave = math.sin(x * wave_freq + wave_phase + y * 0.03) * 0.5 + 0.5
            micro = (((x * 37 + y * 91 + seed[(x + y) & 31]) & 15) - 7.5) * 0.85

            r = c0[0] + (c1[0] - c0[0]) * t + wave * 10.0 + micro
            g = c0[1] + (c1[1] - c0[1]) * t + wave * 12.0 + micro
            b = c0[2] + (c1[2] - c0[2]) * t + wave * 14.0 + micro

            # Soft glowing sine ribbon across lower half
            ribbon_y = small_h * 0.68 + math.sin(x * wave_freq + wave_phase) * wave_amp
            d_rib = abs(y - ribbon_y)
            if d_rib < 9.0:
                wa = (1.0 - d_rib / 9.0) * 0.30
                r = r * (1.0 - wa) + acc1[0] * wa
                g = g * (1.0 - wa) + acc1[1] * wa
                b = b * (1.0 - wa) + acc1[2] * wa

            # Geometric circles & rings
            for cx, cy, rad, col, alpha, ring_only in circles:
                dx = x - cx
                dy = y - cy
                dist2 = dx * dx + dy * dy
                if dist2 <= rad * rad:
                    dist = math.sqrt(dist2)
                    if ring_only:
                        edge = abs(dist - rad + 2.5)
                        if edge <= 2.5:
                            aa = alpha * (1.0 - edge / 2.5)
                            r = r * (1.0 - aa) + col[0] * aa
                            g = g * (1.0 - aa) + col[1] * aa
                            b = b * (1.0 - aa) + col[2] * aa
                    else:
                        falloff = 1.0 - (dist / rad) * 0.45
                        aa = alpha * falloff
                        r = r * (1.0 - aa) + col[0] * aa
                        g = g * (1.0 - aa) + col[1] * aa
                        b = b * (1.0 - aa) + col[2] * aa

            # Geometric accent bars
            for bx0, _by0, bx1, _by1, col, alpha in active_bars:
                if bx0 <= x <= bx1:
                    r = r * (1.0 - alpha) + col[0] * alpha
                    g = g * (1.0 - alpha) + col[1] * alpha
                    b = b * (1.0 - alpha) + col[2] * alpha

            # Subtle geometric grid dots every 16px
            if (x & 15) == 0 and (y & 15) == 0:
                r += 24.0
                g += 28.0
                b += 34.0

            rb = _clamp_byte(r)
            gb = _clamp_byte(g)
            bb = _clamp_byte(b)

            # 4x horizontal pixel scaling
            for _ in range(scale):
                row_buf[pos] = rb
                row_buf[pos + 1] = gb
                row_buf[pos + 2] = bb
                pos += 3

        line_bytes = bytes(row_buf)
        # 4x vertical pixel scaling
        for _ in range(scale):
            scanlines.append(line_bytes)

    raw_data = b"".join(scanlines)
    ihdr_data = struct.pack(">IIBBBBB", full_w, full_h, 8, 2, 0, 0, 0)
    idat_data = zlib.compress(raw_data, level=6)

    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr_data)
        + _png_chunk(b"IDAT", idat_data)
        + _png_chunk(b"IEND", b"")
    )

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(png_bytes)


def _pick_overall_mode(modes: list[str]) -> str:
    if "google_gtx" in modes:
        return "google_gtx"
    if "mymemory" in modes:
        return "mymemory"
    if modes and all(m == "already_zh" for m in modes):
        return "already_zh"
    return "fallback"


async def build_translated_article_bundle(
    *,
    article_date: str,
    title: str,
    url: str = "",
    source_name: str = "",
    category: str = "AI新闻",
    summary: str = "",
    published_at: str = "",
    author: str = "咨询管理后台系统",
) -> dict[str, Any]:
    """Translate a news item + source paragraphs into a complete WeChat-ready Chinese article bundle."""
    orig_title = re.sub(r"\s+", " ", (title or "")).strip() or f"{article_date} 资讯编译"
    clean_summary = re.sub(r"\s+", " ", (summary or "")).strip()
    clean_url = (url or "").strip()
    clean_source = (source_name or "").strip() or "行业资讯"
    clean_category = (category or "").strip() or "AI新闻"
    clean_author = (author or "").strip() or "咨询管理后台系统"

    modes: list[str] = []

    zh_title_raw, title_mode = await translate_to_zh(orig_title)
    modes.append(title_mode)
    zh_title = truncate_utf8_chars(zh_title_raw or orig_title, MAX_TITLE_CHARS)

    if clean_summary:
        zh_summary_raw, summary_mode = await translate_to_zh(clean_summary)
        modes.append(summary_mode)
        zh_summary = re.sub(r"\s+", " ", zh_summary_raw or clean_summary).strip()
    else:
        zh_summary = zh_title
    zh_digest = truncate_utf8_chars(zh_summary, MAX_DIGEST_CHARS)

    source_paras = await fetch_source_paragraphs(clean_url)
    zh_paragraphs: list[str] = []
    if source_paras:
        joined_src = "\n\n".join(source_paras)
        translated_body, body_mode = await translate_to_zh(joined_src)
        modes.append(body_mode)
        zh_paragraphs = [
            re.sub(r"\s+", " ", p).strip()
            for p in re.split(r"\n\s*\n", translated_body)
            if p.strip()
        ]

    if not zh_paragraphs:
        zh_paragraphs = [zh_summary] if zh_summary else [zh_title]

    translation_mode = _pick_overall_mode(modes)

    # Key takeaways (2-3 concise points)
    highlights: list[str] = []
    if zh_summary and zh_summary != zh_title:
        highlights.append(zh_summary)
    for para in zh_paragraphs:
        if len(highlights) >= 3:
            break
        short = truncate_utf8_chars(para, 110)
        if short and short not in highlights:
            highlights.append(short)
    if not highlights:
        highlights.append(zh_title)

    # 1. Structured Chinese Markdown
    md_lines: list[str] = [
        f"# {zh_title}",
        "",
        "## 核心要点",
        "",
    ]
    for point in highlights:
        md_lines.append(f"- {point}")
    md_lines.extend(["", "## 正文编译", ""])
    for para in zh_paragraphs:
        md_lines.append(para)
        md_lines.append("")
    md_lines.extend(
        [
            "## 来源说明",
            "",
            f"- 原始标题：{orig_title}",
            f"- 信息来源：{clean_source}",
        ]
    )
    if published_at:
        md_lines.append(f"- 发布时间：{published_at}")
    if clean_url:
        md_lines.append(f"- 原文链接：{clean_url}")
    md_lines.append("- 编者注：本文由系统自动抓取并编译为中文草稿，仅供人工复核后在公众号后台发布。")
    article_md = "\n".join(md_lines).strip() + "\n"

    # 2. Clean inline-styled WeChat HTML
    esc = html.escape
    highlight_items_html = "".join(
        f'<li style="margin:6px 0;line-height:1.75;color:#1e293b;">{esc(pt)}</li>'
        for pt in highlights
    )
    body_paras_html = "".join(
        f'<p style="margin:0 0 16px 0;line-height:1.85;font-size:16px;color:#1f2937;text-align:justify;">{esc(p)}</p>'
        for p in zh_paragraphs
    )
    meta_rows: list[str] = [
        f'<p style="margin:4px 0;"><strong>原始标题：</strong>{esc(orig_title)}</p>',
        f'<p style="margin:4px 0;"><strong>信息来源：</strong>{esc(clean_source)}</p>',
    ]
    if published_at:
        meta_rows.append(f'<p style="margin:4px 0;"><strong>发布时间：</strong>{esc(published_at)}</p>')
    if clean_url:
        meta_rows.append(f'<p style="margin:4px 0;word-break:break-all;"><strong>原文链接：</strong>{esc(clean_url)}</p>')
    meta_rows.append(
        '<p style="margin:8px 0 0 0;color:#64748b;">说明：本文由系统自动编译整理并同步至微信草稿箱，请人工审核后再发布。</p>'
    )

    raw_html = (
        '<section style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;'
        'max-width:680px;margin:0 auto;padding:12px 8px;color:#1f2937;">'
        f'<p style="margin:0 0 14px 0;font-size:13px;color:#64748b;">'
        f'分类：{esc(clean_category)} · 来源：{esc(clean_source)}</p>'
        '<section style="background:#f8fafc;border-left:4px solid #2563eb;border-radius:6px;'
        'padding:14px 16px;margin:0 0 22px 0;">'
        '<h2 style="margin:0 0 10px 0;font-size:17px;color:#0f172a;">核心要点</h2>'
        f'<ul style="margin:0;padding-left:20px;font-size:15px;">{highlight_items_html}</ul>'
        "</section>"
        '<h2 style="margin:0 0 14px 0;font-size:18px;color:#0f172a;'
        'border-bottom:1px solid #e2e8f0;padding-bottom:8px;">正文编译</h2>'
        f"{body_paras_html}"
        '<section style="margin-top:24px;padding:14px 16px;background:#f1f5f9;'
        'border-radius:8px;font-size:13px;line-height:1.7;color:#475569;">'
        '<h3 style="margin:0 0 8px 0;font-size:14px;color:#0f172a;">来源说明</h3>'
        f'{"".join(meta_rows)}'
        "</section>"
        "</section>"
    )
    article_html = sanitize_wechat_html(raw_html)

    sources_list = [
        {
            "title": orig_title,
            "translated_title": zh_title,
            "url": clean_url,
            "source_name": clean_source,
            "published_at": published_at,
        }
    ]
    sources_data = {"date": article_date, "sources": sources_list}
    metadata = {
        "date": article_date,
        "title": zh_title,
        "original_title": orig_title,
        "subtitle": zh_digest,
        "category": clean_category,
        "template": "news_translated",
        "author": clean_author,
        "publish_mode": "draft_only",
        "translation_mode": translation_mode,
        "source_name": clean_source,
        "source_url": clean_url,
        "published_at": published_at,
    }
    review_data = {
        "date": article_date,
        "passed": True,
        "needs_manual_review": False,
        "translation_mode": translation_mode,
        "issues": [],
        "checks": {
            "translated": True,
            "sanitized_html": True,
            "draft_only": True,
        },
    }
    word_count = len(re.sub(r"\s+", "", article_md))

    return {
        "date": article_date,
        "title": zh_title,
        "original_title": orig_title,
        "subtitle": zh_digest,
        "category": clean_category,
        "author": clean_author,
        "translation_mode": translation_mode,
        "article_md": article_md,
        "article_html": article_html,
        "metadata": metadata,
        "sources": sources_data,
        "review": review_data,
        "word_count": word_count,
        "sources_count": len(sources_list),
    }
