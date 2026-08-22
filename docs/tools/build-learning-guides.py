"""Build bilingual static Learn pages from hand-authored Markdown chapters."""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import markdown


ROOT = Path(__file__).resolve().parents[1]
LEARN = ROOT / "learn"
COURSES_MANIFEST = LEARN / "learning-courses.json"
LANGUAGE_MARKER = "<!-- language:zh -->"


def render_markdown(source: str) -> str:
    return markdown.markdown(
        source.strip(),
        extensions=["fenced_code", "codehilite", "tables", "attr_list", "toc"],
        extension_configs={
            "codehilite": {
                "css_class": "highlight",
                "guess_lang": False,
                "linenums": False,
                "use_pygments": True,
            },
        },
        output_format="html5",
    )


def page_shell(course: dict, chapter: dict, previous: dict | None, following: dict | None, en: str, zh: str) -> str:
    slug = chapter["slug"]
    number = chapter["number"]
    total = course["chapter_count"]
    title = html.escape(chapter["title_en"])
    description = html.escape(chapter["summary_en"], quote=True)

    def chapter_link(target: dict | None, language: str, direction: str) -> str:
        if target is None:
            return '<span aria-hidden="true"></span>'
        label = target[f"title_{language}"]
        arrow = "←" if direction == "previous" else "→"
        text = f"{arrow} {label}" if direction == "previous" else f"{label} {arrow}"
        return f'<a href="{target["slug"]}.html">{html.escape(text)}</a>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; base-uri 'self'; object-src 'none'; script-src 'self'; script-src-attr 'none'; style-src 'self'; style-src-attr 'none'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; worker-src 'self'; upgrade-insecure-requests">
    <meta name="referrer" content="strict-origin-when-cross-origin">
    <meta name="description" content="{description}">
    <meta name="theme-color" content="#0a0c11">
    <meta name="color-scheme" content="dark light">
    <link rel="canonical" href="https://infernux-engine.com/learn/{slug}.html">
    <title>{title} · Infernux Learn</title>
    <link rel="icon" type="image/png" href="../assets/logo.png">
    <link rel="stylesheet" href="../css/fonts.css?v=1">
    <link rel="stylesheet" href="../css/style.css?v=20">
    <link rel="stylesheet" href="../css/docs-search.css?v=3">
    <link rel="stylesheet" href="../css/mission.css?v=2">
    <link rel="stylesheet" href="../css/learn.css?v=6">
    <link rel="stylesheet" href="../css/fontawesome-subset.css?v=6.4.0">
