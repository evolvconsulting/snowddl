"""OIE fork patch (0.67.5-oie.13): the policy signature and return type as Snowflake spells them.

DESC MASKING POLICY and DESC ROW ACCESS POLICY report a VECTOR type with its dimensions and a
space after the comma, "VECTOR(FLOAT, 1536)", in both the signature and the return type. Upstream
built the signature from the bare base type ("VECTOR") and the return type from str(DataType)
("VECTOR(FLOAT,1536)"), so a VECTOR policy never compared equal and every plan dropped and
re-created it. Measured on OIE_UG2_REH, 2026-09-27, MART.MSK_CORPUS_VECTOR_BY_KINDS.

Every other type keeps upstream's exact strings: the base type name in the signature, str() for
the return type.
"""

from snowddl.blueprint import BaseDataType, DataType


def _vector(data_type: DataType) -> str:
    return f"VECTOR({data_type.val1}, {data_type.val2})"


def signature_arg_type(data_type: DataType) -> str:
    if data_type.base_type == BaseDataType.VECTOR:
        return _vector(data_type)

    return data_type.base_type.name


def signature(arguments) -> str:
    return f"({', '.join([f'{a.name} {signature_arg_type(a.type)}' for a in arguments])})"


def return_type(data_type: DataType) -> str:
    if data_type.base_type == BaseDataType.VECTOR:
        return _vector(data_type)

    return str(data_type)
