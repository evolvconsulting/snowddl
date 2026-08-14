from snowddl.blueprint import (
    ProjectionPolicyBlueprint,
    ProjectionPolicyReference,
    Ident,
    ObjectType,
    SchemaObjectIdent,
    build_schema_object_ident,
)
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
projection_policy_json_schema = {
    "type": "object",
    "properties": {
        "body": {
            "type": "string"
        },
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
                    "column": {
                        "type": "string"
                    }
                },
                "required": ["object_type", "object_name", "column"],
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
    "required": ["body"],
    "additionalProperties": False
}
# fmt: on


class ProjectionPolicyParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files("projection_policy", projection_policy_json_schema, self.process_projection_policy)

    def process_projection_policy(self, f: ParsedFile):
        references = []

        for a in f.params.get("references", []):
            ref = ProjectionPolicyReference(
                object_type=ObjectType[a["object_type"].upper()],
                object_name=build_schema_object_ident(self.env_prefix, a["object_name"], f.database, f.schema),
                column=Ident(a["column"]),
            )

            references.append(ref)

        bp = ProjectionPolicyBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            body=self.normalise_sql_text_param(f.params["body"]),
            references=references,
            grants=f.params.get("grants"),
            comment=f.params.get("comment"),
        )

        self.config.add_blueprint(bp)