</head>
<body>
    <a class="skip-link" href="#main-content">Skip to content</a>
    <header class="site-header" role="banner">
        <div class="mission-ribbon" aria-label="Engine identity strip"><div class="mission-ribbon-inner"><span class="mission-kicker" data-i18n="brand.ribbonKicker">Open-source engine · 0.3.4</span><span class="mission-name" data-i18n="brand.ribbonName">INFER<span class="mission-accent">NUX</span></span><span class="mission-sub" data-i18n="brand.ribbonSub">熔炉 · ENG-CORE</span></div></div>
        <nav class="navbar" aria-label="Primary navigation"><div class="nav-container">
            <a href="../index.html" class="nav-logo"><span class="logo-icon"><img src="../assets/logo.png" width="256" height="256" alt="Infernux logo"></span><span class="logo-text" data-i18n="brand.navShort">熔炉 · INFERNUX</span></a>
            <div class="nav-links" id="primary-navigation"><a href="../start.html" class="nav-priority" data-i18n="nav.start">Start</a><a href="../learn.html" class="active" aria-current="page" data-i18n="nav.learn">Learn</a><a href="../wiki/site/en/api/index.html" data-href-en="../wiki/site/en/api/index.html" data-href-zh="../wiki/site/zh/api/index.html" data-i18n="nav.api">API</a><a href="../roadmap.html" data-i18n="nav.roadmap">Roadmap</a><a href="https://infernux-engine.discourse.group/" class="nav-priority" data-i18n="nav.community">Community</a><a href="../download.html" data-i18n="nav.download">Download</a><a href="https://github.com/ChenlizheMe/Infernux" target="_blank" rel="noopener"><i class="fab fa-github" aria-hidden="true"></i> GitHub</a></div>
            <div class="nav-right"><button class="theme-toggle" type="button" data-site-action="theme" title="Toggle theme" aria-label="Switch color theme" aria-pressed="false"><i class="fas fa-moon" id="theme-icon" aria-hidden="true"></i></button><button class="lang-toggle" type="button" data-site-action="language" aria-label="Switch language"><span id="lang-text">中文</span></button><button class="mobile-menu-btn" type="button" data-site-action="menu" aria-label="Open navigation menu" aria-controls="primary-navigation" aria-expanded="false"><i class="fas fa-bars" aria-hidden="true"></i></button></div>
        </div></nav>
    </header>

    <main class="page-shell learn-page" id="main-content"><div class="container"><article class="learn-article">
        <nav class="learn-article-nav" aria-label="Learning chapter navigation"><a href="{course['slug']}.html"><span data-page-language="en">{html.escape(course['title_en'])}</span><span data-page-language="zh" lang="zh-CN" hidden>{html.escape(course['title_zh'])}</span></a><span class="learn-chapter-position"><span data-page-language="en">{html.escape(course['short_en'])} · {number}/{total}</span><span data-page-language="zh" lang="zh-CN" hidden>{html.escape(course['short_zh'])} · {number}/{total}</span></span><a href="{slug}.md"><span data-page-language="en">Markdown source</span><span data-page-language="zh" lang="zh-CN" hidden>Markdown 源文档</span></a></nav>

        <div data-page-language="en" class="learn-article-body">{en}</div>
        <div data-page-language="zh" lang="zh-CN" class="learn-article-body" hidden>{zh}</div>

        <nav class="learn-course-pager" aria-label="Previous and next chapters">
            <div data-page-language="en">{chapter_link(previous, "en", "previous")}<span class="learn-course-home">Chapter {number} of {total}</span>{chapter_link(following, "en", "next")}</div>
            <div data-page-language="zh" lang="zh-CN" hidden>{chapter_link(previous, "zh", "previous")}<span class="learn-course-home">第 {number} 章，共 {total} 章</span>{chapter_link(following, "zh", "next")}</div>
        </nav>
    </article></div></main>

    <footer class="footer"><div class="container"><div class="footer-content"><div class="footer-brand"><div class="nav-logo"><span class="logo-icon"><img src="../assets/logo.png" width="256" height="256" alt="Infernux logo"></span><span class="logo-text" data-i18n="brand.footerTitle">熔炉 · INFERNUX</span></div><p data-i18n="footer.tagline">Open code, explicit architecture, and a render stack you can actually reason about.</p></div><div class="footer-links"><div class="footer-column"><h4 data-i18n="footer.resources">Resources</h4><a href="../start.html" data-i18n="nav.start">Start</a><a href="../learn.html" data-i18n="nav.learn">Learn</a><a href="../roadmap.html" data-i18n="nav.roadmap">Roadmap</a></div><div class="footer-column"><h4 data-i18n="footer.community">Community</h4><a href="https://infernux-engine.discourse.group/" data-i18n="nav.community">Community</a><a href="https://github.com/ChenlizheMe/Infernux/issues" target="_blank" rel="noopener" data-i18n="footer.issues">Issues</a></div></div></div><div class="footer-bottom">© 2024–2026 Lizhe Chen · MIT License · 熔炉 · INFERNUX</div></div></footer>
    <script src="../js/i18n-learn.js?v=1"></script>
    <script src="../js/i18n.js?v=19"></script>
    <script src="../js/main.js?v=15"></script>
    <script src="../js/docs-search.js?v=6"></script>
    <script src="../js/bilingual-page.js?v=1"></script>
