"""Native render-pipeline integration for runtime Screen UI submission."""

from __future__ import annotations

from Infernux.debug import Debug
from Infernux.lib import RenderPipelineCallback


class RuntimeScreenUIRenderPipeline(RenderPipelineCallback):
    """Submit Screen UI before delegating each engine-owned camera render."""

    def __init__(self, submission, delegate) -> None:
        super().__init__()
        self._submission = submission
        self._delegate = delegate

    def render(self, context, camera) -> None:
        try:
            self._submission.submit()
        except Exception as exc:
            Debug.log_suppressed("RenderPipeline.ScreenUI", exc)
        self._delegate.render(context, camera)

    def dispose(self) -> None:
        dispose = getattr(self._delegate, "dispose", None)
        if callable(dispose):
            dispose()
