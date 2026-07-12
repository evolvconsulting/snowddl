# OIE fork patch (0.67.5-oie.1) — NEW converter.
# Upstream ships parser + resolver for PROCEDURE but no converter, so `snowddl-convert`
# silently skips stored procedures. This mirrors converter/function.py (procedures and
# functions share nearly all DESC properties) and emits `procedure/*.yaml` with a
# `body: !include` when --convert-function-body-to-file is set.
from snowddl.blueprint import ObjectType
from snowddl.converter.abc_converter import ConvertResult
from snowddl.converter.function import FunctionConverter
from snowddl.parser.procedure import procedure_json_schema
from snowddl.resolver._utils import dtypes_from_arguments


class ProcedureConverter(FunctionConverter):
    def get_object_type(self) -> ObjectType:
        return ObjectType.PROCEDURE

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW USER PROCEDURES IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            full_name = f"{r['catalog_name']}.{r['schema_name']}.{r['name']}({dtypes_from_arguments(r['arguments'])})"

            existing_objects[full_name] = {
                "database": r["catalog_name"],
                "schema": r["schema_name"],
                "name": r["name"],
                "arguments": r["arguments"],
                "comment": r["description"] if r["description"] else None,
            }

        return existing_objects

    def dump_object(self, row):
        dtypes = dtypes_from_arguments(row["arguments"])

        cur = self.engine.execute_meta(
            "DESC PROCEDURE {database:i}.{schema:i}.{name:i}({dtypes:r})",
            {
                "database": row["database"],
                "schema": row["schema"],
                "name": row["name"],
                "dtypes": dtypes,
            },
        )

        desc_proc_row = {r["property"]: r["value"] for r in cur}

        object_path = (
            self.base_path / self._normalise_name_with_prefix(row["database"]) / self._normalise_name(row["schema"]) / "procedure"
        )

        data = {
            "language": desc_proc_row.get("language"),
            "runtime_version": desc_proc_row.get("runtime_version"),
            "arguments": self._get_arguments(desc_proc_row),
            "returns": self._get_returns(desc_proc_row),
            "is_strict": True if desc_proc_row.get("null handling") == "RETURNS NULL ON NULL INPUT" else None,
            "is_execute_as_caller": True if desc_proc_row.get("execute as") == "CALLER" else None,
            "imports": self._get_imports(desc_proc_row),
            "packages": self._get_packages(desc_proc_row),
            "handler": desc_proc_row.get("handler"),
            "body": self._get_body_or_include(object_path, row["name"], dtypes, desc_proc_row),
            "comment": row.get("comment"),
        }

        if data:
            file_name = self._normalise_name(f"{row['name']}({dtypes}).yaml")
            self._dump_file(object_path / file_name, data, procedure_json_schema)
            return ConvertResult.DUMP

        return ConvertResult.EMPTY