</body>
</html>
'''


TAG_LABELS = {
    "basics": ("Basics", "基础"),
    "python": ("Python", "Python"),
    "gameplay": ("Gameplay", "游戏逻辑"),
    "editor": ("Editor", "编辑器"),
    "input": ("Input", "输入"),
    "prefab": ("Prefab", "Prefab"),
    "physics": ("Physics", "物理"),
    "events": ("Events", "事件"),
    "ui": ("UI", "UI"),
    "audio": ("Audio", "音频"),
    "coroutine": ("Coroutines", "协程"),
    "scene": ("Scenes", "场景"),
    "advanced": ("Advanced", "进阶"),
    "api": ("API", "API"),
    "rendering": ("Rendering", "渲染"),
    "shader": ("Shader", "Shader"),
}


def course_index_shell(course: dict, chapters: list[dict]) -> str:
    def filter_buttons(language: str) -> str:
        seen: list[str] = []
        for chapter in chapters:
            for tag in chapter["tags"]:
                if tag not in seen:
                    seen.append(tag)
        all_label = "All" if language == "en" else "全部"
        buttons = [f'<button type="button" class="learn-tag is-active" data-learn-tag="all" aria-pressed="true">{all_label}</button>']
        label_index = 0 if language == "en" else 1
        for tag in seen:
            label = TAG_LABELS.get(tag, (tag.title(), tag))[label_index]
            buttons.append(f'<button type="button" class="learn-tag" data-learn-tag="{html.escape(tag)}" aria-pressed="false">{html.escape(label)}</button>')
        return "".join(buttons)

    def chapter_entries(language: str) -> str:
        result: list[str] = []
        other = "zh" if language == "en" else "en"
        for chapter in chapters:
            labels = chapter[f"labels_{language}"]
            search = " ".join([
                chapter[f"title_{language}"],
                chapter[f"summary_{language}"],
                *labels,
            ])
            other_search = " ".join([
                chapter[f"title_{other}"],
                chapter[f"summary_{other}"],
                *chapter[f"labels_{other}"],
            ])
            search_en = search if language == "en" else other_search
            search_zh = other_search if language == "en" else search
            tags = " ".join(chapter["tags"])
            tag_html = "".join(f"<span>{html.escape(label)}</span>" for label in labels)
            result.append(
                f'<article class="learn-entry" data-learn-entry data-tags="{html.escape(tags)}" '
                f'data-search-en="{html.escape(search_en, quote=True)}" data-search-zh="{html.escape(search_zh, quote=True)}">'
                f'<a href="{chapter["slug"]}.html"><span class="learn-entry-number">{chapter["number"]:02d}</span>'
                f'<div><h2>{html.escape(chapter[f"title_{language}"])}</h2><p>{html.escape(chapter[f"summary_{language}"])}</p></div>'
                f'<div class="learn-entry-tags">{tag_html}</div></a></article>'
            )
        return "".join(result)

    title = html.escape(course["title_en"])
    description = html.escape(course["summary_en"], quote=True)
    total = len(chapters)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; base-uri 'self'; object-src 'none'; script-src 'self'; script-src-attr 'none'; style-src 'self'; style-src-attr 'none'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; worker-src 'self'; upgrade-insecure-requests">
    <meta name="referrer" content="strict-origin-when-cross-origin">
    <meta name="description" content="{description}">
    <meta name="theme-color" content="#0a0c11">
    <meta name="color-scheme" content="dark light">
    <link rel="canonical" href="https://infernux-engine.com/learn/{course['slug']}.html">
    <title>{title} · Infernux Learn</title>
    <link rel="icon" type="image/png" href="../assets/logo.png">
    <link rel="stylesheet" href="../css/fonts.css?v=1">
    <link rel="stylesheet" href="../css/style.css?v=20">
    <link rel="stylesheet" href="../css/docs-search.css?v=3">
    <link rel="stylesheet" href="../css/mission.css?v=2">
    <link rel="stylesheet" href="../css/learn.css?v=6">
    <link rel="stylesheet" href="../css/fontawesome-subset.css?v=6.4.0">
</head>
<body>
    <a class="skip-link" href="#main-content">Skip to content</a>
    <header class="site-header" role="banner">
        <div class="mission-ribbon" aria-label="Engine identity strip"><div class="mission-ribbon-inner"><span class="mission-kicker" data-i18n="brand.ribbonKicker">Open-source engine · 0.3.4</span><span class="mission-name" data-i18n="brand.ribbonName">INFER<span class="mission-accent">NUX</span></span><span class="mission-sub" data-i18n="brand.ribbonSub">熔炉 · ENG-CORE</span></div></div>
        <nav class="navbar" aria-label="Primary navigation"><div class="nav-container">
            <a href="../index.html" class="nav-logo"><span class="logo-icon"><img src="../assets/logo.png" width="256" height="256" alt="Infernux logo"></span><span class="logo-text" data-i18n="brand.navShort">熔炉 · INFERNUX</span></a>
            <div class="nav-links" id="primary-navigation"><a href="../start.html" class="nav-priority" data-i18n="nav.start">Start</a><a href="../learn.html" class="active" aria-current="page" data-i18n="nav.learn">Learn</a><a href="../wiki/site/en/api/index.html" data-href-en="../wiki/site/en/api/index.html" data-href-zh="../wiki/site/zh/api/index.html" data-i18n="nav.api">API</a><a href="../roadmap.html" data-i18n="nav.roadmap">Roadmap</a><a href="https://infernux-engine.discourse.group/" class="nav-priority" data-i18n="nav.community">Community</a><a href="../download.html" data-i18n="nav.download">Download</a><a href="https://github.com/ChenlizheMe/Infernux" target="_blank" rel="noopener"><i class="fab fa-github" aria-hidden="true"></i> GitHub</a></div>
            <div class="nav-right"><button class="theme-toggle" type="button" data-site-action="theme" title="Toggle theme" aria-label="Switch color theme" aria-pressed="false"><i class="fas fa-moon" id="theme-icon" aria-hidden="true"></i></button><button class="lang-toggle" type="button" data-site-action="language" aria-label="Switch language"><span id="lang-text">中文</span></button><button class="mobile-menu-btn" type="button" data-site-action="menu" aria-label="Open navigation menu" aria-controls="primary-navigation" aria-expanded="false"><i class="fas fa-bars" aria-hidden="true"></i></button></div>
        </div></nav>
    </header>

    <main class="page-shell learn-page" id="main-content"><div class="container learn-shell">
        <div data-page-language="en">
            <a class="learn-course-back" href="../learn.html">← All courses</a>
            <header class="learn-heading"><span class="mini-tag">{html.escape(course['short_en'])}</span><h1>{html.escape(course['title_en'])}</h1><p>{html.escape(course['summary_en'])}</p></header>
            <section class="learn-controls" aria-label="Filter course chapters"><label class="learn-search"><span class="visually-hidden">Search chapters</span><i class="fas fa-magnifying-glass" aria-hidden="true"></i><input type="search" data-learn-search placeholder="Search this course"></label><div class="learn-tags" aria-label="Chapter tags">{filter_buttons('en')}</div><p class="learn-result-status" data-learn-status role="status" aria-live="polite"></p></section>
            <section class="learn-track-intro"><span>{total} chapters · {html.escape(course['level_en'])}</span><h2>{html.escape(course['promise_en'])}</h2><p>{html.escape(course['detail_en'])}</p></section>
            <section class="learn-list" aria-label="Course chapters">{chapter_entries('en')}</section>
        </div>
        <div data-page-language="zh" lang="zh-CN" hidden>
            <a class="learn-course-back" href="../learn.html">← 全部板块</a>
            <header class="learn-heading"><span class="mini-tag">{html.escape(course['short_zh'])}</span><h1>{html.escape(course['title_zh'])}</h1><p>{html.escape(course['summary_zh'])}</p></header>
            <section class="learn-controls" aria-label="筛选课程章节"><label class="learn-search"><span class="visually-hidden">搜索章节</span><i class="fas fa-magnifying-glass" aria-hidden="true"></i><input type="search" data-learn-search placeholder="搜索这个板块"></label><div class="learn-tags" aria-label="章节标签">{filter_buttons('zh')}</div><p class="learn-result-status" data-learn-status role="status" aria-live="polite"></p></section>
            <section class="learn-track-intro"><span>共 {total} 章 · {html.escape(course['level_zh'])}</span><h2>{html.escape(course['promise_zh'])}</h2><p>{html.escape(course['detail_zh'])}</p></section>
            <section class="learn-list" aria-label="课程章节">{chapter_entries('zh')}</section>
        </div>
    </div></main>

    <footer class="footer"><div class="container"><div class="footer-content"><div class="footer-brand"><div class="nav-logo"><span class="logo-icon"><img src="../assets/logo.png" width="256" height="256" alt="Infernux logo"></span><span class="logo-text" data-i18n="brand.footerTitle">熔炉 · INFERNUX</span></div><p data-i18n="footer.tagline">Open code, explicit architecture, and a render stack you can actually reason about.</p></div><div class="footer-links"><div class="footer-column"><h4 data-i18n="footer.resources">Resources</h4><a href="../start.html" data-i18n="nav.start">Start</a><a href="../learn.html" data-i18n="nav.learn">Learn</a><a href="../roadmap.html" data-i18n="nav.roadmap">Roadmap</a></div><div class="footer-column"><h4 data-i18n="footer.community">Community</h4><a href="https://infernux-engine.discourse.group/" data-i18n="nav.community">Community</a><a href="https://github.com/ChenlizheMe/Infernux/issues" target="_blank" rel="noopener" data-i18n="footer.issues">Issues</a></div></div></div><div class="footer-bottom">© 2024–2026 Lizhe Chen · MIT License · 熔炉 · INFERNUX</div></div></footer>
    <script src="../js/i18n-learn.js?v=1"></script><script src="../js/i18n.js?v=19"></script><script src="../js/main.js?v=15"></script><script src="../js/docs-search.js?v=6"></script><script src="../js/bilingual-page.js?v=1"></script><script src="../js/learn.js?v=3"></script>
</body>
</html>
'''


