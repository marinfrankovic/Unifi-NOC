CREATE TABLE maintenance_windows (
    id bigserial PRIMARY KEY,
    site text NOT NULL REFERENCES sites(name),
    title text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    repeat text NOT NULL DEFAULT 'none' CHECK (repeat IN ('none','daily','weekly')),
    timezone text NOT NULL DEFAULT 'UTC',
    enabled boolean NOT NULL DEFAULT true,
    created_by text NOT NULL,
    CHECK (ends_at > starts_at)
);

CREATE TABLE notification_outbox (
    id bigserial PRIMARY KEY,
    source_key text NOT NULL UNIQUE,
    alert_id bigint REFERENCES alerts(id) ON DELETE SET NULL,
    site text NOT NULL,
    event_state text,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    last_error text
);

CREATE TABLE device_snapshots (
    device_mac text PRIMARY KEY,
    site text NOT NULL REFERENCES sites(name),
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE configuration_changes (
    id bigserial PRIMARY KEY,
    device_mac text NOT NULL,
    device_name text,
    site text NOT NULL REFERENCES sites(name),
    ts timestamptz NOT NULL DEFAULT now(),
    before jsonb NOT NULL,
    after jsonb NOT NULL
);
CREATE INDEX configuration_changes_site_ts ON configuration_changes (site, ts DESC);