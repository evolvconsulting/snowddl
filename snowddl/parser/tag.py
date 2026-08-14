from snowddl.blueprint import TagBlueprint, Ident, SchemaObjectIdent, ObjectType, TagReference, build_schema_object_ident
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
tag_json_schema = {
    "type": "object",
    "properties": {
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string"
                    },
                    "object_name": {
                        "type": "string"
                    },
                    "column_name": {
                        "type": "string"
                    },
                    "tag_value": {
                        "type": "string"
                    }
                },
                "required": ["object_type", "object_name", "tag_value"],
                "additionalProperties": False
            },
            "minItems": 1
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


class TagParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files("tag", tag_json_schema, self.process_tag)

    def process_tag(self, f: ParsedFile):
        references = []

        for a in f.params.get("references", []):
            ref = TagReference(
                object_type=ObjectType[a["object_type"].upper()],
                object_name=build_schema_object_ident(self.env_prefix, a["object_name"], f.database, f.schema),
                column_name=Ident(a["column_name"]) if a.get("column_name") else None,
                tag_value=a["tag_value"],
            )

            references.append(ref)

        bp = TagBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            references=references,
            grants=f.params.get("grants"),
            comment=f.params.get("comment"),
        )

        self.config.add_blueprint(bp)
