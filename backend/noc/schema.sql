-- UniFi NOC schema (idempotent). TimescaleDB is required.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS sites (
    name        text PRIMARY KEY,          -- controller short name, e.g. demo0001
    site_id     text NOT NULL,             -- controller object id
    uuid        text,                      -- integration API id
    description text NOT NULL,             -- "Demo East"
    slug        text NOT NULL UNIQUE,      -- "am"
    timezone    text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Narrow time-series table: one row per (entity, metric, timestamp).
CREATE TABLE IF NOT EXISTS metrics (
    ts          timestamptz      NOT NULL,
    site        text             NOT NULL,
    entity_type text             NOT NULL,
    entity_id   text             NOT NULL,
    metric      text             NOT NULL,
    value       double precision NOT NULL
);
SELECT create_hypertable('metrics', by_range('ts', INTERVAL '6 hours'), if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS metrics_uq ON metrics (entity_id, metric, ts DESC);
CREATE INDEX IF NOT EXISTS metrics_site_type ON metrics (site, entity_type, metric, ts DESC);

-- Current inventory (latest state). raw is a SANITIZED subset of the controller record.
CREATE TABLE IF NOT EXISTS devices (
    mac          text PRIMARY KEY,
    site         text NOT NULL REFERENCES sites(name),
    name         text,
    device_type  text NOT NULL,            -- gateway | switch | ap | other
    model        text,
    model_name   text,
    version      text,
    ip           text,
    state        int,
    online       boolean NOT NULL DEFAULT false,
    adopted      boolean,
    uptime       bigint,
    last_seen    timestamptz,
    last_poll    timestamptz NOT NULL DEFAULT now(),
    uplink_mac   text,
    uplink_port  int,
    upgradable   boolean,
    upgrade_to   text,
    raw          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS devices_site ON devices (site, device_type);

CREATE TABLE IF NOT EXISTS clients (
    site         text NOT NULL REFERENCES sites(name),
    mac          text NOT NULL,
    name         text,
    hostname     text,
    ip           text,
    oui          text,
    is_wired     boolean,
    online       boolean NOT NULL DEFAULT false,
    network      text,
    vlan         int,
    essid        text,
    radio        text,
    channel      int,
    ap_mac       text,
    sw_mac       text,
    sw_port      int,
    signal       int,
    rssi         int,
    noise        int,
    first_seen   timestamptz,
    last_seen    timestamptz,
    assoc_time   timestamptz,
    uptime       bigint,
    dev_category int,
    raw          jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, mac)
);
CREATE INDEX IF NOT EXISTS clients_site ON clients (site, online);
CREATE INDEX IF NOT EXISTS clients_mac ON clients (mac);

CREATE TABLE IF NOT EXISTS events (
    id          text PRIMARY KEY,
    ts          timestamptz NOT NULL,
    site        text NOT NULL,
    severity    text NOT NULL,             -- critical | high | medium | low | info
    device      text,
    device_mac  text,
    device_type text,
    event_type  text NOT NULL,
    category    text,
    title       text,
    message     text NOT NULL,
    client      text,
    client_mac  text,
    source      text NOT NULL,             -- unifi | noc
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_site_ts ON events (site, ts DESC);
CREATE INDEX IF NOT EXISTS events_type ON events (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS events_fts ON events USING gin (to_tsvector('simple', coalesce(message,'') || ' ' || coalesce(device,'') || ' ' || coalesce(client,'')));

CREATE TABLE IF NOT EXISTS alert_rules (
    id           text PRIMARY KEY,
    name         text NOT NULL,
    entity_type  text NOT NULL,
    metric       text NOT NULL,
    op           text NOT NULL CHECK (op IN ('>', '<', '>=', '<=')),
    warn         double precision,
    crit         double precision,
    for_s        int NOT NULL DEFAULT 60,
    clear_for_s  int NOT NULL DEFAULT 60,
    hysteresis   double precision NOT NULL DEFAULT 0,
    enabled      boolean NOT NULL DEFAULT true,
    unit         text,
    description  text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL DEFAULT 'threshold',   -- threshold | anomaly | system
    rule_id      text,
    site         text NOT NULL,
    entity_type  text NOT NULL,
    entity_id    text NOT NULL,
    entity_name  text,
    metric       text NOT NULL,
    condition    text NOT NULL,
    threshold    double precision,
    value        double precision,
    peak_value   double precision,
    severity     text NOT NULL,                       -- warning | critical | info
    state        text NOT NULL,                       -- firing | resolved
    message      text NOT NULL,
    started_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    resolved_at  timestamptz,
    acked_by     text,
    acked_at     timestamptz,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS alerts_state ON alerts (state, started_at DESC);
CREATE INDEX IF NOT EXISTS alerts_site ON alerts (site, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_firing ON alerts (kind, coalesce(rule_id, ''), entity_id, metric) WHERE state = 'firing';

CREATE TABLE IF NOT EXISTS kv_state (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    username      text PRIMARY KEY,
    password_hash text NOT NULL,
    role          text NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'viewer')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login    timestamptz
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    username   text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    ip         text,
    user_agent text
);

CREATE TABLE IF NOT EXISTS auth_events (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    username   text,
    ip         text,
    event      text NOT NULL,     -- login_ok | login_failed | logout | rate_limited | session_expired
    user_agent text
);
CREATE INDEX IF NOT EXISTS auth_events_ip_ts ON auth_events (ip, ts DESC);
