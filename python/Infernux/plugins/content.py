"""Discover and read the human-facing pages shipped by an InxPackage."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import unquote, urlsplit

from Infernux.engine.path_utils import is_path_within, portable_path, resolved_path


PLUGIN_PAGES_DIRECTORY = "InxPluginPages"
LOCALIZED_CONTENT_LOCALE = "zh-CN"
LOCALIZED_CONTENT_SUFFIX = ".zh-CN"
PAGE_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^]]*)]\(\s*(?P<source><[^>]+>|[^)\s]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)


def discover_plugin_pages(plugin_root: str) -> tuple[dict[str, str], ...]:
    """Return deterministic page descriptors for conventional plugin docs."""
    root = resolved_path(plugin_root)
    if not root or not os.path.isdir(root):
        return ()
    root_files = [path for path in Path(root).iterdir() if path.is_file()]
    pages: list[dict[str, str]] = []

    for readme, locale in _root_documents(root_files, "README.md"):
        pages.append(_descriptor(root, readme, "intro", "Description", locale))

    seen = {(page["id"], page.get("locale", "")) for page in pages}
    pages_root = next(
        (
            path for path in Path(root).iterdir()
            if path.is_dir() and path.name == PLUGIN_PAGES_DIRECTORY
        ),
        None,
    )
    if pages_root is not None:
        for path in sorted(
            (item for item in pages_root.rglob("*") if item.is_file() and item.suffix.casefold() in PAGE_EXTENSIONS),
            key=lambda item: portable_path(str(item.relative_to(pages_root))).casefold(),
        ):
            relative, locale = _localized_document_identity(path.relative_to(pages_root))
            page_id = _slug(relative.replace("/", "."))
            key = (page_id, locale)
            if key in seen:
                continue
            pages.append(_descriptor(root, path, page_id, _document_title(path), locale))
            seen.add(key)

    for license_path, locale in _root_documents(root_files, "LICENSE"):
        key = ("license", locale)
        if key not in seen:
            pages.append(_descriptor(root, license_path, "license", "License", locale))
            seen.add(key)
    return tuple(pages)


def merge_plugin_pages(
    discovered: Iterable[Mapping[str, object]],
    explicit: object,
) -> list[dict[str, str]]:
    """Merge manifest-declared pages over conventionally discovered pages."""
    values = [normalize_page_descriptor(item) for item in discovered]
    if explicit is None:
        return values
    if not isinstance(explicit, list):
        raise ValueError("InxPackage pages must be a list")
    explicit_values = [normalize_page_descriptor(item) for item in explicit]
    explicit_keys = {(item["id"], item.get("locale", "")) for item in explicit_values}
    return explicit_values + [
        item for item in values
        if (item["id"], item.get("locale", "")) not in explicit_keys
    ]


def normalize_page_descriptor(value: object) -> dict[str, str]:
    if isinstance(value, str):
        path = portable_path(value).strip("/")
        value = {"path": path, "id": _slug(Path(path).stem), "title": Path(path).stem}
    if not isinstance(value, Mapping):
        raise ValueError("InxPackage page must be an object or relative path")
    path = portable_path(str(value.get("path", ""))).strip("/")
    if not _safe_relative(path) or not _supported_page_path(path):
        raise ValueError(f"InxPackage page path is invalid: {path}")
    page_id = str(value.get("id") or _slug(Path(path).stem)).strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", page_id):
        raise ValueError(f"InxPackage page id is invalid: {page_id}")
    title = str(value.get("title") or Path(path).stem).strip()
    if not title:
        raise ValueError("InxPackage page title cannot be empty")
    page_format = str(value.get("format") or _format_for_path(path)).strip().casefold()
    if page_format not in {"markdown", "text"}:
        raise ValueError(f"InxPackage page format is invalid: {page_format}")
    locale = str(value.get("locale", "")).strip()
    if locale not in {"", LOCALIZED_CONTENT_LOCALE}:
        raise ValueError(
            f"InxPackage page locale must be {LOCALIZED_CONTENT_LOCALE}: {locale}"
        )
    descriptor = {"id": page_id, "title": title, "path": path, "format": page_format}
    if locale:
        descriptor["locale"] = locale
    return descriptor


def normalize_locale(value: str) -> str:
    """Map an editor locale to the one supported localized-content suffix."""
    locale = str(value).strip()
    if locale in {"", "en"}:
        return ""
    if locale in {"zh", LOCALIZED_CONTENT_LOCALE}:
        return LOCALIZED_CONTENT_LOCALE
    raise ValueError(
        f"Plugin content locale must be en, zh, or {LOCALIZED_CONTENT_LOCALE}: {value}"
    )


def select_localized_pages(
    descriptors: Iterable[Mapping[str, object]],
    preferred_locale: str,
) -> tuple[dict[str, str], ...]:
    """Select ``.zh-CN`` content for Chinese, otherwise the default file."""
    preferred = normalize_locale(preferred_locale)
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    order: list[str] = []
    for index, value in enumerate(descriptors):
        descriptor = normalize_page_descriptor(value)
        page_id = descriptor["id"]
        if page_id not in groups:
            groups[page_id] = []
            order.append(page_id)
        groups[page_id].append((index, descriptor))
    selected: list[dict[str, str]] = []
    for page_id in order:
        _index, descriptor = min(
            groups[page_id],
            key=lambda item: (
                _locale_score(item[1].get("locale", ""), preferred),
                item[0],
            ),
        )
        selected.append(descriptor)
    return tuple(selected)


def read_plugin_pages(
    plugin_root: str,
    descriptors: object,
    *,
    maximum_bytes: int = 1024 * 1024,
    locale: str | None = None,
) -> tuple[dict[str, str], ...]:
    """Read validated page files without allowing paths outside the plugin."""
    if not isinstance(descriptors, list):
        return ()
    root = resolved_path(plugin_root)
    if not root or not os.path.isdir(root):
        return ()
    normalized = tuple(normalize_page_descriptor(raw) for raw in descriptors)
    if locale is not None:
        normalized = select_localized_pages(normalized, locale)
    pages: list[dict[str, str]] = []
    for descriptor in normalized:
        path = resolved_path(os.path.join(root, *descriptor["path"].split("/")))
        if not is_path_within(path, root, allow_root=False) or not os.path.isfile(path):
            continue
        if os.path.getsize(path) > maximum_bytes:
            continue
        try:
            content = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pages.append({**descriptor, "content": content})
    return tuple(pages)


def markdown_to_plain_text(value: str) -> str:
    """Produce readable editor text from common README Markdown constructs."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda match: match.group(1).strip(), text, flags=re.DOTALL)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`~]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_markdown_images(value: str) -> tuple[dict[str, str], ...]:
    """Split Markdown into text and image blocks without resolving paths."""
    text = str(value)
    blocks: list[dict[str, str]] = []
    cursor = 0
    for match in _MARKDOWN_IMAGE_PATTERN.finditer(text):
        if match.start() > cursor:
            blocks.append({"kind": "text", "content": text[cursor:match.start()]})
        source = match.group("source").strip().strip("<>")
        blocks.append({
            "kind": "image",
            "source": source,
            "alt": match.group("alt").strip(),
        })
        cursor = match.end()
    if cursor < len(text):
        blocks.append({"kind": "text", "content": text[cursor:]})
    return tuple(blocks) if blocks else ({"kind": "text", "content": text},)


