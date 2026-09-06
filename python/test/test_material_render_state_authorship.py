"""Render-state authorship semantics.

The material always wins over the shader: shader annotations only supply
*default* render-state values for fields the material has not explicitly
authored. Setting the full render state claims authorship of every field,
a single-field facade edit claims only its own field, and switching the
material to a different shader hands authorship back to the new shader's
annotation defaults.
"""

from __future__ import annotations

from Infernux.lib import InxMaterial, RenderStateOverride

_ALL_FLAGS = (
    RenderStateOverride.CULL_MODE,
    RenderStateOverride.DEPTH_WRITE,
    RenderStateOverride.DEPTH_TEST,
    RenderStateOverride.DEPTH_COMPARE_OP,
    RenderStateOverride.BLEND_ENABLE,
    RenderStateOverride.BLEND_MODE,
    RenderStateOverride.RENDER_QUEUE,
    RenderStateOverride.SURFACE_TYPE,
    RenderStateOverride.ALPHA_CLIP,
)


def test_set_render_state_claims_full_authorship():
    material = InxMaterial("Authored", "Unlit")
    assert material.render_state_overrides == 0

    state = material.get_render_state()
    state.cull_mode = 0
    state.render_queue = 3000
    material.set_render_state(state)

    for flag in _ALL_FLAGS:
        assert material.has_override(flag), f"missing override for {flag}"


def test_shader_switch_resets_authorship_but_resolution_does_not():
    material = InxMaterial("Switched", "Unlit")
    state = material.get_render_state()
    state.cull_mode = 0
    material.set_render_state(state)
    assert material.render_state_overrides != 0

    # Same shader name: not a switch, authorship survives.
    material.set_shader("Unlit")
    assert material.render_state_overrides != 0

    # Different shader: annotations of the new shader become the baseline.
    material.set_shader("Lit")
    assert material.render_state_overrides == 0


def test_authored_state_survives_shader_annotation_defaults():
    material = InxMaterial("Survives", "Unlit")
    state = material.get_render_state()
    state.cull_mode = 0
    state.render_queue = 3000
    material.set_render_state(state)

    # "Unlit" annotation defaults are Cull Back / Queue 2000; authored
    # values must be untouched.
    material.apply_shader_render_meta("back", "", "", "", 2000)

    state = material.get_render_state()
    assert state.cull_mode == 0
    assert state.render_queue == 3000


def test_facade_single_field_edit_claims_only_its_own_override():
    from Infernux.core.material import Material

    material = Material(native=InxMaterial("FacadeEdit", "Unlit"))
    material.cull_mode = 0

    native = material._native
    assert native.has_override(RenderStateOverride.CULL_MODE)
    # The untouched fields keep following the shader's annotation defaults.
    assert not native.has_override(RenderStateOverride.RENDER_QUEUE)
    assert not native.has_override(RenderStateOverride.BLEND_ENABLE)
