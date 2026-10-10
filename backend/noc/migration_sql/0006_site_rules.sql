CREATE TABLE site_rule_overrides (
    site text NOT NULL REFERENCES sites(name),
    rule_id text NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    values jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, rule_id)
);