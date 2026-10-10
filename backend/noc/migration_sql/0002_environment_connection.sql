ALTER TABLE connections ADD COLUMN config_source text UNIQUE;
ALTER TABLE sites ADD COLUMN selected boolean NOT NULL DEFAULT false;