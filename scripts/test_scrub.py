#!/usr/bin/env python3
"""Tests for the Parsica Weave scrubber. Run: python test_scrub.py

Includes adversarial regression cases."""
import hashlib
import unicodedata

import scrub
import store

# invisible-character test vectors, built via chr() (never a literal non-ASCII byte in this file -
# the house rule for deliberate non-ASCII test vectors, and the Edit tool renders literal \u escapes
# as literal characters anyway, so chr() is the only way to keep this file itself plain ASCII).
_ZWSP = chr(0x200B)            # zero-width space - already Cf, pre-existing coverage baseline
_BRAILLE_BLANK = chr(0x2800)   # not DI, not Cf - explicit addition, pre-existing coverage baseline
_KHMER_AQ = chr(0x17B4)        # convergence pass 3 Q6: category Mn, NOT the old Cf check, NOT in
                                # the old 6-glyph hand-list - mutation-verified GENUINE new coverage
                                # (pre-fix: this exact splice made the anthropic-key pattern miss
                                # entirely, secret survived RAW, n=0)
_VS16 = chr(0xFE0F)            # emoji variation selector - category Mn, same class of genuine new
                                # coverage as the Khmer case, verified the same way

REDACT = [
    # --- known token shapes ---
    ("anthropic key", "key=sk-ant-api03-Abc123Def456Ghi789Jklmno", "sk-ant-api03-Abc123"),
    ("openai key", "use sk-proj-abcdefghij1234567890ABCDEF here", "sk-proj-abcdefghij"),
    ("openai classic", "token sk-abcdefghij1234567890ABCDEFGH", "sk-abcdefghij1234567890"),
    ("aws access key", "AKIAIOSFODNN7EXAMPLE in config", "AKIAIOSFODNN7EXAMPLE"),
    ("gcp api key", "AIzaSyA1234567890abcdefghijklmnopqrstuv done", "AIzaSyA1234567890abcdef"),
    ("github pat fine", "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz123456", "github_pat_11ABCDEFG0"),
    ("github token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "ghp_abcdefghijklmnop"),
    ("slack token", "xoxb-123456789012-abcdefghijklmnop", "xoxb-123456789012-abcd"),
    ("slack app token", "xapp-1-A01234ABCDE-1234567890123-abcdef0123456789", "xapp-1-A01234ABCDE"),
    ("stripe live", "sk_live_abcdefghijklmnop1234567890", "sk_live_abcdefghijklmnop"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEFghiJKL", "eyJhbGciOiJIUzI1NiJ9.eyJ"),
    ("slack webhook", "https://hooks.slack.com/services/T000/B000/XXXXabcdEFGH", "/services/T000/B000"),
    ("discord webhook", "https://discord.com/api/webhooks/123456789/abcDEF_ghiJKL-mno", "/webhooks/123456789/abc"),
    ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123", "abcdefghijklmnop"),
    ("pem block", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nDEF==\n-----END RSA PRIVATE KEY-----", "MIIabc"),
    # --- vendor tokens ---
    ("npm token", "export NPM_TOKEN=npm_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "npm_aBcDeFgHiJkL"),
    ("hf token", "hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789Ab", "hf_aBcDeFgHiJkL"),
    ("pypi token", "pypi-AgEIcHlwaS5vcmcabcdefghijklmnop", "pypi-AgEIcHlwaS5vcmc"),
    ("sendgrid", "SG.aBcDeFgHiJkLmNoPqRsTuV.aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg", "SG.aBcDeFgHiJkL"),
    ("digitalocean", "dop_v1_" + "a1b2c3d4" * 8, "dop_v1_a1b2c3d4"),
    # --- assignment recall ---
    ("password assign", 'password = "hunter2hunter2"', "hunter2hunter2"),
    ("api_key assign", "OPENAI_API_KEY=abcdef1234567890xyz", "abcdef1234567890xyz"),
    ("client secret", "client_secret: 9f8e7d6c5b4a39281706", "9f8e7d6c5b4a39281706"),
    ("access_token opaque", "access_token=7f3b9a2c4e6d8f0a1b3c5d7e9f1a3b5c7d9e1f3a", "7f3b9a2c4e6d8f0a"),
    ("api_token export", "export API_TOKEN=8f3a2b1c9d8e7f6a5b4c3d2e1f0a9b8c", "8f3a2b1c9d8e7f6a"),
    ("bot_token colon", "BOT_TOKEN: deadbeefcafebabe0123456789abcdef", "deadbeefcafebabe0123"),
    ("discord_token assign", "DISCORD_TOKEN=deadbeefcafebabe0123456789abcdef0011", "deadbeefcafebabe0123"),
    ("azure accountkey", "DefaultEndpointsProtocol=https;AccountName=mystore;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUz/K1SZ==;EndpointSuffix=core.windows.net", "Eby8vdM02xNOcqFlqUwJ"),
    ("json apikey", '{"apiKey": "7f3a9c2b1e8d4f6a0b5c9d2e1f8a3b7c"}', "7f3a9c2b1e8d4f6a"),
    ("json client_secret", '"client_secret": "GOCSPX-aBcDeFgHiJkLmNoPqRsTuVwX"', "GOCSPX-aBcDeFgHiJkL"),
    ("json password", '"password": "S3cretP@sswordValue"', "S3cretP@sswordValue"),
    # --- connection strings ---
    ("redis url empty user", "redis://:topsecretpw1@10.0.0.1:6379", "topsecretpw1"),
    ("postgres url", "postgres://admin:supersecret123@dbhost:5432/mydb", "supersecret123"),
    ("amqp url", "amqp://guest:guestpass123@rabbit:5672", "guestpass123"),
    ("redis url assign", "REDIS_URL=redis://:topsecretpw1@10.0.0.1:6379", "topsecretpw1"),
    ("https basic auth", "git clone https://user:supersecret@example.com/r.git", "user:supersecret@"),
    # --- auth headers ---
    ("auth basic", "Authorization: Basic dXNlcjpzM2NyZXRwYXNzd29yZGxvbmdlbm91Z2g=", "dXNlcjpzM2NyZXRw"),
    ("auth raw", "Authorization: 9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b", "9c8b7a6f5e4d3c2b"),
    ("auth token scheme", "Authorization: Token 9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f", "9c8b7a6f5e4d3c2b"),
    # --- structured credential formats ---
    ("stripe test key", "sk_test_abcdefghijklmnop1234567890", "abcdefghijklmnop1234"),
    ("stripe webhook secret", "whsec_abcdefghijklmnop1234567890", "abcdefghijklmnop1234"),
    ("gitlab pat", "glpat-1234567890abcdef1234567890", "1234567890abcdef"),
    ("gitlab oauth token", "gloas-abcdef1234567890abcdef1234", "abcdef1234567890"),
    ("aws presigned signature", "https://s3.amazonaws.com/b/k?X-Amz-Signature=abcdef1234567890abcdef1234567890abcd&X-Amz-Expires=3600", "abcdef1234567890abcdef"),
    ("generic url signature", "download at https://cdn.example.com/f?sig=abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnopqrst"),
    # --- bare value followed by trailing text (regression) ---
    ("bare value trailing text", "use password=hunter2Pass99 here", "hunter2Pass99"),
    ("bare value trailing &&", "export DB_SECRET=AbCdEf1234567890 && run", "AbCdEf1234567890"),
    # --- quoted code-shaped / long secret values ---
    ("quoted dotted secret", 'auth_token="v1.MnoPqRsTuVwXyZ012345"', "v1.MnoPqRsTuVwXyZ012345"),
    ("quoted paren secret", 'secret_key="a8F)kQ2zR7wL1pM4"', "a8F)kQ2zR7wL1pM4"),
    ("quoted gocspx dot", '"client_secret": "GOCSPX.aBcDeFgHiJkLmNoP"', "GOCSPX.aBcDeFgHiJkLmNoP"),
    ("quoted long >256", 'api_key="' + "A1" * 150 + '"', "A1" * 150),
    # --- more vendor tokens ---
    ("docker pat cmd", "docker login -u user -p dckr_pat_C8nZ2kQ9rT5wY1uX3vB7mN6pL0aS4dF8", "dckr_pat_C8nZ2kQ9"),
    ("gocspx bare", "GOCSPX-aBcDeFgHiJkLmNoPqRsTuVwX in config", "GOCSPX-aBcDeFgHiJkL"),
    ("glued underscore token", "VAR=_ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "ghp_abcdefghijklmnop"),
    # --- secret keys ending in _id / _header (Vault, GCP) ---
    ("vault secret_id", "secret_id=6a174c20-f6de-a53c-74d2-6018fcceff64", "6a174c20-f6de-a53c"),
    ("gcp private_key_id", '"private_key_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"', "a1b2c3d4e5f6a7b8"),
    ("apikey_header opaque", "apikey_header: 1234567890abcdef1234567890abcdef", "1234567890abcdef1234"),
    # --- URL/JDBC query-string secrets ---
    ("url query api_key", "https://api.example.com/v1/data?api_key=abcdef1234567890ABCDEF99&page=1", "abcdef1234567890ABCDEF99"),
    ("jdbc query password", "jdbc:postgresql://host/db?user=admin&password=Sup3rS3cretValue123", "Sup3rS3cretValue123"),
    # --- Telegram bot token (standalone + in-URL) ---
    ("telegram bare", "123456789:AAEhBOweik6ad6PsWpJp0pdsQTQ_Q0w8s8M", "AAEhBOweik6ad6PsWpJp"),
    ("telegram in url", "curl https://api.telegram.org/bot123456789:AAEhBOweik6ad6PsWpJp0pdsQTQ_Q0w8s8M/x", "AAEhBOweik6ad6PsWpJp"),
    # --- all-lowercase passphrase under an explicit secret key ---
    ("lowercase db_password", "export DB_PASSWORD=supersecretword", "supersecretword"),
    ("lowercase passphrase", "SSH_KEY_PASSPHRASE=correcthorsebatterystaple", "correcthorsebatterystaple"),
    ("lowercase password colon", "password: opensesameplease", "opensesameplease"),
    ("quoted lowercase passphrase", 'passphrase: "opensesameopensesame"', "opensesameopensesame"),
    ("quoted lowercase password", '"password": "letmeinplease"', "letmeinplease"),
    # --- PGP armored private key ---
    ("pgp private block", "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF8abcDEF0123456789==\n-----END PGP PRIVATE KEY BLOCK-----", "lQOYBF8abcDEF"),
    # --- long grouped opaque secret (config-enum bypass closed) ---
    ("long grouped secret", "api_key=aB1cD-eF2gH-iJ3kL-mN4oP-qR5sT-uV6wX", "iJ3kL-mN4oP"),
    # --- lowercase passphrase under an explicit key WITH trailing prose ---
    ("lowercase passphrase trailing prose", "use password=correcthorsebatterystaple here", "correcthorsebatterystaple"),
    ("lowercase password colon trailing prose", "password: correcthorsebatterystaple here", "correcthorsebatterystaple"),
    # --- lowercase-only Authorization tokens (scheme is the signal) ---
    ("auth bearer lowercase", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
    ("auth token lowercase", "Authorization: Token abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
    # --- auth_key / X-Auth-Key now recognized as a secret-bearing key ---
    ("auth_key assign", "auth_key=abcdef1234567890ABCDEF99", "abcdef1234567890"),
    ("x-auth-key header", "X-Auth-Key: 1234567890abcdef1234567890ABCDEF", "1234567890abcdef"),
    # --- prefix-shadowed inner assignment (header/cookie/curl/log) ---
    ("cookie shadowed token", "Cookie: session_token=9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a", "9d8c7b6a5f4e3d2c"),
    ("set-cookie shadowed", "Set-Cookie: session_token=9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a; HttpOnly", "9d8c7b6a5f4e3d2c"),
    ("curl -H shadowed api_key", "curl -H 'X-Custom: api_key=8f14e45fceea167a5a36dedd4bea2543' https://api", "8f14e45fceea167a"),
    ("log-prefix shadowed api_key", "DEBUG: api_key=8f14e45fceea167a5a36dedd4bea2543", "8f14e45fceea167a"),
    # --- all-lowercase opaque value under a *_key secret key ---
    ("lowercase secret_key hex", "secret_key=fadebabefadebabefadebabefadebabe", "fadebabefadebabe"),
    ("lowercase api_key passphrase", "api_key=correcthorsebatterystaplexyz", "correcthorsebattery"),
    ("lowercase auth_key", "auth_key=abcdefghijklmnopqrstuvwx", "abcdefghijklmnop"),
    # --- no-scheme Authorization opaque value (gateway/proxy log) ---
    ("auth no scheme lowercase", "Authorization: secrettokenvaluegoeshere", "secrettokenvaluegoes"),
    # --- SPACED prefix-shadow (whitespace around the inner sep) ---
    ("spaced shadow debug", "DEBUG: api_key = Xy9kQ2mPvR7sLtN4wB8c", "Xy9kQ2mPvR7sLtN4"),
    ("spaced shadow cookie", "Cookie: session_token = Xy9kQ2mPvR7sLtN4wB8c", "Xy9kQ2mPvR7sLtN4"),
    ("spaced shadow curl", "curl -H 'X: api_key = Xy9kQ2mPvR7sLtN4wB8c'", "Xy9kQ2mPvR7sLtN4"),
    # --- Ruby/Perl => and Go/Pascal := assignment operators ---
    ("hash-rocket api_key", '"api_key" => "AbCdEf0123456789GhIjKlMnOpQr"', "AbCdEf0123456789"),
    ("hash-rocket password", 'password => "AbCdEf0123456789GhIjKlMnOpQr"', "AbCdEf0123456789"),
    ("walrus client_secret", "client_secret := AbCdEf0123456789GhIjKlMnOpQr", "AbCdEf0123456789"),
    # --- compound-assignment operators store a secret literal ---
    ("memoized ||= secret", 'api_key ||= "aB3xK9mPqR7sT2vWxYz1"', "aB3xK9mPqR7sT2vWxYz1"),
    ("append += secret", 'password += "MyRealPassw0rdValue"', "MyRealPassw0rdValue"),
    ("nullish ??= secret", 'api_key ??= "aB3xK9mPqR7sT2vWxYz1"', "aB3xK9mPqR7sT2vWxYz1"),
    # --- a LIVE secret glued before an in-place marker is still redacted ---
    ("live secret before marker", "api_key=aB3xK9mPqR7sT2vWxYz1[REDACTED:secret]", "aB3xK9mPqR7sT2vWxYz1"),
    ("live secret before marker quoted", 'password = "MyRealPassw0rdValue[REDACTED:secret]"', "MyRealPassw0rdValue"),
    # --- a live secret glued AFTER a marker (concatenated secrets) ---
    ("after-marker aws secret", "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE.wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY99", "wJalrXUtnFEMIK7"),
    ("after-marker quoted aws", 'aws_secret_access_key="AKIAIOSFODNN7EXAMPLE.wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY99"', "wJalrXUtnFEMIK7"),
    ("after-marker glued plus", "api_key=AKIAIOSFODNN7EXAMPLE+aB3xK9mNp2qR5tY7wZ1cQ8", "aB3xK9mNp2qR5tY7"),
    # --- live secret glued to a marker via a value-TERMINATOR ---
    ("marker comma-glued secret", "password=AKIAIOSFODNN7EXAMPLE,wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMIK7"),
    ("marker semicolon-glued secret", "password=AKIAIOSFODNN7EXAMPLE;wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMIK7"),
    ("marker amp-glued secret", "api_key=AKIAIOSFODNN7EXAMPLE&aB3xK9mNp2qR5tY7wZ1cQ8", "aB3xK9mNp2qR5tY7"),
    ("marker close-paren-glued secret", "password=[REDACTED:a])wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMIK7"),
    # --- invisible-character splice evasion (convergence pass 3, Q6) ---
    ("zwsp-spliced key", "api_key=aB3xK9m" + _ZWSP + "PqR7sT2vWxYz1", "aB3xK9mPqR7sT2vWxYz1"),
    ("braille-spliced key", "api_key=aB3xK9m" + _BRAILLE_BLANK + "PqR7sT2vWxYz1", "aB3xK9mPqR7sT2vWxYz1"),
    ("khmer-spliced anthropic key", "sk-ant-api03-Abc123" + _KHMER_AQ + "Def456Ghi789Jklmno",
     "Abc123Def456Ghi789Jklmno"),
    ("vs16-spliced anthropic key", "sk-ant-api03-Abc123" + _VS16 + "Def456Ghi789Jklmno",
     "Abc123Def456Ghi789Jklmno"),
]

KEEP = [
    # (description, input, must-still-appear-substring) - MUST NOT be redacted
    # --- the operator's SHA-heavy workflow ---
    ("git sha short", "fixed in commit 4f49f39", "4f49f39"),
    ("git sha full", "HEAD is at d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708", "d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708"),
    ("version", "running version 4.8.0 build 26200", "4.8.0"),
    ("file path", "edited myapp/security/effects.py:180", "effects.py:180"),
    ("normal prose", "the password reset flow needs a test", "password reset flow"),
    ("placeholder angle", "set token=<your-token-here>", "<your-token-here>"),
    ("placeholder var", "export TOKEN=$MY_TOKEN", "$MY_TOKEN"),
    ("placeholder stars", "password = ********", "********"),
    ("hex hash", "sha256: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"),
    ("port number", "listening on 127.0.0.1:8080", "127.0.0.1:8080"),
    ("test id", "test_control_auth.py::test_verify", "test_control_auth.py"),
    # --- code that must NOT be gutted ---
    ("private_key code", "self.private_key = load_pem(path)", "load_pem(path)"),
    ("api_key attr", "api_key = settings.API_KEY", "settings.API_KEY"),
    ("access_key call", "self.access_key = compute_cache_key(self.inputs)", "compute_cache_key(self.inputs)"),
    ("private_key_path", "private_key_path = /home/op/.ssh/id_ed25519.pub", "/home/op/.ssh/id_ed25519.pub"),
    ("pwd getcwd", "pwd = os.getcwd()", "os.getcwd()"),
    ("passwd parse", "passwd_entry = parse_etc_passwd_line(line)", "parse_etc_passwd_line(line)"),
    ("secretKey call", "secretKey = generateDeterministicId(seed)", "generateDeterministicId(seed)"),
    ("session_token attr", "session_token = request.session.session_key", "request.session.session_key"),
    ("api_key_name quoted", "api_key_name = 'production-readonly'", "production-readonly"),
    ("apikey_header call", 'apikey_header = request.headers.get("X-Api-Key")', "request.headers.get"),
    ("auth_token_regex", 'auth_token_regex = re.compile(r"[A-Za-z0-9]+")', "re.compile"),
    ("cap_secret_grant call", "cap_secret_grant = mint(holder, scope)", "mint(holder, scope)"),
    ("cache_key ordinary", "cache_key = build_cache_key(req)", "build_cache_key(req)"),
    # --- prose after secret-word labels ---
    ("token prose camel", "token: nextPageToken returned by the paginated list endpoint", "nextPageToken"),
    ("password prose", "password: choose something memorable but unique to this site", "choose something"),
    ("secret prose snake", "secret = the_function_returns_early_on_empty_input", "the_function_returns_early_on_empty_input"),
    ("password pick short", "password: pick a strong one", "pick a strong one"),
    # --- conn-string negatives ---
    ("github https", "https://github.com/org/repo.git", "github.com/org/repo.git"),
    ("ssh scp url", "git@github.com:org/repo.git", "git@github.com"),
    ("localhost url", "http://localhost:3000/path", "localhost:3000/path"),
    # --- auth-header negatives ---
    ("auth prose", "Authorization: Required for this endpoint", "Required for this endpoint"),
    ("auth word", "Authorization: indistinguishable", "indistinguishable"),
    ("bearer prose snake", "Bearer authentication_is_required_here_now", "authentication_is_required"),
    ("auth bearer required prose", "Authorization: Bearer required", "Bearer required"),
    ("auth token prose long", "Authorization: Token requiredforendpoint", "requiredforendpoint"),
    # --- quoted config-enum values under secret keys (precision) ---
    ("quoted cipher enum", '"encryption_key": "AES-256-GCM"', "AES-256-GCM"),
    ("quoted region", 'secret_key: "us-east-1"', "us-east-1"),
    ("quoted version", 'client_secret = "v1.2.3-beta"', "v1.2.3-beta"),
    ("quoted not-configured", 'client_secret: "not-configured-yet"', "not-configured-yet"),
    # --- precision tightening ---
    ("camelcase identifier", "auth_token = AuthTokenProvider", "AuthTokenProvider"),
    ("pascal secret code", "secret = CapabilityGrant", "CapabilityGrant"),
    ("conn host port host", "redis://cache:6379@primary", "cache:6379@primary"),
    ("token_url reference", "token_url: https://auth.example.com/oauth/token", "https://auth.example.com/oauth/token"),
    ("secret_version ref", "secret_version = build-2026-06-27-rc1", "build-2026-06-27-rc1"),
    # --- precision under explicit keys ---
    ("camelcase under token key", "auth_token = AuthTokenProvider", "AuthTokenProvider"),
    ("snake phrase under secret", "secret = the_function_returns_early_on_empty_input", "the_function_returns_early_on_empty_input"),
    ("vault kv path", "vault_secret = secret/data/prod/db", "secret/data/prod/db"),
    ("pgp public block kept", "-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQENBF8abcDEF0123456789==\n-----END PGP PUBLIC KEY BLOCK-----", "mQENBF8abcDEF"),
    ("cipher under secret key", "secret_cipher = TLS_AES_128_GCM_SHA256", "TLS_AES_128_GCM_SHA256"),
    # --- precision boundary on the lowercase-passphrase widening ---
    # an 11-char lowercase word + trailing prose under an explicit key is prose, not a secret
    ("password 11-char word trailing", "password: complicated stuff to recall", "complicated"),
    ("auth scheme short prose", "Authorization: Bearer shortish", "shortish"),
    # --- prose words after a literal password/secret key survive ---
    ("password prose yesterday", "password: yesterday", "yesterday"),
    ("password prose regenerated", "password: regenerated", "regenerated"),
    ("secret prose configuration", "secret: configuration was the issue", "configuration"),
    ("secret prose authentication", "secret: authentication is broken now", "authentication"),
    # --- *_key explicit must not gut a short lowercase config word ---
    ("api_key short config word", "api_key = production", "production"),
    # --- a non-secret prefix + inner non-secret assignment survives ---
    ("log non-secret inner assign", "log: env=prod port=8080", "port=8080"),
    # --- long lowercase snake/hyphen identifier after Authorization: survives ---
    ("auth snake ident", "We pass Authorization: x_internal_request_id downstream.", "x_internal_request_id"),
    ("auth hyphen ident", "The Authorization: middleware-request-validator rejected it.", "middleware-request-validator"),
    # --- a comparison operator (==) is not an assignment - no false redaction ---
    ("comparison == not assign", "if count == 1000000000 then halt", "1000000000"),
    # --- a marker beside a git SHA / long word under a NON-secret key survives ---
    ("marker beside sha non-secret key", 'commit_note = "[REDACTED] 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"', "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"),
    ("marker beside word non-secret key", 'log_line = "value [REDACTED] internationalization"', "internationalization"),
    # --- marker + terminator must NOT absorb a following key=value / enum / snake / path ---
    ("marker semicolon then keyvalue", "conn=[REDACTED:secret];Host=database_primary", "Host=database_primary"),
    ("marker comma then enum", "data=[REDACTED:x],us-east-1", "us-east-1"),
    ("marker comma then snake", "ref=[REDACTED:x],the_function_name_here", "the_function_name_here"),
    ("marker comma then path", "p=[REDACTED:x],/etc/app/config/store.enc", "/etc/app/config/store.enc"),
]

PII = [
    ("email", "ping me at alex@example.com", "[EMAIL]", "alex@example.com"),
    ("ssn", "ssn 123-45-6789 on file", "[SSN]", "123-45-6789"),
    ("phone", "call +1 415-555-0142 today", "[PHONE]", "415-555-0142"),
    # SEPARATOR COVERAGE. The patterns used to accept only SPACE and HYPHEN between digit groups,
    # so a Luhn-valid PAN written with any other common separator reached DISK unredacted - not a
    # theoretical gap: reproduced end to end through the `note` verb against the publication
    # archive. A card number is written however the source wrote it, and the scrub is the only
    # write-time gate, so the separator class is the whole fence here.
    ("card dot",        "card 4111.1111.1111.1111 on file", "[CARD]", "4111.1111.1111.1111"),
    ("card underscore", "card 4111_1111_1111_1111 on file", "[CARD]", "4111_1111_1111_1111"),
    ("card colon",      "card 4111:1111:1111:1111 on file", "[CARD]", "4111:1111:1111:1111"),
    ("card space",      "card 4111 1111 1111 1111 on file", "[CARD]", "4111 1111 1111 1111"),
    ("card hyphen",     "card 4111-1111-1111-1111 on file", "[CARD]", "4111-1111-1111-1111"),
    ("card bare",       "card 4111111111111111 on file",    "[CARD]", "4111111111111111"),
    ("ssn dot",         "ssn 123.45.6789 on file",          "[SSN]",  "123.45.6789"),
    ("ssn space",       "ssn 123 45 6789 on file",          "[SSN]",  "123 45 6789"),
    # CUE-GATED BARE FORM. Nine unseparated digits is how a database, a CSV export or a form field
    # writes an SSN, and the separated pattern cannot see it. Redacting every 9-digit run to reach
    # it destroys 14/15 ordinary developer strings (measured), so the bare form is admitted only
    # when the author's own label supplies the evidence the digits do not.
    ("ssn cued bare",       "ssn 123456789 on file",              "[SSN]", "123456789"),
    ("ssn cued colon",      "SSN: 123456789",                     "[SSN]", "123456789"),
    ("ssn cued equals",     "ssn=123456789",                      "[SSN]", "123456789"),
    ("ssn cued phrase",     "social security number 123456789",   "[SSN]", "123456789"),
    ("ssn cued abbrev",     "Social Security No. 078051120",      "[SSN]", "078051120"),
    ("ssn cued snake",      "social_security_number 123456789",   "[SSN]", "123456789"),
    ("ssn cued plural",     "SSNs: 123456789",                    "[SSN]", "123456789"),
    ("ssn cued midphrase",  "employee ssn 123456789 verified",    "[SSN]", "123456789"),
    # END-OF-SENTENCE (#4). A trailing period is not a decimal, so the SSN before it still redacts.
    ("ssn cued end period", "employee ssn 123456789.",            "[SSN]", "123456789"),
]

PII_KEEP = [
    ("date not phone", "the 2026-06-27 release", "2026-06-27"),
    ("version not phone", "v1.2.3.4 shipped", "1.2.3.4"),
    # FALSE-POSITIVE COMPANIONS for the widened separator classes. The card widening is free - the
    # Luhn checksum gates every match, so a wider class cannot redact a non-card. The SSN has NO
    # checksum, so its widening is bounded by lookarounds instead: a 3-2-4 run already embedded in
    # a longer dotted sequence is not an SSN. Without those lookarounds every vector below starts
    # being redacted, which is why they are pinned here rather than left to judgement.
    ("ipv4 not ssn",         "gateway 192.168.1.1 is up",        "192.168.1.1"),
    ("long dotted not ssn",  "matrix 123.45.6789.0 address",     "123.45.6789.0"),
    ("dotted ip not ssn",    "host 10.123.45.6789 responded",    "10.123.45.6789"),
    ("oid not ssn",          "oid 1.3.6.1.4.1 registered",       "1.3.6.1.4.1"),
    ("build not ssn",        "build 20260719.1 shipped",         "20260719.1"),
    # The SSN separator class is deliberately NARROWER than the card's: card is Luhn-gated so a wide
    # class is free, SSN has no checksum so every separator added is a false-positive risk paid in
    # full. These two pin that asymmetry - widening the SSN class to \D (any non-digit) redacts both.
    ("snake ident not ssn",  "job_123_45_6789 finished",         "job_123_45_6789"),
    ("dimensions not ssn",   "size 192x64x1024 buffer",          "192x64x1024"),
    # The Luhn gate is what stops a digit run reading as a card, so this companion has to be a run
    # that FAILS the checksum - otherwise it is asserting the checksum does not work.
    # ACCEPTED RESIDUAL: a 13-19 digit run that happens to PASS Luhn is redacted regardless of
    # intent - "1000 2000 3000 4000" and "1111 2222 3333 4444" both do. That is roughly 1 run in 10,
    # it predates the separator widening, and the scrub cannot tell such a run from a PAN. Erring
    # toward redaction is the correct direction for an always-on privacy gate.
    ("numeric run not card", "counts 1234 5678 9012 3456 7890",  "1234 5678 9012 3456 7890"),
    ("did not card",         "id 123456789012 minted",           "123456789012"),
    ("ports not card",       "ports 8080 9090 3000 5000 7000",   "8080 9090 3000 5000 7000"),
    # COMPANIONS FOR THE CUE GATE. These are the reason the bare form is cue-gated rather than
    # matched outright: every one is a 9-digit run a developer memory store legitimately holds, and
    # an always-on, not-optional scrub that ate them would be destroying content, not protecting it.
    # The last two matter most - they contain the cue WORD but no SSN, so a gate that keyed on the
    # word alone, or that let the cue reach across arbitrary distance, would redact them.
    ("commit not ssn",       "commit 123456789 in the log",      "123456789"),
    ("pid not ssn",          "pid 123456789 exited",             "123456789"),
    ("bytes not ssn",        "the file is 123456789 bytes",      "123456789"),
    ("runid not ssn",        "run_id 483920184 completed",       "483920184"),
    ("checksum not ssn",     "checksum 987654321 verified",      "987654321"),
    ("rowcount not ssn",     "row count 123456789 after import", "123456789"),
    ("epoch not ssn",        "epoch 175294837 rounded",          "175294837"),
    ("cue without digits",   "ssn policy doc revision 4",        "ssn policy doc revision 4"),
    ("cue far from digits",  "order 123456789 shipped to ssn-free address", "123456789"),
    # These two pin the two BOUNDS that make the cue gate safe, and each was added because a
    # deliberately over-strict mutation of the gate survived the suite without them:
    #   - the cue's REACH. Widen it and an unrelated run later in the same sentence is redacted
    #     because the word "ssn" appeared earlier. This is the likeliest real-world over-redaction.
    #   - the DIGIT BOUNDARY. Drop it and a longer run adjacent to a cue has its first nine digits
    #     eaten, silently corrupting an id into "[SSN]" plus a leftover tail.
    ("cue reach bounded",    "ssn policy updated, see commit 123456789 for the change",
                             "123456789"),
    ("longer run near cue",  "ssn=1234567890",                   "1234567890"),
    # #4 DECIMAL BOUNDARY + #3-REVERT PINS. The decimal must stay intact, and the compound-cue
    # widening an adversarial pass proved unsafe must not creep back: "ssn" is overloaded (SubSystem
    # / Sequence Number), so a `*_ssn` field and an opaque token containing "ssn" are NOT Social
    # Security Numbers. Un-anchor the cue from \b again and the last two fail.
    ("cued decimal not ssn", "ssn 123456789.42 logged",          "123456789.42"),
    ("compound ssn field",   "local_ssn=123456789 addressed",    "123456789"),
    ("ssn inside token",     "SSN123456789ab serial",            "SSN123456789ab"),
]


def run():
    fails = []

    for desc, inp, banned in REDACT:
        out, n = scrub.scrub(inp)
        if banned in out:
            fails.append(f"REDACT[{desc}] secret survived: {banned!r} still in {out!r}")
        elif n == 0:
            fails.append(f"REDACT[{desc}] no redaction counted for {inp!r}")

    for desc, inp, must in KEEP:
        out, n = scrub.scrub(inp, scrub_pii=False)
        if must not in out:
            fails.append(f"KEEP[{desc}] over-redacted: {must!r} gone from {out!r}")

    for desc, inp, token, banned in PII:
        out, n = scrub.scrub(inp, scrub_pii=True)
        if banned in out:
            fails.append(f"PII[{desc}] survived: {banned!r} in {out!r}")
        if token not in out:
            fails.append(f"PII[{desc}] wrong token, got {out!r}")
        out2, _ = scrub.scrub(inp, scrub_pii=False)
        if banned not in out2:
            fails.append(f"PII[{desc}] redacted even with scrub_pii=False: {out2!r}")

    for desc, inp, must in PII_KEEP:
        out, n = scrub.scrub(inp, scrub_pii=True)
        if must not in out:
            fails.append(f"PII_KEEP[{desc}] over-redacted: {must!r} gone from {out!r}")

    # an already-redacted inner value must NOT be re-mangled into
    # '[REDACTED:secret]]' (stray bracket) or double-counted by the bare/shadow pass.
    for desc, inp in (("shadow remangle", "config: api_key = api_token=anotherSecret456"),
                      ("bare remangle", "api_key=prefix_sk-ant-abcdefghij1234567890klmnop_x"),
                      ("live before marker", "api_key=aB3xK9mPqR7sT2vWxYz1[REDACTED:secret]")):
        out, n = scrub.scrub(inp, scrub_pii=False)
        if "]]" in out:
            fails.append(f"MANGLE[{desc}] stray bracket in {out!r}")
        if n != 1:
            fails.append(f"MANGLE[{desc}] expected n=1, got n={n} for {inp!r}")

    # --- SAFETY INVARIANT (convergence pass 3, Q6): INVISIBLE_CODE_POINTS must never touch a
    # combining mark that carries real meaning. Enumerated, never `category(ch) == "Mn"` - verified
    # here against real accented Latin and Devanagari text: content-preserving (NFKC MAY canonically
    # compose a decomposed accent - "e"+U+0301 -> the precomposed "e-acute" - pre-existing scrub.py
    # behavior, unrelated to this fix; the safety property is that the accent's MEANING survives via
    # composition, never that it is byte-identical to a decomposed input). ---
    _cafe = "cafe" + chr(0x0301)                        # combining acute accent (Mn, NOT in the set)
    _devanagari = "".join(chr(c) for c in
                          (0x0928, 0x092E, 0x0938, 0x094D, 0x0924, 0x0947))  # "namaste": virama +
                                                                             # vowel sign are Mn too
    _grinning = chr(0x1F600)                            # emoji with its OWN code point, no selector
    for desc, text in (("cafe + combining acute", _cafe),
                       ("devanagari with virama/vowel-sign", _devanagari),
                       ("emoji with own code point", _grinning)):
        out, n = scrub.scrub(text, scrub_pii=False)
        expected = unicodedata.normalize("NFKC", text)
        if out != expected:
            fails.append(f"SAFETY[{desc}] touched meaningful text: {text!r} -> {out!r}")
        if n != 0:
            fails.append(f"SAFETY[{desc}] counted a redaction on clean text: n={n}")

    # accepted cost: a color-presentation emoji (base + VS16) loses ONLY the selector, never the base.
    _heart_base = chr(0x2764)
    heart_out, _ = scrub.scrub(_heart_base + chr(0xFE0F), scrub_pii=False)
    if _heart_base not in heart_out:
        fails.append(f"SAFETY[vs16 emoji] base glyph lost: {heart_out!r}")
    if chr(0xFE0F) in heart_out:
        fails.append("SAFETY[vs16 emoji] selector was NOT stripped (accepted-cost regression)")

    # category audit: no code point actually present in real accented/Indic sample text may be a
    # member of INVISIBLE_CODE_POINTS - the enumerated set may legitimately include a handful of
    # Mn-category code points (variation selectors, Khmer inherent vowels) that Unicode itself has
    # vetted as default-ignorable, but none of THOSE may coincide with ordinary combining marks.
    _sample_cps = {ord(c) for c in _cafe + _devanagari}
    overlap = _sample_cps & scrub.INVISIBLE_CODE_POINTS
    if overlap:
        fails.append(f"SAFETY[category audit] INVISIBLE_CODE_POINTS overlaps real text: {overlap!r}")

    # cross-module drift check (the actual Q6 finding): store.py's label policy and scrub.py's
    # scrubber must agree on the Khmer inherent vowel U+17B4 - the exact code point that was
    # REJECTED in a channel name but NOT stripped from ordinary content before this fix, a hand-list
    # rotting in production. Both routes now share the ONE set, so they can no longer drift apart.
    _khmer_name = "proj" + chr(0x17B4) + "name"
    if store.valid_channel_name(_khmer_name):
        fails.append("DRIFT[store] a Khmer-inherent-vowel-bearing channel name was accepted")
    stripped, _ = scrub.scrub(_khmer_name, scrub_pii=False)
    if chr(0x17B4) in stripped:
        fails.append("DRIFT[scrub] the Khmer inherent vowel was not stripped from content")

    # PARITY FINGERPRINT (convergence pass 3, Q6): this repo and parsiclaude/src/parsica_spirit/
    # enrich/scrub.py each own a SEPARATE copy of the same canonical Default_Ignorable_Code_Point
    # set (two git repos, no shared package). This digest must match the companion assertion in
    # parsiclaude/tests/test_scrub.py - if either set ever drifts, ITS OWN repo's test catches it
    # (self-consistency); keeping the two hardcoded digests in sync across repos on a DELIBERATE,
    # coordinated change is a human/reviewer responsibility this comment exists to flag.
    _expected_digest = "4ffed20e4c8ff49b607ae98fe21d17cb067e820e7976610cd2195078bf165945"
    digest = hashlib.sha256(repr(sorted(scrub.INVISIBLE_CODE_POINTS)).encode("ascii")).hexdigest()
    if digest != _expected_digest:
        fails.append(f"PARITY[fingerprint] INVISIBLE_CODE_POINTS changed - got {digest}, "
                     f"expected {_expected_digest} (update BOTH this and the Spirit companion test)")

    # --- RC5-L rider: the refusal fence is MARKER-AWARE at both entry points -------------------
    # The property under test is IDEMPOTENCE ON MARKED TEXT: scrub(scrub(x)) == scrub(x). Stated
    # that way deliberately, because non-idempotence IS the bug. Refusal matching is substring +
    # case-insensitive, so a term that is a substring of a marker word rewrote the marker an earlier
    # pass emitted - `red` turning `<redacted>` into `<<redacted>acted>`, `mail` turning `[EMAIL]`
    # into `[E<redacted>]` - and query.py's _s() re-scrubs mirror content on EVERY retrieval
    # display, so the corruption compounded once per view. Asserting the PROPERTY rather than a
    # fixed output keeps this test alive through a refactor of the walker's internals.
    _MARKED = [
        # (description, text carrying a GENUINE marker, refusal term COLLIDING with that marker)
        ("refusal marker", "a <redacted> here and a red flag",         "red",  "<redacted>"),
        ("email marker",   "contact [EMAIL] about the mail thread",    "mail", "[EMAIL]"),
        ("ssn marker",     "on file [SSN] - ssn requested",            "ssn",  "[SSN]"),
        ("card marker",    "paid [CARD] - card on file",               "card", "[CARD]"),
        ("phone marker",   "call [PHONE] - phone it in",               "phone", "[PHONE]"),
        ("secret label",   "key [REDACTED:secret] is a secret",        "secret", "[REDACTED:secret]"),
        ("token label",    "[REDACTED:aws_access_key] access granted", "access",
         "[REDACTED:aws_access_key]"),
    ]
    for desc, text, term, marker in _MARKED:
        p1, _ = scrub.scrub(text, refusal=[term])
        p2, _ = scrub.scrub(p1, refusal=[term])
        p3, _ = scrub.scrub(p2, refusal=[term])
        if p1 != p2:
            fails.append(f"MARKER[{desc}] scrub not idempotent: pass1={p1!r} pass2={p2!r}")
        if p2 != p3:
            fails.append(f"MARKER[{desc}] scrub diverges by pass 3: pass2={p2!r} pass3={p3!r}")
        # the marker must survive VERBATIM - not merely be stable in an already-corrupted form
        if marker not in p1:
            fails.append(f"MARKER[{desc}] genuine {marker!r} corrupted on pass 1: {p1!r}")
        if marker not in p3:
            fails.append(f"MARKER[{desc}] genuine {marker!r} corrupted by pass 3: {p3!r}")
        # the DISPLAY path (store.fence_output -> apply_refusal), which query.py runs per view
        d1, _ = scrub.apply_refusal(text, [term])
        d2, _ = scrub.apply_refusal(d1, [term])
        d3, _ = scrub.apply_refusal(d2, [term])
        if not (d1 == d2 == d3):
            fails.append(f"MARKER[{desc}] apply_refusal not idempotent: {d1!r}/{d2!r}/{d3!r}")
        if marker not in d3:
            fails.append(f"MARKER[{desc}] apply_refusal corrupted {marker!r} by pass 3: {d3!r}")

    # Single-pass integrity: scrub()'s refusal pass runs AFTER its own secret/PII passes, so the
    # markers it must protect are ones THIS SAME CALL just emitted. Not only a re-entry bug.
    _one_pass, _ = scrub.scrub("mail: nobody@example.com", refusal=["mail"])
    if "[EMAIL]" not in _one_pass:
        fails.append(f"MARKER[single pass] scrub corrupted its OWN [EMAIL] marker: {_one_pass!r}")
    if scrub.REFUSAL_MARKER not in _one_pass:
        fails.append(f"MARKER[single pass] term outside the marker was not fenced: {_one_pass!r}")

    # FALSE-POSITIVE COMPANION: marker-awareness must not become a bypass. Marker-SHAPED text is
    # not a marker - an untrusted legacy filename or a pasted body can forge "[REDACTED:xTERMx]"
    # around a live refusal term, and is_genuine_marker's SECRET_LABELS check tells the two apart.
    _FORGED = [
        ("unknown label",   "[REDACTED:xCODENAMEx]", "CODENAME"),
        ("bare label",      "[REDACTED:CODENAME]",   "CODENAME"),
        ("uppercase label", "[REDACTED:SECRET]",     "SECRET"),   # labels are lowercase by _mask()
        # NOT a discriminator for is_genuine_marker: MARKER_RE does not match "[EMAILX]" at all, so
        # this is fenced as plain content either way. It is kept because it pins MARKER_RE's
        # TIGHTNESS - loosening the pattern to something like r"\[[A-Z]+\]" would start trusting
        # near-miss shapes, and this vector is the only thing that would notice.
        ("marker-ish word", "[EMAILX] here",         "EMAILX"),
    ]
    for desc, forged, term in _FORGED:
        out, _ = scrub.apply_refusal(forged, [term])
        if term in out:
            fails.append(f"MARKER[forged {desc}] apply_refusal smuggled {term!r} through: {out!r}")
        s_out, _ = scrub.scrub(forged, refusal=[term])
        if term in s_out:
            fails.append(f"MARKER[forged {desc}] scrub() let {term!r} ride through: {s_out!r}")
    # ...and a genuine marker in the SAME string still survives, so the two are truly discriminated
    _mixed, _ = scrub.apply_refusal("[REDACTED:xSECRETNAMEx] vs [REDACTED:secret]", ["SECRETNAME"])
    if "SECRETNAME" in _mixed or "[REDACTED:secret]" not in _mixed:
        fails.append(f"MARKER[forged mixed] forged/genuine not discriminated: {_mixed!r}")

    # NESTING: a forged marker must not SWALLOW a genuine one. Every vector above puts at most one
    # bracket family in the string, which is precisely the case where the swallow cannot happen -
    # so the whole block above passed while MARKER_RE's label class still permitted `[`, letting a
    # literal "[REDACTED:" anywhere earlier on the line run through a nested genuine marker to the
    # first `]`. is_genuine_marker then judged the WIDE span forged and handed the real marker
    # inside it to the refusal sub. Each vector here therefore needs BOTH bracket families present
    # and the genuine one nested inside the forged one's reach.
    # Each vector carries the genuine marker ALREADY MINTED (so it is valid through apply_refusal,
    # which is a pure refusal pass and mints nothing) and a second, bare occurrence of the term
    # OUTSIDE any marker, so the false-positive companion below has something it must still fence.
    _NESTED = [
        # (description, text, refusal term, genuine marker that must survive)
        ("minted marker inside forged span",
         "note [REDACTED: from [EMAIL] about it] - mail thread", "mail", "[EMAIL]"),
        ("genuine label inside forged span",
         "log [REDACTED: rotated [REDACTED:secret] today] - secret rotated",
         "secret", "[REDACTED:secret]"),
        ("unclosed forged prefix then genuine",
         "[REDACTED: call [PHONE] now - phone it in", "phone", "[PHONE]"),
        ("two forged prefixes straddling a genuine",
         "[REDACTED:a [REDACTED:b [CARD] - card on file", "card", "[CARD]"),
        ("forged span AFTER the genuine marker",
         "[SSN] on file [REDACTED: requested] - ssn pending", "ssn", "[SSN]"),
    ]
    for desc, text, term, marker in _NESTED:
        for label, fn in (("scrub", lambda t: scrub.scrub(t, refusal=[term])),
                          ("apply_refusal", lambda t: scrub.apply_refusal(t, [term]))):
            n1, _ = fn(text)
            n2, _ = fn(n1)
            n3, _ = fn(n2)
            if marker not in n1:
                fails.append(
                    f"MARKER[nested {desc}] {label} corrupted genuine {marker!r} on pass 1: {n1!r}")
            if not (n1 == n2 == n3):
                # the swallow ALSO breaks idempotence outright: pass 1 shrinks the span back under
                # the {1,64} bound, so pass 2 swallows what pass 1 protected.
                fails.append(
                    f"MARKER[nested {desc}] {label} not idempotent: {n1!r}/{n2!r}/{n3!r}")
        # ...and the forged wrapper is still ordinary content: the term OUTSIDE the genuine marker
        # must be fenced, so narrowing the pattern did not turn a forged span into a safe harbor.
        f_out, _ = scrub.apply_refusal(text, [term])
        if scrub.REFUSAL_MARKER not in f_out:
            fails.append(
                f"MARKER[nested {desc}] forged wrapper became a bypass - nothing fenced: {f_out!r}")

    # The sharpest form of the same defect, and the one that reaches the durable mirror chunk: the
    # swallowed marker is one THIS CALL mints. A legacy body carrying a literal "[REDACTED:" needs
    # no marker of its own - scrub()'s PII pass supplies the victim, and the refusal pass that runs
    # after it corrupts what it just emitted. scrub-only: apply_refusal mints nothing.
    _nest1, _ = scrub.scrub(
        "note [REDACTED: from someone@example.com about it] - mail thread", refusal=["mail"])
    if "[EMAIL]" not in _nest1:
        fails.append(f"MARKER[nested same-call] scrub corrupted the [EMAIL] it just minted, inside "
                     f"a forged span: {_nest1!r}")
    if scrub.REFUSAL_MARKER not in _nest1:
        fails.append(f"MARKER[nested same-call] term outside the forged span not fenced: {_nest1!r}")

    # LONGEST-FIRST: a phrase must beat a token it contains. _compile_refusal orders the alternation
    # by length descending, and until now nothing tested it in either repo. With shortest-first,
    # 'Pro' matches at position 0 and the scan resumes AFTER it, stranding 'jectX' - a fragment of
    # the codename - in the output; every other assertion here still passes on that, because neither
    # full term remains. Expectation DERIVED, never hand-written: 'Pro' occurs in this body only
    # inside 'ProjectX', so fencing with both terms must equal fencing with the long one alone.
    _lf_long, _ = scrub.apply_refusal("ProjectX ships", ["ProjectX"])
    _lf_both, _ = scrub.apply_refusal("ProjectX ships", ["Pro", "ProjectX"])
    if _lf_both != _lf_long:
        fails.append(f"REFUSAL[longest-first] a token beat the phrase containing it, stranding a "
                     f"fragment: both-terms={_lf_both!r} long-term-only={_lf_long!r}")

    # CROSS-REPO PARITY: a promoted body is Weave-minted text that Spirit's promote path re-fences,
    # so Spirit must recognize exactly the markers Weave can emit. Both repos DERIVE their label set
    # from their own _TOKEN_PATTERNS (never a hand-list), and this digest pins the derived results
    # equal. Same cross-repo human responsibility as the INVISIBLE_CODE_POINTS fingerprint above.
    _vocab_expected = "c4f010518df06d9bffecf4d4364afbee48b88505db664cd83f5bfea3dc2a26a1"
    _canon = "|".join([scrub.REFUSAL_MARKER, *scrub.PII_MARKERS, scrub.MARKER_RE.pattern,
                       *sorted(scrub.SECRET_LABELS)])
    _vocab_got = hashlib.sha256(_canon.encode("ascii")).hexdigest()
    if _vocab_got != _vocab_expected:
        fails.append(f"PARITY[marker vocabulary] changed - got {_vocab_got}, expected "
                     f"{_vocab_expected} (update BOTH this and the Spirit companion test)")

    # CROSS-REPO PII PARITY. Spirit re-scrubs promoted Weave text, so the two PII fences are one
    # contract: if they drift, the pair catches strictly less than either claims. Nothing gated that
    # before - the marker-vocabulary digest above covers MARKERS, not the PII patterns, so the
    # separator classes could have diverged silently in either direction.
    # The digest is over BEHAVIOUR (input -> scrubbed output), not over pattern strings, for two
    # reasons: the two modules legitimately differ in their secret-detection families so a
    # whole-module pattern digest would be noise, and behaviour parity survives an equivalent
    # respelling while still failing on real drift. PROBE and the expected digest must stay
    # byte-identical to the companion block in parsica-spirit's tests/test_scrub.py.
    _NBSP, _THIN, _ZWSP = chr(0x00A0), chr(0x2009), chr(0x200B)
    _FW = str.maketrans({str(_d): chr(0xFF10 + _d) for _d in range(10)})   # ASCII digit -> full-width
    _DEG, _CJK = chr(0x2103), chr(0x65E5) + chr(0x672C) + chr(0x8A9E)      # deg-C symbol; CJK to preserve
    _PII_PROBE = (
        "card 4111.1111.1111.1111 on file", "card 4111_1111_1111_1111 on file",
        "card 4111:1111:1111:1111 on file", "card 4111 1111 1111 1111 on file",
        "card 4111-1111-1111-1111 on file", "card 4111111111111111 on file",
        "ssn 123-45-6789 on file", "ssn 123.45.6789 on file", "ssn 123 45 6789 on file",
        "ping me at alex@example.com", "call +1 415-555-0142 today",
        "gateway 192.168.1.1 is up", "matrix 123.45.6789.0 address",
        "host 10.123.45.6789 responded", "oid 1.3.6.1.4.1 registered",
        "build 20260719.1 shipped", "job_123_45_6789 finished", "size 192x64x1024 buffer",
        "counts 1234 5678 9012 3456 7890", "id 123456789012 minted",
        "ports 8080 9090 3000 5000 7000", "the 2026-06-27 release", "v1.2.3.4 shipped",
        # the cue-gated bare form, and the runs that must survive beside it
        "ssn 123456789 on file", "SSN: 123456789", "social security number 123456789",
        "commit 123456789 in the log", "pid 123456789 exited",
        "ssn policy doc revision 4", "order 123456789 shipped to ssn-free address",
        # the two bounds that make the cue safe: its reach, and the digit boundary
        "ssn policy updated, see commit 123456789 for the change", "ssn=1234567890",
        # non-ASCII DISGUISE vectors (chr()-built, so the shipped source stays plain ASCII): a card or
        # SSN split by an invisible / exotic space, or written full-width, MUST redact identically in
        # BOTH fences. This is the class that leaked through Spirit before the PII fold was added.
        "card 4111" + _NBSP + "1111" + _NBSP + "1111" + _NBSP + "1111 nbsp",
        "card 4111" + _THIN + "1111" + _THIN + "1111" + _THIN + "1111 thin",
        "card 4111" + _ZWSP + "1111" + _ZWSP + "1111" + _ZWSP + "1111 zwsp",
        "card " + "4111 1111 1111 1111".translate(_FW) + " fullwidth",
        "ssn 123" + _NBSP + "45" + _NBSP + "6789 nbsp",
        "ssn 123" + _THIN + "45" + _THIN + "6789 thin",
        "reach j" + chr(0xFF41) + "ke@example.com now",
        # false-positive controls: legit non-ASCII / non-Luhn that must SURVIVE (a fold may canonicalise
        # a compatibility glyph, but must never fabricate or drop PII)
        "boiling at 100" + _DEG + " in the note", "memo " + _CJK + " stays intact",
        "order 1234 5678 9012 3456 not a card",
    )
    _pii_expected = "59938950d03579fdab537c968ea42b75081b57b738da9ee6e197e752240746e5"
    _pii_canon = "|".join(scrub.scrub(p, scrub_pii=True)[0] for p in _PII_PROBE)
    _pii_got = hashlib.sha256(_pii_canon.encode("utf-8")).hexdigest()
    if _pii_got != _pii_expected:
        fails.append(f"PARITY[pii behaviour] changed - got {_pii_got}, expected {_pii_expected} "
                     f"(update BOTH this and the Spirit companion, and say why in the commit)")

    total = len(REDACT) + len(KEEP) + len(PII) + len(PII_KEEP)
    if fails:
        print(f"FAIL - {len(fails)}/{total} checks failed:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"OK - all {total} scrub checks passed "
          f"({len(REDACT)} redact, {len(KEEP)} keep, {len(PII)} pii, {len(PII_KEEP)} pii-keep)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())
