"""Native render-pipeline integration for runtime Screen UI submission."""

from __future__ import annotations

from Infernux.lib import RenderPipelineCallback


class RuntimeScreenUIRenderPipeline(RenderPipelineCallback):
    """Submit Screen UI before delegating each engine-owned camera render."""

    def __init__(self, submission, delegate) -> None:
        super().__init__()
        self._submission = submission
        self._delegate = delegate

    def render(self, context, camera) -> None:
        self._submission.submit()
        self._delegate.render(context, camera)

    def dispose(self) -> None:
        self._delegate.dispose()
