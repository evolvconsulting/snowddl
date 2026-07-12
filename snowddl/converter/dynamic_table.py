# OIE fork patch (0.67.5-oie.1) — NEW converter.
# Upstream ships parser + resolver for DYNAMIC_TABLE but no converter. Emits
# `dynamic_table/*.yaml` with text / target_lag / warehouse / refresh_mode / columns
# from `SHOW AS RESOURCE DYNAMIC TABLES` (same metadata source the resolver reads back).
from json import loads as json_loads
from pathlib import Path

from snowddl.blueprint import ObjectType
from snowddl.converter.abc_converter import ConvertResult
from snowddl.converter.abc_schema_object_converter import AbstractSchemaObjectConverter
from snowddl.converter._yaml import YamlLiteralStr, YamlIncludeStr
from snowddl.parser.dynamic_table import dynamic_table_json_schema


class DynamicTableConverter(AbstractSchemaObjectConverter):
    # Ordered largest-first so target_lag renders in the coarsest whole unit.
    target_lag_units = [
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
        ("second", 1),
    ]

    def get_object_type(self) -> ObjectType:
        return ObjectType.DYNAMIC_TABLE

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW AS RESOURCE DYNAMIC TABLES IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            res = json_loads(r["As Resource"])

            existing_objects[f"{res['database_name']}.{res['schema_name']}.{res['name']}"] = {
                "database": res["database_name"],
                "schema": res["schema_name"],
                "name": res["name"],
                "is_transient": res["kind"] == "TRANSIENT",
                "retention_time": res["data_retention_time_in_days"],
                "columns": res["columns"],
                "text": res["query"].rstrip(";"),
                "cluster_by": res["cluster_by"],
                "target_lag": res["target_lag"] if res["target_lag"] else None,
                "refresh_mode": res["refresh_mode"],
                "warehouse": res["warehouse"],
                "scheduler": res["scheduler"] if res["scheduler"] else "ENABLE",
                "comment": res["comment"] if res["comment"] else None,
            }

        return existing_objects

    def dump_object(self, row):
        object_path = (
            self.base_path
            / self._normalise_name_with_prefix(row["database"])
            / self._normalise_name(row["schema"])
            / "dynamic_table"
        )

        data = {
            "columns": self._get_columns(row),
            "text": self._get_text_or_include(object_path, row),
            "target_lag": self._get_target_lag(row["target_lag"]),
            "warehouse": self._normalise_name_with_prefix(row["warehouse"]),
            "refresh_mode": row["refresh_mode"],
            "is_transient": True if row["is_transient"] else None,
            "retention_time": row["retention_time"],
            "cluster_by": list(row["cluster_by"]) if row["cluster_by"] else None,
            "comment": row["comment"],
        }

        if data:
            self._dump_file(object_path / f"{self._normalise_name(row['name'])}.yaml", data, dynamic_table_json_schema)
            return ConvertResult.DUMP

        return ConvertResult.EMPTY

    def _get_columns(self, row):
        cols = {}

        for c in row["columns"]:
            cols[self._normalise_name(c["name"])] = c["comment"] if c.get("comment") else None

        return cols

    def _get_text_or_include(self, object_path: Path, row: dict):
        text = row["text"].strip(" \n\r\t")

        # Strip trailing whitespace per line to avoid pyyaml block-scalar formatting issues.
        text = "\n".join(line.rstrip(" ") for line in text.split("\n"))

        if self.engine.settings.convert_view_text_to_file:
            file_name = f"{self._normalise_name(row['name'])}.sql"
            self._dump_code(object_path / "sql" / file_name, text)

            return YamlIncludeStr(f"sql/{file_name}")

        return YamlLiteralStr(text)

    def _get_target_lag(self, target_lag):
        if not target_lag:
            return None

        if target_lag.get("type") == "DOWNSTREAM":
            return "DOWNSTREAM"

        seconds = int(target_lag["seconds"])

        for unit, size in self.target_lag_units:
            if seconds % size == 0:
                num = seconds // size
                return f"{num} {unit}" if num == 1 else f"{num} {unit}s"

        return f"{seconds} second{'s' if seconds != 1 else ''}"
