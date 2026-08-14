from snowddl.blueprint import AlertBlueprint, AccountObjectIdent, SchemaObjectIdent
from snowddl.parser.abc_parser import AbstractParser, ParsedFile


# fmt: off
alert_json_schema = {
    "type": "object",
    "properties": {
        "warehouse": {
            "type": "string"
        },
        "schedule": {
            "type": "string"
        },
        "condition": {
            "type": "string"
        },
        "action": {
            "type": "string"
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
        },
        "enabled": {
            "type": "boolean"
        },
    },
    "required": ["schedule", "condition", "action"],
    "additionalProperties": False,
}
# fmt: on


class AlertParser(AbstractParser):
    def load_blueprints(self):
        self.parse_schema_object_files("alert", alert_json_schema, self.process_alert)

    def process_alert(self, f: ParsedFile):
        bp = AlertBlueprint(
            full_name=SchemaObjectIdent(self.env_prefix, f.database, f.schema, f.name),
            warehouse=AccountObjectIdent(self.env_prefix, f.params["warehouse"]) if f.params.get("warehouse") else None,
            schedule=str(f.params["schedule"]).strip(),
            condition=self.normalise_sql_text_param(f.params["condition"]),
            action=self.normalise_sql_text_param(f.params["action"]),
            grants=f.params.get("grants"),
            comment=f.params.get("comment"),
            enabled=f.params.get("enabled", True),
        )

        self.config.add_blueprint(bp)
