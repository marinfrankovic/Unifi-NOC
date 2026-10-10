CREATE TABLE connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    controller_url text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    system_identity text,
    os_version text,
    network_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE sites ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX sites_internal_id ON sites (id);
ALTER TABLE sites ADD COLUMN connection_id uuid REFERENCES connections(id);
CREATE UNIQUE INDEX sites_connection_native_id ON sites (connection_id, site_id);

ALTER TABLE devices ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX devices_internal_id ON devices (id);