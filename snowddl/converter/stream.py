# OIE fork patch (0.67.5-oie.1) — NEW converter.
# Upstream ships parser + resolver for STREAM but no converter. Emits `stream/*.yaml`
# with object_type / object_name / append_only / insert_only from SHOW STREAMS.
from snowddl.blueprint import ObjectType
from snowddl.converter.abc_converter import ConvertResult
from snowddl.converter.abc_schema_object_converter import AbstractSchemaObjectConverter
from snowddl.parser.stream import stream_json_schema


class StreamConverter(AbstractSchemaObjectConverter):
    # Reverse of StreamResolver.object_type_to_source_type_map. "Table" is ambiguous
    # (TABLE / EVENT_TABLE both emit "Table") — default to TABLE, matching the resolver's
    # own source-type comparison for the common case.
    source_type_to_object_type_map = {
        "External Table": ObjectType.EXTERNAL_TABLE,
        "Table": ObjectType.TABLE,
        "Stage": ObjectType.STAGE,
        "View": ObjectType.VIEW,
        "Dynamic Table": ObjectType.DYNAMIC_TABLE,
    }

    def get_object_type(self) -> ObjectType:
        return ObjectType.STREAM

    def get_existing_objects_in_schema(self, schema: dict):
        existing_objects = {}

        cur = self.engine.execute_meta(
            "SHOW STREAMS IN SCHEMA {database:i}.{schema:i}",
            {
                "database": schema["database"],
                "schema": schema["schema"],
            },
        )

        for r in cur:
            existing_objects[f"{r['database_name']}.{r['schema_name']}.{r['name']}"] = {
                "database": r["database_name"],
                "schema": r["schema_name"],
                "name": r["name"],
                "source_type": r["source_type"],
                "table_name": r["table_name"],
                "mode": r["mode"],
                "comment": r["comment"] if r["comment"] else None,
            }

        return existing_objects

    def dump_object(self, row):
        object_type = self.source_type_to_object_type_map.get(row["source_type"])

        if object_type is None:
            raise ValueError(f"Unsupported stream source_type [{row['source_type']}] for stream [{row['name']}]")

        mode = str(row["mode"])

        data = {
            "object_type": object_type.name.lower(),
            "object_name": self._normalise_name_with_prefix(row["table_name"]),
            "append_only": True if "APPEND_ONLY" in mode else None,
            "insert_only": True if "INSERT_ONLY" in mode else None,
            "comment": row["comment"],
        }

        object_path = (
            self.base_path / self._normalise_name_with_prefix(row["database"]) / self._normalise_name(row["schema"]) / "stream"
        )

        if data:
            self._dump_file(object_path / f"{self._normalise_name(row['name'])}.yaml", data, stream_json_schema)
            return ConvertResult.DUMP

        return ConvertResult.EMPTY