def parse_markdown_blocks(value: str) -> tuple[dict[str, object], ...]:
    """Parse the block-level Markdown used by plugin information pages."""
    lines = str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, object]] = []
    paragraph: list[str] = []
    code_lines: list[str] | None = None
    code_language = ""

    def append_text_blocks(kind: str, content: str, **metadata: object) -> None:
        for item in split_markdown_images(content):
            if item["kind"] == "image":
                blocks.append(dict(item))
            elif item.get("content", "").strip():
                blocks.append({"kind": kind, "content": item["content"], **metadata})

    def flush_paragraph() -> None:
        if paragraph:
            append_text_blocks("paragraph", "\n".join(paragraph).strip())
            paragraph.clear()

    for line in lines:
        fence = re.match(r"^\s*```\s*([^`]*)$", line)
        if code_lines is not None:
            if fence:
                blocks.append({
                    "kind": "code",
                    "content": "\n".join(code_lines),
                    "language": code_language,
                })
                code_lines = None
                code_language = ""
            else:
                code_lines.append(line)
            continue
        if fence:
            flush_paragraph()
            code_lines = []
            code_language = fence.group(1).strip()
            continue
        if not line.strip():
            flush_paragraph()
            continue

        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_paragraph()
            blocks.append({
                "kind": "heading",
                "level": len(heading.group(1)),
                "content": heading.group(2),
            })
            continue
        if re.match(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", line):
            flush_paragraph()
            blocks.append({"kind": "divider"})
            continue

        quote = re.match(r"^\s{0,3}>\s?(.*)$", line)
        if quote:
            flush_paragraph()
            append_text_blocks("quote", quote.group(1))
            continue

        list_item = re.match(r"^(?P<indent>\s*)(?P<marker>[-*+]|\d+[.)])\s+(?P<content>.+)$", line)
        if list_item:
            flush_paragraph()
            marker = list_item.group("marker")
            append_text_blocks(
                "list_item",
                list_item.group("content"),
                ordered=marker[0].isdigit(),
                marker=marker,
                depth=min(6, len(list_item.group("indent").expandtabs(4)) // 2),
            )
            continue
        paragraph.append(line)

    flush_paragraph()
    if code_lines is not None:
        blocks.append({"kind": "code", "content": "\n".join(code_lines), "language": code_language})
    return tuple(blocks)


def resolve_plugin_page_asset(plugin_root: str, page_path: str, source: str) -> str:
    """Resolve a local Markdown asset while confining it to the plugin root."""
    value = str(source).strip()
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith("//"):
        return ""
    relative = portable_path(unquote(parsed.path))
    root_relative = relative.startswith("/")
    relative = relative.strip("/")
    if not relative:
        return ""
    base = "" if root_relative else portable_path(str(Path(page_path).parent))
    candidate = resolved_path(os.path.join(plugin_root, *(part for part in f"{base}/{relative}".split("/") if part)))
    if not is_path_within(candidate, plugin_root, allow_root=False) or not os.path.isfile(candidate):
        return ""
    return candidate


def intro_from_readme(plugin_root: str, pages: Iterable[Mapping[str, object]]) -> str:
    intro = next((item for item in select_localized_pages(pages, "") if item["id"] == "intro"), None)
    if intro is None:
        return ""
    loaded = read_plugin_pages(plugin_root, [intro])
    if not loaded:
        return ""
    return _intro_summary(loaded[0]["content"])


def localized_intros_from_readmes(
    plugin_root: str,
    pages: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    """Build localized package-list summaries from localized README pages."""
    result: dict[str, str] = {}
    for page in pages:
        descriptor = normalize_page_descriptor(page)
        locale = descriptor.get("locale", "")
        if descriptor["id"] != "intro" or not locale:
            continue
        loaded = read_plugin_pages(plugin_root, [descriptor])
        if loaded:
            summary = _intro_summary(loaded[0]["content"])
            if summary:
                result[locale] = summary
    return result


def localized_intro(metadata: Mapping[str, object], preferred_locale: str) -> str:
    """Return the package summary matching the editor locale."""
    candidates: list[tuple[str, str]] = []
    intro = str(metadata.get("intro", "")).strip()
    if intro:
        candidates.append(("", intro))
    raw = metadata.get("intros", {})
    if isinstance(raw, Mapping):
        for locale, value in raw.items():
            text = str(value).strip()
            if text:
                candidates.append((normalize_locale(str(locale)), text))
    if not candidates:
        return ""
    preferred = normalize_locale(preferred_locale)
    return min(
        enumerate(candidates),
        key=lambda item: (_locale_score(item[1][0], preferred), item[0]),
    )[1][1]


def _intro_summary(content: str) -> str:
    plain = markdown_to_plain_text(content)
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", plain) if item.strip()]
    if not paragraphs:
        return ""
    # Skip a README heading when the following paragraph carries the summary.
    summary = paragraphs[1] if len(paragraphs) > 1 and "\n" not in paragraphs[0] else paragraphs[0]
    return summary[:500].strip()


def _root_documents(
    files: Iterable[Path], default_name: str
) -> tuple[tuple[Path, str], ...]:
    localized_name = (
        f"{Path(default_name).stem}{LOCALIZED_CONTENT_SUFFIX}{Path(default_name).suffix}"
        if Path(default_name).suffix
        else f"{default_name}{LOCALIZED_CONTENT_SUFFIX}.md"
    )
    by_name = {path.name: path for path in files}
    return tuple(
        item for item in (
            (by_name[default_name], "") if default_name in by_name else None,
            (by_name[localized_name], LOCALIZED_CONTENT_LOCALE)
            if localized_name in by_name else None,
        )
        if item is not None
    )


def _localized_document_identity(relative_path: Path) -> tuple[str, str]:
    path = relative_path
    locale = ""
    if path.stem.endswith(LOCALIZED_CONTENT_SUFFIX):
        path = path.with_name(
            path.stem[: -len(LOCALIZED_CONTENT_SUFFIX)] + path.suffix
        )
        locale = LOCALIZED_CONTENT_LOCALE
    return portable_path(str(path.with_suffix(""))), locale


def _locale_score(locale: str, preferred: str) -> int:
    value = str(locale)
    if value == preferred:
        return 0
    if not value:
        return 10
    return 20


def _descriptor(
    root: str, path: Path, page_id: str, title: str, locale: str = ""
) -> dict[str, str]:
    relative = portable_path(str(path.relative_to(root)))
    descriptor = {
        "id": page_id,
        "title": title,
        "path": relative,
        "format": _format_for_path(relative),
    }
    if locale:
        descriptor["locale"] = locale
    return descriptor


def _document_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
                if match:
                    return match.group(1).strip(" #")
    except (OSError, UnicodeDecodeError):
        pass
    return path.stem.replace("_", " ").replace("-", " ").strip()


def _format_for_path(path: str) -> str:
    return "markdown" if Path(path).suffix.casefold() in {".md", ".markdown"} else "text"


def _supported_page_path(path: str) -> bool:
    normalized = portable_path(path).strip("/")
    if normalized in {
        "README.md",
        f"README{LOCALIZED_CONTENT_SUFFIX}.md",
        "LICENSE",
        f"LICENSE{LOCALIZED_CONTENT_SUFFIX}.md",
    }:
        return True
    prefix = PLUGIN_PAGES_DIRECTORY + "/"
    return (
        normalized.startswith(prefix)
        and Path(normalized).suffix.casefold() in PAGE_EXTENSIONS
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(value).casefold()).strip("-._") or "page"


def _safe_relative(value: str) -> bool:
    normalized = portable_path(value).strip("/")
    return (
        bool(normalized)
        and normalized == value.replace("\\", "/").strip("/")
        and all(part not in {"", ".", ".."} for part in normalized.split("/"))
        and not os.path.isabs(value)
    )


__all__ = [
    "PAGE_EXTENSIONS",
    "LOCALIZED_CONTENT_LOCALE",
    "LOCALIZED_CONTENT_SUFFIX",
    "PLUGIN_PAGES_DIRECTORY",
    "discover_plugin_pages",
    "intro_from_readme",
    "localized_intro",
    "markdown_to_plain_text",
    "merge_plugin_pages",
    "normalize_page_descriptor",
    "read_plugin_pages",
    "parse_markdown_blocks",
    "resolve_plugin_page_asset",
    "split_markdown_images",
]
