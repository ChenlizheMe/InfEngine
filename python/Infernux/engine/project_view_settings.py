"""Section-preserving storage for project-local editor view settings."""

from __future__ import annotations

import configparser
import os
from collections.abc import Mapping

from Infernux.core.document_store import write_document_text
from Infernux.engine.path_utils import resolved_path


def load_project_view_settings(path: str) -> configparser.ConfigParser:
    """Load one editor settings file without inventing compatibility state."""
    parser = configparser.ConfigParser()
    if not path or not os.path.isfile(path):
        return parser
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        parser.read_string(stream.read())
    return parser


def write_project_view_settings_section(
    path: str,
    section: str,
    values: Mapping[str, object],
) -> None:
    """Replace one section while preserving every other view's settings."""
    target = str(path or "").strip()
    section_name = str(section or "").strip()
    if not target:
        raise ValueError("project view settings require a path")
    if not section_name:
        raise ValueError("project view settings require a section")

    try:
        parser = load_project_view_settings(target)
    except (OSError, configparser.Error):
        parser = configparser.ConfigParser()
    parser[section_name] = {str(key): str(value) for key, value in values.items()}

    from io import StringIO

    output = StringIO()
    parser.write(output)
    os.makedirs(os.path.dirname(resolved_path(target)), exist_ok=True)
    write_document_text(target, output.getvalue())


__all__ = ["load_project_view_settings", "write_project_view_settings_section"]
