"""Tests for Infernux.components.decorators — component class decorators."""

from Infernux.components.decorators import (
    require_component,
    disallow_multiple,
    execute_in_edit_mode,
    add_component_menu,
    icon,
    help_url,
    RequireComponent,
    DisallowMultipleComponent,
    ExecuteInEditMode,
    AddComponentMenu,
    HelpURL,
    Icon,
)


# ── Dummy component stand-ins ──

class _DummyA:
    pass

class _DummyB:
    pass


# ══════════════════════════════════════════════════════════════════════
# require_component
# ══════════════════════════════════════════════════════════════════════

class TestRequireComponent:
    def test_single_requirement(self):
        @require_component(_DummyA)
        class Comp:
            pass
        assert _DummyA in Comp._require_components_

    def test_multiple_requirements(self):
        @require_component(_DummyA, _DummyB)
        class Comp:
            pass
        assert _DummyA in Comp._require_components_
        assert _DummyB in Comp._require_components_

    def test_no_duplicates(self):
        @require_component(_DummyA)
        @require_component(_DummyA, _DummyB)
        class Comp:
            pass
        assert Comp._require_components_.count(_DummyA) == 1

    def test_alias_RequireComponent(self):
        assert RequireComponent is require_component

    def test_subclass_requirement_does_not_mutate_base_or_sibling(self):
        @require_component(_DummyA)
        class Base:
            pass

        @require_component(_DummyB)
        class Child(Base):
            pass

        class Sibling(Base):
            pass

        assert Base._require_components_ == [_DummyA]
        assert Child._require_components_ == [_DummyA, _DummyB]
        assert Sibling._require_components_ == [_DummyA]


# ══════════════════════════════════════════════════════════════════════
# disallow_multiple
# ══════════════════════════════════════════════════════════════════════

class TestDisallowMultiple:
    def test_without_parens(self):
        @disallow_multiple
        class Comp:
            pass
        assert Comp._disallow_multiple_ is True

    def test_with_parens(self):
        @disallow_multiple()
        class Comp:
            pass
        assert Comp._disallow_multiple_ is True

    def test_alias(self):
        assert DisallowMultipleComponent is disallow_multiple


# ══════════════════════════════════════════════════════════════════════
# execute_in_edit_mode
# ══════════════════════════════════════════════════════════════════════

class TestExecuteInEditMode:
    def test_without_parens(self):
        @execute_in_edit_mode
        class Comp:
            pass
        assert Comp._execute_in_edit_mode_ is True

    def test_with_parens(self):
        @execute_in_edit_mode()
        class Comp:
            pass
        assert Comp._execute_in_edit_mode_ is True

    def test_alias(self):
        assert ExecuteInEditMode is execute_in_edit_mode


# ══════════════════════════════════════════════════════════════════════
# add_component_menu
# ══════════════════════════════════════════════════════════════════════

class TestAddComponentMenu:
    def test_sets_menu_path(self):
        @add_component_menu("Physics/My Controller")
        class Comp:
            pass
        assert Comp._component_menu_path_ == "Physics/My Controller"

    def test_alias(self):
        assert AddComponentMenu is add_component_menu


# ══════════════════════════════════════════════════════════════════════
# icon
# ══════════════════════════════════════════════════════════════════════

class TestIcon:
    def test_sets_icon_path(self):
        @icon("icons/star.png")
        class Comp:
            pass
        assert Comp._component_icon_ == "icons/star.png"

    def test_alias(self):
        assert Icon is icon


# ══════════════════════════════════════════════════════════════════════
# help_url
# ══════════════════════════════════════════════════════════════════════

class TestHelpUrl:
    def test_sets_help_url(self):
        @help_url("https://example.com/docs")
        class Comp:
            pass
        assert Comp._help_url_ == "https://example.com/docs"

    def test_alias(self):
        assert HelpURL is help_url


# ══════════════════════════════════════════════════════════════════════
# Stacking decorators
# ══════════════════════════════════════════════════════════════════════

class TestCombined:
    def test_all_decorators_stack(self):
        @require_component(_DummyA)
        @disallow_multiple
        @execute_in_edit_mode
        @add_component_menu("Test/All")
        @icon("icons/test.png")
        @help_url("https://example.com")
        class Comp:
            pass

        assert _DummyA in Comp._require_components_
        assert Comp._disallow_multiple_ is True
        assert Comp._execute_in_edit_mode_ is True
        assert Comp._component_menu_path_ == "Test/All"
        assert Comp._component_icon_ == "icons/test.png"
        assert Comp._help_url_ == "https://example.com"
