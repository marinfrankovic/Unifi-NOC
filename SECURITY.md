# Security Policy

Report suspected vulnerabilities using [GitHub private vulnerability reporting](https://github.com/marinfrankovic/Unifi-NOC/security/advisories/new). Do not publish credentials, production screenshots, logs containing personal data, database dumps or exploitable details in a public issue. No response-time or support guarantee is offered.

Use maintained stable releases, least-privilege controller keys, restricted network access and trusted TLS. The API/collector images run as an unprivileged user with read-only roots; PostgreSQL and the API are not published to the host. Connection profiles are encrypted at rest using a persistent instance key. Encryption does not protect an already-compromised host, administrator account or backup containing both the database and key.

First-owner setup is sensitive: keep loopback binding or otherwise restrict access until the owner is created. The application does not claim MFA/SSO, compliance certification, a security audit or an enterprise support contract. Dependency and base-image vulnerabilities must also be assessed by operators.

Local publication performs secret-pattern and private-artifact checks. These checks cannot identify every possible secret or personal identifier. Review changes and enable GitHub secret scanning/push protection where available. Runtime configuration, encrypted profiles, notification tokens, TLS keys and backups never belong in the public repository.

See [NOTICE](NOTICE) for authorized-use and privacy responsibilities and [LICENSE](LICENSE) for noncommercial terms.