def main() -> None:
    check = "--check" in sys.argv[1:]
    unknown = [argument for argument in sys.argv[1:] if argument != "--check"]
    if unknown:
        raise SystemExit(f"unknown arguments: {', '.join(unknown)}")
    courses = json.loads(COURSES_MANIFEST.read_text(encoding="utf-8"))
    for course in courses:
        chapters = json.loads((LEARN / course["manifest"]).read_text(encoding="utf-8"))
        course["chapter_count"] = len(chapters)
        course_output = course_index_shell(course, chapters)
        course_output_path = LEARN / f"{course['slug']}.html"
        if check:
            if not course_output_path.is_file() or course_output_path.read_text(encoding="utf-8") != course_output:
                raise RuntimeError(
                    f"{course_output_path.name} is stale; run: "
                    "python docs/tools/build-learning-guides.py"
                )
            print(f"verified learn/{course['slug']}.html")
        else:
            course_output_path.write_text(course_output, encoding="utf-8", newline="\n")
            print(f"built learn/{course['slug']}.html")

        for index, chapter in enumerate(chapters):
            source_path = LEARN / f"{chapter['slug']}.md"
            source = source_path.read_text(encoding="utf-8")
            if LANGUAGE_MARKER not in source:
                raise RuntimeError(f"{source_path.name} has no Chinese language marker")
            english_source, chinese_source = source.split(LANGUAGE_MARKER, 1)
            if english_source.startswith("<!-- language:en -->"):
                english_source = english_source.removeprefix("<!-- language:en -->")
            output = page_shell(
                course,
                chapter,
                chapters[index - 1] if index else None,
                chapters[index + 1] if index + 1 < len(chapters) else None,
                render_markdown(english_source),
                render_markdown(chinese_source),
            )
            output_path = LEARN / f"{chapter['slug']}.html"
            if check:
                if not output_path.is_file() or output_path.read_text(encoding="utf-8") != output:
                    raise RuntimeError(
                        f"{output_path.name} is stale; run: "
                        "python docs/tools/build-learning-guides.py"
                    )
                print(f"verified learn/{chapter['slug']}.html")
            else:
                output_path.write_text(output, encoding="utf-8", newline="\n")
                print(f"built learn/{chapter['slug']}.html")


if __name__ == "__main__":
    main()
