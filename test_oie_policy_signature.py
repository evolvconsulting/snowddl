"""OIE fork patch (0.67.5-oie.13) regression tests: policy signature and return type.

DESC MASKING POLICY on OIE_UG2_REH (2026-09-27) returned, for MART.MSK_CORPUS_VECTOR_BY_KINDS:
    signature   (VAL VECTOR(FLOAT, 1536), KINDS ARRAY)
    return_type VECTOR(FLOAT, 1536)
oie.12 expected "(VAL VECTOR, KINDS ARRAY)" and "VECTOR(FLOAT,1536)", so every plan dropped and
re-created the policy. Every non-VECTOR type must keep oie.12's exact strings.

Pure-unit: no Snowflake connection.
"""

from types import SimpleNamespace

import pytest

from snowddl.blueprint import DataType
from snowddl.resolver import policy_signature


def _args(*pairs):
    return [SimpleNamespace(name=n, type=DataType(t)) for n, t in pairs]


def _oie12_signature(arguments):
    # oie.12's expression, copied verbatim from masking_policy.py / row_access_policy.py.
    return f"({', '.join([f'{a.name} {a.type.base_type.name}' for a in arguments])})"


def _oie12_return_type(data_type):
    return str(data_type)


# --- VECTOR: the Snowflake-returned form -----------------------------------


def test_vector_signature_is_the_snowflake_form():
    args = _args(("VAL", "VECTOR(FLOAT, 1536)"), ("KINDS", "ARRAY"))
    assert policy_signature.signature(args) == "(VAL VECTOR(FLOAT, 1536), KINDS ARRAY)"


@pytest.mark.parametrize("spelling", ["VECTOR(FLOAT, 1536)", "VECTOR(FLOAT,1536)", "vector(float, 1536)"])
def test_vector_return_type_is_the_snowflake_form(spelling):
    assert policy_signature.return_type(DataType(spelling)) == "VECTOR(FLOAT, 1536)"


def test_int_vector_uses_the_same_form():
    assert policy_signature.return_type(DataType("VECTOR(INT, 16)")) == "VECTOR(INT, 16)"


def test_oie12_never_matched_a_vector_policy():
    # The defect, pinned: the old strings differ from what Snowflake returns.
    args = _args(("VAL", "VECTOR(FLOAT, 1536)"), ("KINDS", "ARRAY"))
    assert _oie12_signature(args) != "(VAL VECTOR(FLOAT, 1536), KINDS ARRAY)"
    assert _oie12_return_type(DataType("VECTOR(FLOAT, 1536)")) != "VECTOR(FLOAT, 1536)"


# --- every other type: byte-identical to oie.12 ----------------------------


NON_VECTOR = [
    "VARCHAR(16777216)",
    "VARCHAR(100)",
    "NUMBER(38,0)",
    "NUMBER(10,2)",
    "ARRAY",
    "VARIANT",
    "OBJECT",
    "BOOLEAN",
    "TIMESTAMP_NTZ(9)",
]


@pytest.mark.parametrize("type_str", NON_VECTOR)
def test_non_vector_return_type_is_unchanged(type_str):
    dt = DataType(type_str)
    assert policy_signature.return_type(dt) == _oie12_return_type(dt)


@pytest.mark.parametrize("type_str", NON_VECTOR)
def test_non_vector_signature_is_unchanged(type_str):
    args = _args(("VAL", type_str), ("KINDS", "ARRAY"))
    assert policy_signature.signature(args) == _oie12_signature(args)


def test_the_six_oie_text_policy_shapes_are_unchanged():
    # The argument lists of the six non-VECTOR OIE unified-gate policies.
    shapes = [
        [("VAL", "VARCHAR(16777216)"), ("KINDS", "ARRAY")],
        [("VAL", "VARCHAR(16777216)"), ("SUBJECT", "VARCHAR(16777216)")],
        [("VAL", "VARIANT"), ("SUBJECT", "VARCHAR(16777216)"), ("IS_CHUNKED", "BOOLEAN")],
        [("VAL", "VARCHAR(16777216)")],
        [("VAL", "VARIANT")],
        [("VAL", "ARRAY")],
    ]
    for shape in shapes:
        args = _args(*shape)
        assert policy_signature.signature(args) == _oie12_signature(args)
        assert policy_signature.return_type(args[0].type) == _oie12_return_type(args[0].type)


def test_both_resolvers_use_the_helper():
    import inspect

    from snowddl.resolver import masking_policy, row_access_policy

    for mod in (masking_policy, row_access_policy):
        src = inspect.getsource(mod)
        assert "policy_signature.signature(bp.arguments)" in src
        assert "a.type.base_type.name" not in src
    assert "policy_signature.return_type(bp.returns)" in inspect.getsource(masking_policy)
