from snowddl.blueprint import SequenceBlueprint, SchemaObjectIdent
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
sequence_json_schema = {
    "type": "object",
    "properties": {
        "start": {
            "type": "integer"
        },
        "interval": {
            "type": "integer"
        },
        "is_ordered": {
            "type": "boolean"
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
    "additionalProperties": False
}
# fmt: on


class SequenceParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files("sequence", sequence_json_schema, self.process_sequence)

    def process_sequence(self, f: ParsedFile):
        bp = SequenceBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            start=f.params.get("start", 1),
            interval=f.params.get("interval", 1),
            is_ordered=f.params.get("is_ordered"),
            grants=f.params.get("grants"),
            comment=f.params.get("comment"),
        )

        self.config.add_blueprint(bp)
