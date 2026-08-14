from snowddl.blueprint import FileFormatBlueprint, SchemaObjectIdent
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
file_format_json_schema = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string"
        },
        "format_options": {
            "type": "object",
            "additionalProperties": {
                "type": ["array", "boolean", "number", "string"]
            }
        },
        "grants": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                # OIE patch (#10): minItems 0 -- an EMPTY list is the declaration that
                # carries the point, "no role holds this privilege", so it must be
                # expressible. Patch #8 used minItems 1 and could not say it.
                "minItems": 0
            }
        },
        "comment": {
            "type": "string"
        }
    },
    "required": ["type"],
    "additionalProperties": False
}
# fmt: on


class FileFormatParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files("file_format", file_format_json_schema, self.process_file_format)

    def process_file_format(self, f: ParsedFile):
        bp = FileFormatBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            type=f.params["type"].upper(),
            format_options={
                option_name.upper(): option_value for option_name, option_value in f.params.get("format_options", {}).items()
            },
            grants=f.params.get("grants"),
            comment=f.params.get("comment"),
        )

        self.config.add_blueprint(bp)
