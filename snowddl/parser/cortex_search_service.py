from snowddl.blueprint import (
    AccountObjectIdent,
    CortexSearchServiceBlueprint,
    Ident,
    SchemaObjectIdent,
)
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
cortex_search_service_json_schema = {
    "type": "object",
    "properties": {
        "search_column": {
            "type": "string"
        },
        "vector_indexes": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "minItems": 1
        },
        "attributes": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "warehouse": {
            "type": "string"
        },
        "target_lag": {
            "type": "string"
        },
        "refresh_mode": {
            "type": "string"
        },
        "text": {
            "type": "string"
        },
        "comment": {
            "type": "string"
        },
    },
    "required": ["search_column", "warehouse", "target_lag", "text"],
    "additionalProperties": False
}
# fmt: on


class CortexSearchServiceParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files(
            "cortex_search_service", cortex_search_service_json_schema, self.process_cortex_search_service
        )

    def process_cortex_search_service(self, f: ParsedFile):
        bp = CortexSearchServiceBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            search_column=Ident(f.params["search_column"]),
            vector_indexes=[Ident(v) for v in f.params.get("vector_indexes", [])] if f.params.get("vector_indexes") else None,
            attributes=[Ident(a) for a in f.params.get("attributes", [])],
            warehouse=AccountObjectIdent(self.env_prefix, f.params["warehouse"]),
            target_lag=f.params["target_lag"],
            refresh_mode=f.params.get("refresh_mode").upper() if f.params.get("refresh_mode") else None,
            text=self.normalise_sql_text_param(f.params["text"]),
            comment=f.params.get("comment"),
        )

        self.config.add_blueprint(bp)
