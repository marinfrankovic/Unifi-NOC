ALTER TABLE users ADD COLUMN is_owner boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN disabled boolean NOT NULL DEFAULT false;

UPDATE users SET is_owner=true
WHERE username=(SELECT username FROM users WHERE role='admin' ORDER BY created_at, username LIMIT 1);

CREATE TABLE user_site_roles (
    username text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('admin', 'reader')),
    granted_by text REFERENCES users(username) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (username, site_id)
);

INSERT INTO user_site_roles (username, site_id, role)
SELECT u.username, s.id, CASE WHEN u.role='admin' THEN 'admin' ELSE 'reader' END
FROM users u CROSS JOIN sites s WHERE NOT u.is_owner;

UPDATE users SET role='viewer' WHERE NOT is_owner;

CREATE TABLE admin_audit (
    id bigserial PRIMARY KEY,
    actor text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    site_id uuid REFERENCES sites(id) ON DELETE SET NULL,
    ts timestamptz NOT NULL DEFAULT now()
);