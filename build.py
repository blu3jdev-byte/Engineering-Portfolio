#!/usr/bin/env python3
"""Static site generator for the engineering portfolio.

    python build.py                build the site into ./dist (what GitHub Pages publishes)
    python build.py serve          build, serve at http://localhost:8000, rebuild on changes
    python build.py --base ""      build with a different base path

How it works:
    content/projects/*.md        one Markdown file per project (YAML header + write-up)
    content/publications/*.md    one Markdown file per paper
    templates/*.html             Jinja2 page templates
    public/                      files copied as-is (images, papers, videos, css, favicon)
    config.py                    name, intro text, links
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import importlib
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from latex2mathml.converter import convert as tex_to_mathml
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound

import config

ROOT = Path(__file__).parent.resolve()
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"
PUBLIC = ROOT / "public"
DIST = ROOT / "dist"


class BuildError(Exception):
    """A problem in a content file or setting, reported without a stack trace."""


# ---------------------------------------------------------------------------
# Reading content files
# ---------------------------------------------------------------------------

FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n(.*))?\Z", re.S)


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def read_entry(path: Path) -> tuple[dict, str]:
    """Split a content file into its YAML header and Markdown body."""
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    if not match:
        raise BuildError(
            f"{rel(path)}: the file must start with a header block between two '---' lines."
        )
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise BuildError(f"{rel(path)}: the header is not valid YAML ({error}).") from error
    if not isinstance(meta, dict):
        raise BuildError(f"{rel(path)}: the header must be a list of 'key: value' lines.")
    return meta, match.group(2) or ""


def entry_files(folder: Path) -> list[Path]:
    """Markdown files in a folder. Files starting with an underscore are ignored."""
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*.md") if not p.name.startswith("_"))


def entry_id(path: Path, folder: Path) -> str:
    return path.relative_to(folder).with_suffix("").as_posix()


class Fields:
    """Reads one header block and gives clear errors for typos and wrong types."""

    def __init__(self, meta: dict, path: Path):
        self.meta = meta
        self.path = path
        self.used: set[str] = set()

    def fail(self, key: str, message: str):
        raise BuildError(f"{rel(self.path)}: '{key}' {message}")

    def _take(self, key: str, required: bool):
        self.used.add(key)
        value = self.meta.get(key)
        if value is None and required:
            self.fail(key, "is required.")
        return value

    def text(self, key: str, required: bool = False, default=None):
        value = self._take(key, required)
        if value is None:
            return default
        if not isinstance(value, str):
            self.fail(key, "must be text. Put it in quotes.")
        return value.strip()

    def number(self, key: str, required: bool = False, default=None):
        value = self._take(key, required)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(key, "must be a whole number, like 2025.")
        return value

    def boolean(self, key: str, default: bool = False) -> bool:
        value = self._take(key, False)
        if value is None:
            return default
        if not isinstance(value, bool):
            self.fail(key, "must be true or false.")
        return value

    def day(self, key: str, required: bool = False):
        value = self._take(key, required)
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value))
        except ValueError:
            self.fail(key, "must be a date written like 2026-03-01.")

    def choice(self, key: str, options: list[str], default: str) -> str:
        value = self.text(key, default=default)
        if value not in options:
            self.fail(key, f"must be one of: {', '.join(options)}.")
        return value

    def text_list(self, key: str) -> list[str]:
        value = self._take(key, False)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.fail(key, "must be a list of text items, one per line starting with '-'.")
        return [v.strip() for v in value]

    def records(self, key: str, required: list[str], optional: list[str] = ()) -> list[dict]:
        value = self._take(key, False)
        if value is None:
            return []
        if not isinstance(value, list):
            self.fail(key, "must be a list.")
        out = []
        for number, item in enumerate(value, 1):
            if not isinstance(item, dict):
                self.fail(key, f"item {number} must have fields like '{required[0]}: ...'.")
            unknown = set(item) - set(required) - set(optional)
            if unknown:
                allowed = ", ".join([*required, *optional])
                self.fail(key, f"item {number} has unknown field(s) {sorted(unknown)}. Allowed: {allowed}.")
            for name in required:
                if not isinstance(item.get(name), str) or not item[name].strip():
                    self.fail(key, f"item {number} needs '{name}'.")
            out.append({name: item.get(name) for name in [*required, *optional]})
        return out

    def finish(self):
        unknown = set(self.meta) - self.used
        if unknown:
            self.fail(
                ", ".join(sorted(unknown)),
                "is not a recognized header field. Check the spelling against content/projects/_TEMPLATE.md.",
            )


# ---------------------------------------------------------------------------
# Markdown, equations, and base-path handling
# ---------------------------------------------------------------------------

MD_EXTENSIONS = ["extra", "sane_lists", "codehilite", "pymdownx.arithmatex"]
MD_CONFIG = {
    "codehilite": {"guess_lang": False, "css_class": "highlight"},
    "pymdownx.arithmatex": {"generic": True},
}
INLINE_MATH = re.compile(r'<span class="arithmatex">\\\((.*?)\\\)</span>', re.S)
BLOCK_MATH = re.compile(r'<div class="arithmatex">\\\[(.*?)\\\]</div>', re.S)
ROOT_PATH_ATTR = re.compile(r'\b(src|href)="(/(?!/)[^"]*)"')


def equations_to_mathml(text: str) -> str:
    """Turn LaTeX equations into MathML while building, so no JavaScript is needed."""

    def convert(tex: str, display: str) -> str:
        tex = html.unescape(tex).strip()
        try:
            return tex_to_mathml(tex, display=display)
        except Exception as error:  # the converter raises many kinds of errors
            raise BuildError(f"Could not convert the equation {tex!r}: {error}") from error

    text = BLOCK_MATH.sub(lambda m: f'<div class="math-block">{convert(m.group(1), "block")}</div>', text)
    return INLINE_MATH.sub(lambda m: convert(m.group(1), "inline"), text)


def prefix_root_paths(text: str, base: str) -> str:
    """Let Markdown use /images/x.jpg and have the base path added automatically."""
    if not base:
        return text

    def add(match: re.Match) -> str:
        attr, path = match.groups()
        if path == base or path.startswith(base + "/"):
            return match.group(0)
        return f'{attr}="{base}{path}"'

    return ROOT_PATH_ATTR.sub(add, text)


def render_markdown(text: str, base: str) -> str:
    converter = markdown.Markdown(extensions=MD_EXTENSIONS, extension_configs=MD_CONFIG)
    return prefix_root_paths(equations_to_mathml(converter.convert(text)), base)


def make_url(base: str):
    def url(path: str) -> str:
        if re.match(r"^(https?:)?//", path) or path.startswith(("mailto:", "#")):
            return path
        return base + (path if path.startswith("/") else "/" + path)

    return url


def video_info(src: str) -> dict:
    """Accepts a YouTube link, a Vimeo link, or a path to a small video file."""
    youtube = re.search(r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([\w-]{11})", src)
    if youtube:
        return {"kind": "youtube", "id": youtube.group(1)}
    vimeo = re.search(r"vimeo\.com/(?:video/)?(\d+)", src)
    if vimeo:
        return {"kind": "vimeo", "id": vimeo.group(1)}
    return {"kind": "file", "id": ""}


# ---------------------------------------------------------------------------
# Content types
# ---------------------------------------------------------------------------


@dataclass
class Project:
    id: str
    title: str
    summary: str
    date: dt.date
    status: str
    context: str | None
    tools: list[str]
    tags: list[str]
    cover: str | None
    cover_alt: str | None
    videos: list[dict]
    gallery: list[dict]
    papers: list[str]
    links: list[dict]
    featured: bool
    draft: bool
    body_html: str

    @property
    def year(self) -> int:
        return self.date.year

    @property
    def date_label(self) -> str:
        return self.date.strftime("%B %Y")


@dataclass
class Publication:
    id: str
    title: str
    authors: list[str]
    venue: str
    year: int
    type: str
    abstract_url: str | None
    pdf: str | None
    doi: str | None
    url: str | None
    abstract_html: str

    @property
    def links(self) -> list[tuple[str, str]]:
        links = []
        if self.abstract_url:
            links.append(("Abstract", self.abstract_url))
        if self.pdf:
            links.append(("Full paper (PDF)", self.pdf))
        if self.doi:
            links.append(("DOI", f"https://doi.org/{self.doi}"))
        if self.url:
            links.append(("Publisher page", self.url))
        return links


def load_project(path: Path, base: str) -> Project:
    meta, body = read_entry(path)
    f = Fields(meta, path)
    videos = f.records("videos", ["src"], ["title", "caption"])
    for video in videos:
        video.update(video_info(video["src"]))
    project = Project(
        id=entry_id(path, CONTENT / "projects"),
        title=f.text("title", required=True),
        summary=f.text("summary", required=True),
        date=f.day("date", required=True),
        status=f.choice("status", ["Complete", "In progress", "Concept"], "Complete"),
        context=f.text("context"),
        tools=f.text_list("tools"),
        tags=f.text_list("tags"),
        cover=f.text("cover"),
        cover_alt=f.text("cover_alt"),
        videos=videos,
        gallery=f.records("gallery", ["src", "alt"], ["caption"]),
        papers=f.text_list("papers"),
        links=f.records("links", ["label", "href"]),
        featured=f.boolean("featured"),
        draft=f.boolean("draft"),
        body_html=render_markdown(body, base),
    )
    f.finish()
    return project


def load_publication(path: Path, base: str) -> Publication:
    meta, body = read_entry(path)
    f = Fields(meta, path)
    publication = Publication(
        id=entry_id(path, CONTENT / "publications"),
        title=f.text("title", required=True),
        authors=f.text_list("authors"),
        venue=f.text("venue", required=True),
        year=f.number("year", required=True),
        type=f.text("type", default="Paper"),
        abstract_url=f.text("abstract_url"),
        pdf=f.text("pdf"),
        doi=f.text("doi"),
        url=f.text("url"),
        abstract_html=render_markdown(body, base) if body.strip() else "",
    )
    if not publication.authors:
        f.fail("authors", "needs at least one author.")
    f.finish()
    return publication


# ---------------------------------------------------------------------------
# Drawing geometry for the home page beam
# ---------------------------------------------------------------------------


def beam_geometry() -> dict:
    arrows = " ".join(
        f"M{x} 102 V166 M{x - 5} 157 L{x} 168 L{x + 5} 157" for x in range(80, 641, 56)
    )

    def hatch(x1: int, x2: int, y: int) -> str:
        return " ".join(f"M{x} {y} l-7 9" for x in range(x1 + 6, x2 + 1, 9))

    return {"arrows": arrows, "hatches": f"{hatch(52, 108, 212)} {hatch(616, 664, 212)}"}


# ---------------------------------------------------------------------------
# Building the site
# ---------------------------------------------------------------------------


def make_env(base: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml", "svg"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        url=make_url(base),
        site=config,
        year=dt.date.today().year,
        beam=beam_geometry(),
    )
    return env


def pygments_css() -> str:
    for style in ("github-dark", "monokai"):
        try:
            return HtmlFormatter(style=style).get_style_defs(".highlight")
        except ClassNotFound:
            continue
    return ""


def build(base: str) -> None:
    started = time.time()
    base = base.rstrip("/")
    if base and not base.startswith("/"):
        raise BuildError("The base path must be empty or start with '/', like '/Portfolio'.")

    publications = sorted(
        (load_publication(p, base) for p in entry_files(CONTENT / "publications")),
        key=lambda p: (-p.year, p.title),
    )
    all_projects = [load_project(p, base) for p in entry_files(CONTENT / "projects")]
    projects = sorted((p for p in all_projects if not p.draft), key=lambda p: p.date, reverse=True)
    drafts = len(all_projects) - len(projects)

    by_id = {p.id: p for p in publications}
    for project in projects:
        for paper in project.papers:
            if paper not in by_id:
                raise BuildError(
                    f"content/projects/{project.id}.md: papers lists '{paper}', but there is no "
                    f"such file in content/publications. Available: {', '.join(by_id) or 'none'}."
                )

    env = make_env(base)

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    if PUBLIC.exists():
        shutil.copytree(PUBLIC, DIST, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".gitkeep"))
    (DIST / "css").mkdir(exist_ok=True)
    (DIST / "css" / "pygments.css").write_text(pygments_css(), encoding="utf-8")

    def title_for(title: str | None) -> str:
        return f"{title} | {config.NAME}" if title else f"{config.NAME}, {config.ROLE}"

    def write(path: str, template: str, title: str | None, description: str | None = None,
              section: str = "", **context) -> None:
        page = env.get_template(template).render(
            page_title=title_for(title),
            description=description or config.DESCRIPTION,
            section=section,
            **context,
        )
        target = DIST / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")

    featured_first = sorted(projects, key=lambda p: (not p.featured, -p.date.toordinal()))
    write(
        "index.html", "index.html", None,
        projects=featured_first[: config.HOME_PROJECT_COUNT], publications=publications,
    )
    write(
        "projects/index.html", "projects.html", "Projects", section="projects",
        description="Engineering problem statements and the solutions developed for them.",
        projects=projects,
    )
    for project in projects:
        write(
            f"projects/{project.id}/index.html", "project.html", project.title,
            description=project.summary, section="projects",
            project=project, papers=[by_id[i] for i in project.papers],
        )
    write(
        "publications/index.html", "publications.html", "Publications", section="publications",
        description="Technical papers and conference publications.", publications=publications,
    )
    write("404.html", "404.html", "Page not found")

    note = f", skipped {drafts} draft(s)" if drafts else ""
    print(
        f"Built {len(projects)} project(s) and {len(publications)} publication(s){note} "
        f"into {rel(DIST)}/ in {time.time() - started:.1f}s"
    )


# ---------------------------------------------------------------------------
# Local preview
# ---------------------------------------------------------------------------


def fingerprint() -> int:
    total = 0
    for folder in (CONTENT, TEMPLATES, PUBLIC):
        for path in folder.rglob("*"):
            if path.is_file():
                total += path.stat().st_mtime_ns
    return total + (ROOT / "config.py").stat().st_mtime_ns


def serve(port: int) -> None:
    build("")

    def watch() -> None:
        seen = fingerprint()
        while True:
            time.sleep(1)
            now = fingerprint()
            if now != seen:
                seen = now
                try:
                    importlib.reload(config)
                    build("")
                except BuildError as error:
                    print(f"Build failed: {error}", file=sys.stderr)

    threading.Thread(target=watch, daemon=True).start()

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(DIST)))
    print(f"Preview at http://localhost:{port}/  (press Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the portfolio site.")
    parser.add_argument("command", nargs="?", choices=["build", "serve"], default="build")
    parser.add_argument("--base", default=None, help="base path override, e.g. '' or '/Portfolio'")
    parser.add_argument("--port", type=int, default=8000, help="port for 'serve'")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.port)
        else:
            build(config.BASE_PATH if args.base is None else args.base)
    except BuildError as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
