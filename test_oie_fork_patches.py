"""OIE fork patch (0.67.5-oie.1) regression tests.

Covers the two parser/emitter fixes carried on top of upstream 0.67.5. Pure-unit
(no Snowflake connection): they exercise the exact emit/parse paths that halted
`snowddl-convert` / `snowddl plan` against the OIE account.

  * Fix #1 — VECTOR emitter round-trip: DESC TABLE emits "VECTOR(FLOAT, 1536)" (space
    after comma); the table parser's col_type_re rejects that space, so the emitted
    YAML failed to re-parse and `plan` halted on the 13 real VECTOR columns. The
    emitter now normalises to the no-space form the parser accepts.
  * Fix #2 — bare-VARCHAR returns: DESC PROCEDURE/FUNCTION can report a bare "VARCHAR"
    (no length), which DataType() rejected -> convert aborted (exit 8). Now defaulted.
"""

import pytest

from snowddl.blueprint import DataType
from snowddl.converter.table import TableConverter
from snowddl.converter.function import FunctionConverter
from snowddl.parser.table import col_type_re


# --- Fix #1: VECTOR emitter round-trip -------------------------------------


@pytest.mark.parametrize(
    "desc_type,expected",
    [
        ("VECTOR(FLOAT, 1536)", "VECTOR(FLOAT,1536)"),  # the real OIE embedding cols (1536-d)
        ("VECTOR(FLOAT, 256)", "VECTOR(FLOAT,256)"),
        ("VECTOR(INT, 16)", "VECTOR(INT,16)"),
        ("vector(float, 1536)", "VECTOR(FLOAT,1536)"),  # case-insensitive
        ("VECTOR(FLOAT,1536)", "VECTOR(FLOAT,1536)"),  # already no-space -> unchanged
    ],
)
def test_vector_emitter_drops_space(desc_type, expected):
    assert TableConverter._normalise_col_type(None, desc_type) == expected


def test_vector_emitter_output_reparses():
    # The point of the fix: the emitted type must be accepted by BOTH the table column
    # parser regex and DataType. Upstream 0.67.5 rejected the spaced form -> plan exit 8.
    normalised = TableConverter._normalise_col_type(None, "VECTOR(FLOAT, 1536)")
    assert col_type_re.match(normalised) is not None
    assert str(DataType(normalised)) == "VECTOR(FLOAT,1536)"


def test_non_vector_types_untouched():
    # Targeted fix: it must not rewrite any other type.
    for t in ("NUMBER(38,0)", "VARCHAR(16777216)", "TIMESTAMP_NTZ(9)", "BOOLEAN", "VARIANT"):
        assert TableConverter._normalise_col_type(None, t) == t


# --- Fix #2: bare-VARCHAR returns ------------------------------------------


def test_bare_varchar_return_defaults_length():
    # DESC PROCEDURE on OIE's SP_APPLY_MERGE_LEDGER returns bare "VARCHAR".
    # Upstream DataType("VARCHAR") raised ValueError -> convert exit 8.
    result = FunctionConverter._get_returns_single(None, {"returns": "VARCHAR"})
    assert result == "VARCHAR(16777216)"
    assert str(DataType(result)) == "VARCHAR(16777216)"


def test_sized_return_unchanged():
    assert FunctionConverter._get_returns_single(None, {"returns": "NUMBER(38,0)"}) == "NUMBER(38,0)"


def test_propertyless_return_unchanged():
    # FLOAT / VARIANT have zero properties -> no defaulting, no crash.
    assert FunctionConverter._get_returns_single(None, {"returns": "FLOAT"}) == "FLOAT"
    assert FunctionConverter._get_returns_single(None, {"returns": "VARIANT"}) == "VARIANT"


def test_bare_varchar_upstream_would_crash():
    # Guard: prove the pre-patch code path (raw DataType on a bare sized type) really failed,
    # so this test is non-vacuous.
    with pytest.raises(ValueError):
        DataType("VARCHAR")
