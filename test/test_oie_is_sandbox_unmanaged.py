"""OIE fork patch (D-218 / OIE-133): `is_sandbox` schemas are recognized-but-not-managed.

Pure-Python unit test (no Snowflake connection) guarding the `_is_unmanaged_blueprint`
hook against silent loss on a future upstream rebase. The hook makes a whole-config
`snowddl apply`, run by a role that does not own (and may not even see) another
management tier's schema, skip create/compare of that schema and its declared child
objects instead of erroring with "already exists, but current role has no privileges".
"""

from types import SimpleNamespace

from snowddl.blueprint import ObjectType
from snowddl.resolver.abc_resolver import AbstractResolver
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver
from snowddl.resolver.schema import SchemaResolver


class _FakeConfig:
    def __init__(self, schema_blueprints):
        self._schema_blueprints = schema_blueprints

    def get_blueprints_by_type(self, _cls):
        return self._schema_blueprints


def _engine(schema_blueprints=None):
    return SimpleNamespace(config=_FakeConfig(schema_blueprints or {}))


def _schema_bp(is_sandbox):
    return SimpleNamespace(is_sandbox=is_sandbox)


class _StubSchemaObjectResolver(AbstractSchemaObjectResolver):
    def get_object_type(self):
        return ObjectType.TABLE

    def get_blueprints(self):
        return {}

    def get_existing_objects_in_schema(self, schema):
        return {}

    def create_object(self, bp):  # pragma: no cover - not exercised
        raise AssertionError("unmanaged object should never be created")

    def compare_object(self, bp, row):  # pragma: no cover - not exercised
        raise AssertionError("unmanaged object should never be compared")

    def drop_object(self, row):  # pragma: no cover - not exercised
        raise AssertionError("unmanaged object should never be dropped")


def test_base_resolver_manages_everything_by_default():
    # Upstream default: nothing is treated as unmanaged.
    assert AbstractResolver._is_unmanaged_blueprint(object.__new__(SchemaResolver), "OIE.MDM") is False


def test_schema_resolver_skips_is_sandbox_schemas():
    resolver = SchemaResolver(_engine())
    resolver.blueprints = {
        "OIE.OPS": _schema_bp(True),  # invisible other-tier -> would hit CREATE
        "OIE.GOVERNANCE": _schema_bp(True),  # visible other-tier -> would hit COMPARE
        "OIE.MDM": _schema_bp(False),  # managed data-plane
        "OIE.RAW": _schema_bp(None),  # is_sandbox unset -> managed
    }

    assert resolver._is_unmanaged_blueprint("OIE.OPS") is True
    assert resolver._is_unmanaged_blueprint("OIE.GOVERNANCE") is True
    assert resolver._is_unmanaged_blueprint("OIE.MDM") is False
    assert resolver._is_unmanaged_blueprint("OIE.RAW") is False
    assert resolver._is_unmanaged_blueprint("OIE.NOT_IN_CONFIG") is False


def test_schema_object_resolver_skips_children_of_is_sandbox_schemas():
    resolver = _StubSchemaObjectResolver(
        _engine(
            {
                "OIE.OPS": _schema_bp(True),
                "OIE.MART": _schema_bp(False),
            }
        )
    )

    # OPS.SP_PROVISION_REHEARSAL_CLONE lives in an is_sandbox schema -> skipped.
    assert resolver._is_unmanaged_blueprint("OIE.OPS.SP_PROVISION_REHEARSAL_CLONE") is True
    # A managed data-plane object is still reconciled.
    assert resolver._is_unmanaged_blueprint("OIE.MART.T_SOMETHING") is False
    # Unknown parent schema -> manage (net-new, owned by the applying role).
    assert resolver._is_unmanaged_blueprint("OIE.UNKNOWN.X") is False
