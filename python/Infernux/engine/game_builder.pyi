"""GameBuilder — compile an Infernux project into a standalone executable.

Orchestrates Nuitka compilation, asset copying, splash processing,
build manifest generation, and final cleanup.

Example::

    builder = GameBuilder(
        project_path="/path/to/project",
        output_dir="/path/to/output",
        on_progress=lambda msg, pct: print(f"{msg} ({pct*100:.0f}%)"),
    )
    builder.build()
"""

from __future__ import annotations

from typing import Callable, List, Optional


class GameBuilder:
    """Compile a project into a standalone player executable."""

    project_path: str
    project_name: str
    output_dir: str
    icon_path: str
    display_mode: str
    window_width: int
    window_height: int
    window_resizable: bool
    splash_items: list

    def __init__(
        self,
        project_path: str,
        output_dir: str,
        *,
        game_name: str = ...,
        icon_path: Optional[str] = None,
        display_mode: str = ...,
        window_width: int = ...,
        window_height: int = ...,
        window_resizable: bool = ...,
        splash_items: Optional[List[dict]] = ...,
    ) -> None: ...

    def build(
        self,
        on_progress: Optional[Callable[[str, float], None]] = ...,
        cancel_event: object = ...,
    ) -> str:
        """Run the full build pipeline.

        Returns:
            Path to the final output directory containing the built executable.

        Raises:
            RuntimeError: If validation, compilation, or packaging fails.
        """
        ...
