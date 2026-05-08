"""Migration component package.

Component classes live in submodules and are imported by callers as needed
(e.g. ``from src.application.components.user_migration import UserMigration``).

A registry-based discovery API is being phased in via
:mod:`src.application.components.registry`; once components self-register
through ``@register_component``, this package will eagerly import each
submodule so the decorators fire on first access. Until that step lands,
this ``__init__`` stays empty so importing a single component does not
pull in all 42 sibling modules.
"""
