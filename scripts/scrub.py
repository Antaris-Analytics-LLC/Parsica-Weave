#!/usr/bin/env python3
"""
Parsica Weave - content scrubber.

Redacts credentials/secrets (always) and PII (configurable) from any text BEFORE
it is written to the chat mirror or journal. The dual-write must never silently
hoard a pasted secret.

Design rules:
  * NEVER entropy-redact whole tokens. Transcripts are full of 40-char git SHAs,
    file hashes, base64 artifacts. Secrets are caught by SPECIFIC token shapes +
    key-name/value-SHAPE rules only.
  * Recall: catch opaque-valued secrets in assignment context regardless of the
    surrounding key name (access_token=, AccountKey=, "apiKey": "...", DB URLs),
    including values followed by trailing text on the same line.
  * Precision: do NOT gut ordinary code/config. A secret-named key with a CODE
    value (call, attribute access, path) or a config-enum value (AES-256-GCM,
    us-east-1, v1.2.3) is left intact. The value's SHAPE + position decide.
  * Placeholders are not secrets - skip <...>, ${VAR}, $VAR, ***, your-...

Public API:
    scrub(text, *, scrub_pii=True, refusal=None) -> (clean_text, n_redactions)
    looks_clean(text) -> bool        # quick "does this contain a secret?" probe
    apply_refusal(text, terms) -> (clean_text, n_redactions)   # redact operator-defined terms
"""
import re
import unicodedata

__all__ = ["scrub", "looks_clean", "apply_refusal", "is_genuine_marker", "REDACTION",
           "REFUSAL_MARKER", "PII_MARKERS", "MARKER_RE", "INVISIBLE_CODE_POINTS", "SECRET_LABELS"]

REDACTION = "[REDACTED:{label}]"
#: the PII masks, as CONSTANTS rather than literals inlined at each substitution site - a caller
#: that must protect an emitted marker from a second pass has to know the full set, and a hand-kept
#: copy in that caller is a list that drifts (this module already carries one such scar).
PII_MARKERS = ("[EMAIL]", "[SSN]", "[CARD]", "[PHONE]")
_MAXVAL = 8192  # upper length bound on a captured/judged secret value


def _mask(label):
    return REDACTION.format(label=label)


# ---- user refusal list (configurable keyword redaction) --------------------
# A user-supplied list of words/phrases redacted to a VISIBLE marker, ON TOP of the automatic
# secret/PII scrub - automatic scrub for the secrets everyone has, plus a configurable list for the
# terms secret only to you. Matching is SUBSTRING and case-insensitive (see _compile_refusal);
# longest-first so a phrase beats a token it contains. Marker `<redacted>` is visibly distinct from
# the auto [REDACTED:label] (told-to-hide vs auto-caught), and it leaves a trace, never a silent
# deletion.
REFUSAL_MARKER = "<redacted>"

#: EVERY marker family this module can emit, in one pattern. A caller that runs a SECOND refusal
#: pass over already-scrubbed text must skip these spans: apply_refusal matches substrings,
#: case-insensitively, so a refusal term that is a substring of a marker word rewrites the marker
#: the first pass just emitted (`red` turns `<redacted>` into `<<redacted>acted>`), corrupting the
#: audit trail on every line of a run. Owned HERE, beside the constants it is built from, so it
#: cannot drift out of step with them the way a copy in the caller would.
#:
#: The label class excludes `[` as well as `]`: a marker span must not be allowed to run THROUGH a
#: nested marker. `[REDACTED:` is ordinary text an untrusted body can type, and with `[` permitted a
#: forged prefix swallowed everything up to the first `]` - a genuine marker inside it included -
#: which is_genuine_marker then judged forged AS A WHOLE, handing the real marker to the refusal
#: sub. That is the exact `[EMAIL]` -> `[E<redacted>]` corruption this pattern exists to prevent,
#: and it also broke idempotence outright: pass 1's substitution shrank the span back under the
#: {1,64} bound, so pass 2 swallowed what pass 1 had protected. No emittable label contains a
#: bracket - every SECRET_LABELS value comes from _TOKEN_PATTERNS or the four literals, all
#: `[a-z_]` - so excluding `[` cannot lose a genuine marker; it can only split a forged span off
#: the real one nested inside it.
MARKER_RE = re.compile(
    "|".join([re.escape(REFUSAL_MARKER),
              re.escape(REDACTION.split("{label}")[0]) + r"[^\[\]\n]{1,64}\]"]
             + [re.escape(m) for m in PII_MARKERS]))

_refusal_cache = {}


def _compile_refusal(terms):
    clean = sorted({t.strip() for t in (terms or ()) if isinstance(t, str) and t.strip()},
                   key=len, reverse=True)
    if not clean:
        return None
    alts = "|".join(re.escape(t) for t in clean)
    # SUBSTRING match, no word boundary: a refusal term is a "never store this string" directive, so it
    # is redacted ANYWHERE it appears - glued into a word (codenamedProjectX) or camelCase
    # (myProjectXClient) included. The trade is over-redaction (refuse 'mars' and 'marsupial' also
    # goes); the safe side to err on for a secrecy fence. Longest-first alternation keeps a phrase
    # winning over a token it contains.
    return re.compile(r"(?:" + alts + r")", re.IGNORECASE)


def _get_refusal(terms):
    key = tuple(terms) if terms else ()
    if key not in _refusal_cache:
        _refusal_cache[key] = _compile_refusal(terms)
    return _refusal_cache[key]


def apply_refusal(text, terms):
    """Refusal-ONLY pass (no secret/PII scrub): redact the given terms to <redacted>. For a
    caller (enrich.py's overlay walk, store.fence_output) that already ran the full scrub and only
    needs the user terms fenced. Returns (clean_text, n_redactions).

    MARKER-AWARE (see _refuse_outside_markers): this is the output-side fence, so it runs over
    already-scrubbed text by definition - every caller here is re-fencing content that carries
    markers from an earlier pass. Genuine markers survive untouched; marker-SHAPED text does not."""
    if not text or not terms:
        return text, 0
    pat = _get_refusal(terms)
    if pat is None:
        return text, 0
    counter = [0]
    return _refuse_outside_markers(text, pat, counter), counter[0]


# ---- placeholder detection (these are NOT secrets) -------------------------
_PLACEHOLDER = re.compile(
    r"^(?:[<\[{].*[>\]}]|\$\{?\w+\}?|%\w+%|\*{3,}|x{3,}|\.{3,}|"
    r"your[-_].*|my[-_].*|example.*|changeme|change[-_]?me|redacted|placeholder|"
    r"none|null|true|false|test|dummy|sample|fake|todo|not[-_].*|tbd|"
    r"required|optional|enabled|disabled|unset|default|localhost|empty|auto)$",
    re.IGNORECASE,
)


def _is_placeholder(val):
    v = val.strip().strip("'\"")
    if not v or len(v) < 6:
        return True
    if _PLACEHOLDER.match(v):
        return True
    if v[0] in "$<%" or v.startswith("${"):
        return True
    return False


# ---- secret token patterns (specific prefixes / shapes) --------------------
# Left boundary: not glued to a preceding alphanumeric (catches separator- and
# underscore-glued pastes; a non-consuming lookbehind so it never eats a char).
_NB = r"(?<![A-Za-z0-9])"
_TOKEN_PATTERNS = [
    ("anthropic_key", re.compile(_NB + r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(_NB + r"sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(_NB + r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("gcp_api_key", re.compile(_NB + r"AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9])")),
    ("github_pat", re.compile(_NB + r"github_pat_[A-Za-z0-9_]{22,}")),
    ("github_token", re.compile(_NB + r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack_token", re.compile(_NB + r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("slack_app_token", re.compile(_NB + r"xapp-[0-9]-[A-Za-z0-9\-]{20,}")),
    ("stripe_key", re.compile(_NB + r"(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{16,}")),
    ("stripe_webhook_secret", re.compile(_NB + r"whsec_[0-9a-zA-Z]{16,}")),
    ("gitlab_token", re.compile(_NB + r"(?:glpat|gloas|gldt|glrt|glrtr|glcbt|glptt|glsoat)-[A-Za-z0-9_\-]{16,}")),
    ("google_oauth", re.compile(_NB + r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("google_client_secret", re.compile(_NB + r"GOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("npm_token", re.compile(_NB + r"npm_[A-Za-z0-9]{36}(?![A-Za-z0-9])")),
    ("pypi_token", re.compile(_NB + r"pypi-AgE[A-Za-z0-9_\-]{16,}")),
    ("hf_token", re.compile(_NB + r"hf_[A-Za-z0-9]{34,}")),
    ("sendgrid_key", re.compile(_NB + r"SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}(?![A-Za-z0-9])")),
    ("digitalocean_token", re.compile(_NB + r"dop_v1_[a-f0-9]{64}(?![a-f0-9])")),
    ("docker_pat", re.compile(_NB + r"dckr_pat_[A-Za-z0-9_\-]{20,}")),
    # Telegram bot token - no left boundary so the in-URL form (.../bot<token>/...) is caught.
    ("telegram_bot_token", re.compile(r"[0-9]{8,10}:AA[A-Za-z0-9_\-]{32,33}(?![A-Za-z0-9_\-])")),
    ("discord_token", re.compile(_NB + r"[MNO][A-Za-z0-9]{23}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}")),
    ("jwt", re.compile(_NB + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("discord_webhook", re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_\-]+")),
    ("bearer_token", re.compile(r"(?<![A-Za-z0-9])Bearer\s+(?=[A-Za-z0-9._\-]*[A-Z0-9])[A-Za-z0-9._\-]{20,}")),
    # scheme://[user]:password@host  - any scheme, optional user, always-on. The
    # (?!\d+@) keeps host:port@host infra notation (redis://cache:6379@primary) intact.
    ("conn_string_cred", re.compile(_NB + r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]*:(?!\d+@)[^/\s@]{3,}@")),
]

#: EVERY label this module's own `_mask()`/literal calls can put inside `[REDACTED:{label}]` - the
#: enumerable counterpart to PII_MARKERS/REFUSAL_MARKER for the one marker family with variable
#: content. A caller distinguishing a GENUINE marker from marker-SHAPED text in an untrusted string
#: (an attacker can forge "[REDACTED:xFOOx]" around a live secret in a filename MARKER_RE alone
#: cannot tell apart - convergence pass 4) checks the label against this set: `_TOKEN_PATTERNS`'
#: labels plus the few call sites that mask with a literal label directly (grepped exhaustively,
#: not hand-guessed - see every `_mask(` and literal `"[REDACTED:` site in this file).
SECRET_LABELS = frozenset(
    ["secret", "private_key", "signature", "auth_header"] + [label for label, _ in _TOKEN_PATTERNS]
)

_REDACTION_PREFIX = REDACTION.split("{label}")[0]        # "[REDACTED:"


def is_genuine_marker(span):
    """True only for a marker this module can ACTUALLY emit - not merely marker-SHAPED text. The
    fixed-literal families (REFUSAL_MARKER, PII_MARKERS) carry no variable content, so an exact
    match is always safe to trust regardless of where the text came from. The one variable-content
    family, [REDACTED:<label>], is trusted only when <label> is one of SECRET_LABELS' real values -
    anything else is untrusted content merely shaped like a marker (an attacker can forge
    "[REDACTED:xCODENAMEx]" around a live term in a legacy filename, and MARKER_RE alone cannot tell
    the two apart). Lives HERE, beside MARKER_RE and the constants both are built from, for the same
    reason MARKER_RE does: a copy in a caller is a list that drifts."""
    if span == REFUSAL_MARKER or span in PII_MARKERS:
        return True
    if span.startswith(_REDACTION_PREFIX) and span.endswith("]"):
        return span[len(_REDACTION_PREFIX):-1] in SECRET_LABELS
    return False


def _refuse_outside_markers(text, pat, counter):
    """Substitute `pat` (a compiled refusal alternation) everywhere in `text` EXCEPT inside a
    genuine marker. The single marker-aware refusal primitive, shared by apply_refusal() and
    scrub()'s own trailing refusal pass, so the two cannot diverge.

    Why this must exist at all: refusal matching is SUBSTRING and case-insensitive, so a term that
    is a substring of a marker word rewrites a marker some earlier pass already emitted - `red`
    turning `<redacted>` into `<<redacted>acted>`, and the same for [REDACTED:...], [EMAIL], [SSN],
    [CARD], [PHONE]. That corrupts the audit trail, and it is NOT only a re-entry problem: inside a
    single scrub() call the secret/PII passes emit their markers BEFORE the refusal pass runs, so
    `scrub("mail: a@b.com", refusal=["mail"])` corrupted `[EMAIL]` to `[E<redacted>]` on the first
    pass. The property this restores is idempotence on marked text: scrub(scrub(x)) == scrub(x).

    A FORGED marker-shaped span is deliberately treated as ordinary content and fenced, so the
    marker-awareness cannot become a bypass - hiding a refusal term inside a hand-typed
    "[REDACTED:...]" bracket must not smuggle it through.

    Accepted, pre-existing tradeoff: a refusal term that SPANS a marker boundary no longer matches.
    For a term to span a marker, an earlier pass must have redacted content sitting inside that
    term - so the sensitive middle is already gone. This is the same reasoning _emit_migrate's pass
    2 has recorded since round 4, and this primitive generalizes that loop."""
    def _repl(m):
        counter[0] += 1
        return REFUSAL_MARKER

    out, last = [], 0
    for m in MARKER_RE.finditer(text):
        out.append(pat.sub(_repl, text[last:m.start()]))
        span = m.group(0)
        out.append(span if is_genuine_marker(span) else pat.sub(_repl, span))
        last = m.end()
    out.append(pat.sub(_repl, text[last:]))
    return "".join(out)


# `(?: BLOCK)?` so PGP/GPG armored private keys (-----BEGIN PGP PRIVATE KEY BLOCK-----)
# match alongside RSA/EC/OPENSSH/ENCRYPTED/PKCS8/DSA. PUBLIC KEY armor is left intact.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)

# Signed-URL credential query params - keep the URL shape, redact only the credential-bearing
# value (AWS presigned X-Amz-Signature, Azure SAS sig, generic signature=...).
_SIGNED_URL_PARAM = re.compile(r"(?i)([?&](?:sig|signature|x-amz-signature)=)([^&#\s\"']{16,})")

# Authorization headers - case-INSENSITIVE on the scheme word only; the value is judged by
# a callback (placeholder check + case-sensitive entropy) so prose like "Bearer required"
# survives while real header-shaped credentials are redacted.
_AUTH_HEADER = re.compile(
    r"\bAuthorization:[ \t]*(?:(?P<scheme>Bearer|Basic|Token|ApiKey|Digest)[ \t]+)?(?P<val>[A-Za-z0-9._/+=\-]{8,})",
    re.IGNORECASE,
)
# a pure lowercase snake_case / hyphenated identifier (x_internal_request_id,
# middleware-request-validator) - a code symbol, NOT an opaque credential.
_AUTH_IDENT = re.compile(r"[a-z0-9]+(?:[_\-][a-z0-9]+)+$")


def _auth_header_value_is_secret(val):
    if _is_placeholder(val):
        return False
    # The literal `Authorization:` header name is strong credential context, so a long opaque
    # value (>=20 chars) is a credential regardless of scheme word OR case - gateways/proxies
    # log "Authorization: <opaque-token>" with no scheme. But a long pure-lowercase snake_case /
    # hyphenated IDENTIFIER (no digit/symbol/case-mix) is a code symbol, not a token (priority
    # #2), so length alone must not eat it. Placeholders already bailed.
    if len(val) >= 20 and not _AUTH_IDENT.match(val):
        return True
    has_digit = any(c.isdigit() for c in val)
    has_sym = any(c in "+/=" for c in val)
    has_mix = any(c.isupper() for c in val) and any(c.islower() for c in val)
    return has_digit or has_sym or has_mix

# ---- assignment scrubbing: key-name + value-SHAPE driven -------------------
# A key "counts" as secret-bearing if a delimited segment matches one of these
# compounds. Bare 'key' is deliberately EXCLUDED (cache_key/sort_key/primary_key
# are ordinary code); only specific *_key compounds count.
_SECRET_KEY_COMPOUNDS = (
    r"api[_-]?key", r"access[_-]?key", r"secret[_-]?key", r"account[_-]?key",
    r"subscription[_-]?key", r"consumer[_-]?key", r"encryption[_-]?key",
    r"private[_-]?key", r"signing[_-]?key", r"auth[_-]?key",
    r"client[_-]?secret", r"auth[_-]?token", r"session[_-]?token",
    r"refresh[_-]?token", r"access[_-]?token", r"api[_-]?token",
    r"bot[_-]?token", r"app[_-]?token", r"bearer[_-]?token",
    r"secret", r"password", r"passwd", r"passphrase",
)
_SECRET_KEY_RE = re.compile(
    r"^(?:[a-z0-9]+[_\-])*(?:" + "|".join(_SECRET_KEY_COMPOUNDS) + r")(?:[_\-][a-z0-9]+)?$",
    re.IGNORECASE,
)
_SECRET_SUFFIX_RE = re.compile(
    r"(?:^|[_\-])(?:token|secret|password|passwd|passphrase)$", re.IGNORECASE
)
# a key ending in one of these is a REFERENCE (the path/name/version OF a secret),
# not the secret value itself -> never redact its value. NOTE: 'id' and 'header'
# are deliberately NOT here - secret_id / private_key_id / apikey_header are real
# credentials, not references (their value-shape gate still guards precision).
_KEY_REF_SUFFIX_RE = re.compile(
    r"(?:^|[_\-])(?:path|file|files|dir|directory|name|url|uri|"
    r"regex|pattern|count|len|length|size|type|field|column|var|env|prefix|suffix|"
    r"enabled|disabled|required|optional|ttl|expiry|expires|format|encoding|algo|"
    r"algorithm|hash|hint|label|kind|class|scope|list|map|set|index|store|provider|"
    r"manager|service|rotation|policy|mode|status|state|version|revision|rev)$",
    re.IGNORECASE,
)


# An unambiguous secret base compound (secret/password/token/api_key/...) as a delimited
# segment makes the key secret-bearing EVEN under a reference suffix: secret_state,
# password_mode, api_key_scope are secrets, not references. The value-SHAPE gate still
# spares a genuine path/name/version value, so this cannot over-redact ordinary config.
_UNAMBIG_SECRET_RE = re.compile(
    r"(?:^|[_\-])(?:" + "|".join(_SECRET_KEY_COMPOUNDS) + r")(?:[_\-]|$)",
    re.IGNORECASE,
)
# "weak" reference suffixes: config nouns ABOUT a secret (state/mode/scope/status/...) which,
# unlike a genuine reference (name/path/url/file/version...), do NOT prove the value is not the
# secret itself. On an unambiguous secret base these must still be scrubbed (secret_state,
# password_mode), while genuine references stay exempt (api_key_name, secret_path, token_url).
_WEAK_REF_SUFFIX_RE = re.compile(
    r"(?:^|[_\-])(?:state|mode|scope|status|kind|class|enabled|disabled|required|optional)$",
    re.IGNORECASE,
)


def _is_secret_key(key):
    k = (key or "").strip()
    if not k:
        return False
    if _KEY_REF_SUFFIX_RE.search(k):
        # A reference suffix normally exempts the value (it names a path/file/url/version OF a
        # secret, not the secret). Exception - the reviewer's bypass class: a WEAK config-noun
        # suffix (state/mode/scope/status/...) on an unambiguous secret base is still a secret.
        if _WEAK_REF_SUFFIX_RE.search(k) and _UNAMBIG_SECRET_RE.search(k):
            return True
        return False
    if _SECRET_SUFFIX_RE.search(k):
        return True
    return bool(_SECRET_KEY_RE.match(k))


_ATTR_ACCESS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)+$")
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SEG_RE = re.compile(r"[-._]")


def _has_entropy(v):
    return (any(c.isdigit() for c in v) or any(c.isupper() for c in v)
            or any(c in "+/=" for c in v))


def _is_config_enum(v):
    """Cipher names / regions / versions / short enums: every '-._'-segment is short
    (AES-256-GCM, us-east-1, v1.2.3-beta). A long, many-segment value is an opaque
    grouped secret, NOT an enum, so it is not whitelisted."""
    segs = [s for s in _SEG_RE.split(v) if s]
    if not segs or not all(len(s) <= 8 for s in segs):
        return False
    if len(v) >= 24 and len(segs) >= 6:
        return False
    return True


_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_PATHSEG_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _is_snake_identifier(v):
    """lowercase words joined by underscores -> a code identifier / phrase, not a secret."""
    return bool(_SNAKE_RE.match(v))


def _looks_like_path(v):
    """A slash-delimited reference (Vault KV path secret/data/prod/db) - every segment a
    short word, no long opaque base64 blob - is a path, not the secret itself."""
    if "/" not in v:
        return False
    segs = [s for s in v.split("/") if s]
    return bool(segs) and all(len(s) <= 16 and _PATHSEG_RE.match(s) for s in segs)


def _looks_secret_quoted(v, explicit=False):
    """A quoted RHS after a secret key. `explicit` = the key is an unambiguous
    password/passphrase/secret/token name, so an all-lowercase passphrase counts."""
    v = v.strip()
    if not (6 <= len(v) <= _MAXVAL):
        return False
    if _is_placeholder(v):
        return False
    if any(c.isspace() for c in v):  # secrets have no internal whitespace
        return False
    if v[0] in "/~" or _WIN_PATH_RE.match(v):
        return False
    if _looks_like_path(v):          # secret/data/prod/db -> a path reference
        return False
    if _is_config_enum(v):           # AES-256-GCM / us-east-1 / v1.2.3-beta
        return False
    if _is_snake_identifier(v):      # the_function_returns_early -> code/phrase
        return False
    if explicit:                     # explicit secret key + delimited value -> trust it
        return True
    if not _has_entropy(v):          # plain dictionary words -> not a secret
        return False
    return True


def _looks_secret_value(v, standalone=True, explicit=False):
    """An UNQUOTED RHS token. Stricter than quoted: reject code/path/hash shapes. When
    not standalone (trailing prose follows), require a digit/symbol so camelCase prose
    words aren't masked. `explicit` lets an all-lowercase passphrase under an explicit
    password/secret key be caught (correcthorsebatterystaple)."""
    v = v.strip().strip("'\"")
    if len(v) < 8 or len(v) > _MAXVAL:
        return False
    if _is_placeholder(v):
        return False
    if any(c.isspace() for c in v):
        return False
    if v[0] in "/~." or _WIN_PATH_RE.match(v):  # path
        return False
    if "(" in v or ")" in v:                     # code call
        return False
    if _ATTR_ACCESS_RE.match(v):                 # attribute access -> code
        return False
    if _looks_like_path(v):                      # secret/data/prod/db -> path reference
        return False
    if _is_config_enum(v):                       # AES-256-GCM etc.
        return False
    if _is_snake_identifier(v):                  # the_function_returns_early -> code
        return False
    # explicit secret key + an all-lowercase value: catch a real passphrase
    # (correcthorsebatterystaple, supersecretword) but NOT an ordinary prose word that follows
    # a literal password:/secret: in notes (yesterday, configuration, authentication). Length
    # is the only separator with entropy-off - require 15+, above the common prose words.
    if explicit and re.match(r"^[a-z]{15,}$", v):
        return True
    has_digit = any(c.isdigit() for c in v)
    has_symbol = any(c in "+/=" for c in v)
    if has_digit or has_symbol:
        return True
    if standalone and any(c.isupper() for c in v):
        # a bare PascalCase/camelCase word with no separator/digit is a code
        # identifier (AuthTokenProvider, OAuthClient), not an opaque secret
        if re.match(r"^[A-Za-z][A-Za-z0-9]*$", v):
            return False
        return True
    return False


# key (optional quotes) sep value (quoted)
_ASSIGN_QUOTED = re.compile(
    r"(?P<kq>[\"']?)(?P<key>[A-Za-z][\w\-]{0,40})(?P=kq)"
    r"(?P<sep>\s*(?:\|\|=|\?\?=|\+=|=>|:=|[:=])\s*)"
    r"(?P<vq>[\"'])(?P<val>[^\"'\r\n]{1," + str(_MAXVAL) + r"})(?P=vq)"
)
# key (optional quotes) sep value (bare token, self-terminating at ws/terminator). The
# trailing lookahead accepts a closing quote/backtick as a terminator (mirroring the value
# class) so a quote-wrapped credential - curl -H 'X: api_key=<secret>' - is reachable.
_ASSIGN_BARE = re.compile(
    r"(?P<kq>[\"']?)(?P<key>[A-Za-z][\w\-]{0,40})(?P=kq)"
    r"(?P<sep>\s*(?:\|\|=|\?\?=|\+=|=>|:=|[:=])\s*)"
    r"(?P<val>(?:\[REDACTED(?::[^\]]*)?\]|[^\s\"'`;,)\]}&?\r\n]){6," + str(_MAXVAL) + r"})(?=[\s\"'`;,)\]}&?]|$)"
)

# A secret-key assignment that a NON-SECRET prefix shadowed in the bare pass: in
# "DEBUG: api_key = <secret>" the bare value class stops at the space, so the outer non-secret
# key ('DEBUG') eats the inner key NAME ('api_key') and the value past the space is never seen.
# This pass is anchored on the secret-key NAME itself (prefix-independent), catching the value
# the bare pass missed. It runs last; values already redacted (starting "[REDACTED") are skipped.
_SHADOW_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<key>(?:[A-Za-z0-9]+[_\-])*(?:" + "|".join(_SECRET_KEY_COMPOUNDS) + r")(?:[_\-][A-Za-z0-9]+)?)"
    r"(?P<sep>\s*(?:\|\|=|\?\?=|\+=|=>|:=|[:=])\s*)"
    r"(?P<val>(?:\[REDACTED(?::[^\]]*)?\]|[^\s\"'`;,)\]}&?\r\n]){6," + str(_MAXVAL) + r"})(?=[\s\"'`;,)\]}&?]|$)",
    re.IGNORECASE,
)


_VAL_MARKER_SPLIT = re.compile(r"(\[REDACTED(?::[^\]]*)?\]?)")   # keep markers when splitting a value


def _value_with_marker_redacted(val, counter):
    """A captured value that ALREADY contains a redaction marker: redact any LIVE secret material
    around the marker (a real secret glued before/after an in-place redaction) while PRESERVING the
    marker(s) - never re-mask a marker (no double-count) and never leave a stray bracket. Returns
    the rebuilt value, or None when there is no live secret (caller leaves the span untouched)."""
    found = False
    out = []
    for seg in _VAL_MARKER_SPLIT.split(val):
        if not seg:
            continue
        if seg.startswith("[REDACTED"):
            out.append(seg)                     # an existing marker - keep verbatim
            continue
        core = seg.strip(" =:;,\t./~")          # strip a key-separator / glue wrapper (incl '.'/'/' glue)
        if core and _looks_secret_value(core, standalone=True, explicit=True):
            out.append(seg.replace(core, _mask("secret"), 1))
            found = True
        else:
            out.append(seg)
    if not found:
        return None
    counter[0] += 1
    return "".join(out)


# A live secret glued to an in-place marker via a value-TERMINATOR char (',' ';' ')' ']' '}' '&'
# '?') - the assignment value class STOPS at the terminator, so a trailing opaque blob escapes the
# marker handler above (two concatenated secrets: `password=AKIA...[id-redacted],wJalrXU...`). Catch
# <marker><terminator(s)><bare-opaque-blob>. The blob class excludes '='/':' so a following
# key=value (';Host=db') is NOT a blob (left alone), and _looks_secret_value gates it so a snake_case
# id / config-enum / path / plain word glued after a marker survives.
_MARKER_SEP_GLUE = re.compile(
    r"(\[REDACTED(?::[^\]]*)?\])([;,)\]}&?]+)([^\s\"'`;,)\]}&?=:\r\n]{8," + str(_MAXVAL) + r"})"
)


def _redact_marker_glue(text, counter):
    def repl(m):
        if _looks_secret_value(m.group(3), standalone=True, explicit=False):
            counter[0] += 1
            return m.group(1) + m.group(2) + _mask("secret")
        return m.group(0)
    return _MARKER_SEP_GLUE.sub(repl, text)


# YAML / indented block-scalar credentials: `api_key: |` or `private_key: >` followed by a
# more-indented block (pasted k8s Secrets, Ansible vault, GCP service-account keys, non-PEM
# armored blobs). The single-line assignment scrubber cannot see these. The block indicator
# (| or >) marks an intentional literal value; redact the block when the key is secret-shaped.
_BLOCK_SCALAR_KEY = re.compile(
    r"^(?P<indent>[ \t]*)(?P<q>[\"']?)(?P<key>[A-Za-z][\w\-]{0,40})(?P=q)"
    r"[ \t]*:[ \t]*[|>][+\-]?[ \t]*(?:#.*)?$"
)


def _redact_block_scalars(text, counter):
    if "|" not in text and ">" not in text:
        return text
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _BLOCK_SCALAR_KEY.match(line)
        if m and _is_secret_key(m.group("key")):
            key_indent = len(m.group("indent"))
            j = i + 1
            block_has_content = False
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "":
                    j += 1
                    continue
                if (len(nxt) - len(nxt.lstrip())) > key_indent:
                    block_has_content = True
                    j += 1
                else:
                    break
            if block_has_content:
                out.append(line)                     # keep the `key: |` line intact
                counter[0] += 1
                out.append(m.group("indent") + "  " + _mask("secret"))
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _redact_assignments(text, counter):
    def repl_quoted(m):
        val = m.group("val")
        key = m.group("key")
        if "[REDACTED" in val:                  # already-redacted: mask only LIVE residue, keep markers
            # gate on a secret key - a marker's mere presence must NOT flip a NON-secret key's value
            # (a git SHA / long word glued beside a marker under commit_note/log_line must survive).
            if not _is_secret_key(key):
                return m.group(0)
            rebuilt = _value_with_marker_redacted(val, counter)
            if rebuilt is None:
                return m.group(0)
            kq, vq = m.group("kq"), m.group("vq")
            return f"{kq}{key}{kq}{m.group('sep')}{vq}{rebuilt}{vq}"
        # every key in _SECRET_KEY_COMPOUNDS is an UNAMBIGUOUS secret key (api_key, secret_key,
        # auth_key, client_secret, *_token, password...), so an all-lowercase quoted value under
        # one is the secret - treat it as explicit. Config-enum/path/placeholder gates in
        # _looks_secret_quoted still spare AES-256-GCM / us-east-1 / not-configured-yet.
        if _is_secret_key(key) and _looks_secret_quoted(val, explicit=True):
            counter[0] += 1
            kq, vq = m.group("kq"), m.group("vq")
            return f"{kq}{key}{kq}{m.group('sep')}{vq}{_mask('secret')}{vq}"
        return m.group(0)

    def repl_bare(m):
        val = m.group("val")
        key, kq, sep = m.group("key"), m.group("kq"), m.group("sep")
        if _is_secret_key(key):
            if "[REDACTED" in val:              # already-redacted: mask only LIVE residue, keep markers
                rebuilt = _value_with_marker_redacted(val, counter)
                return f"{kq}{key}{kq}{sep}{rebuilt}" if rebuilt is not None else m.group(0)
            # bounded lookahead: m.string[m.end():] is O(n) per match -> O(n^2) on text
            # with many assignments (a multi-MB paste hangs past the hook timeout). Only the
            # rest of THIS line decides `standalone`, so a small fixed window suffices.
            rest = m.string[m.end():m.end() + 200]
            line_rest = rest.split("\n", 1)[0].strip()
            standalone = (line_rest == "" or line_rest[:1] in ";,)]}")
            if _looks_secret_value(val, standalone=standalone, explicit=True):
                counter[0] += 1
                return f"{kq}{key}{kq}{sep}{_mask('secret')}"
        # a non-secret (or non-opaque) PREFIX key can shadow an inner key=value, because the
        # bare-value class admits '='/':' (Cookie: session_token=<secret>, curl -H 'X: k=<s>',
        # DEBUG: api_key=<secret>) - re-scan the captured value for an inner assignment so the
        # real secret isn't swallowed whole by the outer non-secret key and skipped. (Skip when a
        # marker is present - that case is handled in the secret-key branch above.)
        if ("=" in val or ":" in val) and "[REDACTED" not in val:
            inner = _ASSIGN_BARE.sub(repl_bare, val)
            if inner != val:
                return f"{kq}{key}{kq}{sep}{inner}"
        return m.group(0)

    def repl_shadow(m):
        key, sep, val = m.group("key"), m.group("sep"), m.group("val")
        if not _is_secret_key(key):
            return m.group(0)
        if "[REDACTED" in val:                  # already-redacted: mask only LIVE residue, keep markers
            rebuilt = _value_with_marker_redacted(val, counter)
            return f"{key}{sep}{rebuilt}" if rebuilt is not None else m.group(0)
        if _looks_secret_value(val, standalone=True, explicit=True):
            counter[0] += 1
            return f"{key}{sep}{_mask('secret')}"
        return m.group(0)

    text = _ASSIGN_QUOTED.sub(repl_quoted, text)
    text = _ASSIGN_BARE.sub(repl_bare, text)
    text = _SHADOW_KEY_RE.sub(repl_shadow, text)   # catch prefix-shadowed spaced assignments
    return text


# ---- PII --------------------------------------------------------------------
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"
)
#: Both accept the FULL common separator class, not just space and hyphen. Accepting only ` ` and
#: `-` meant a Luhn-valid PAN written `4111.1111.1111.1111` (or with `_` or `:`) reached disk
#: unredacted - reproduced end-to-end through `note` against the publication archive, so this is
#: PII on disk, not a theoretical gap.
#:
#: The card widening is free: a match is only redacted when the Luhn checksum passes, so the
#: checksum absorbs every false positive the wider class introduces (measured against a corpus of
#: version strings, IPs, ports, digests and numeric runs - zero new redactions).
#:
#: The SSN has NO checksum, so its widening is bounded by lookarounds instead: a run already
#: embedded in a longer dotted sequence (`10.123.45.6789`, `123.45.6789.0`) is NOT an SSN, and
#: excluding those is what keeps IPs, OIDs and version strings intact while still catching the
#: three shapes a person actually writes an SSN in.
_SSN = re.compile(r"(?<![\d.])\d{3}[-. ]\d{2}[-. ]\d{4}(?![\d.])")
_CC = re.compile(r"\b(?:\d[ \-._:]?){13,19}\b")

#: The BARE nine-digit form, which is how a database, a CSV export or a form field writes an SSN -
#: and which the separated pattern above cannot catch. Redacting every 9-digit run to reach it is
#: not an option: measured against ordinary developer text it destroys 14 of 15 strings (commit
#: ids, pids, byte counts, row counts, epochs), and this gate is always-on and by its own contract
#: not optional, so a false positive here is unrecoverable content loss rather than an annoyance.
#:
#: So the bare form is gated on an adjacent CUE. "ssn 123456789" is an SSN by the author's own
#: label; "commit 123456789" is not. Measured on the same corpora: 6/6 real leak shapes caught,
#: 0/15 false positives. The cue does the work the checksum does for cards - it supplies the
#: evidence the digits alone cannot.
#:
#: The cue must be a STANDALONE word (\b on both sides). Widening it to compound identifiers like
#: `customer_ssn` was tried and reverted: "ssn" is overloaded (SS7 SubSystem Number, 802.11
#: Sequence Number, ETL batch Sequence Number), so `local_ssn: 123456789` and `sub_ssn=123456789`
#: get eaten, and dropping the trailing \b let the cue glue to a run inside an opaque token, silently
#: deleting the middle of `SSN123456789ab`. Those are false positives an always-on, unrecoverable
#: gate cannot afford - catching person-prefixed fields safely needs a curated prefix allowlist,
#: not a widening. The trailing (?!\.\d) excludes a decimal: a 9-digit run followed by a dot and
#: more digits (`123456789.42`) is a number, not an SSN, and is left intact - but `ssn 123456789.`
#: at a sentence end still redacts.
#:
#: Deliberately NOT a general widening of _SSN: the separator classes above stay exactly as narrow
#: as they were, and this adds one narrow, evidence-bearing shape rather than relaxing a bound.
#: The captured group is the digits only, so the cue word itself survives in the text - a reader
#: still sees "ssn [SSN]" and knows what was removed.
_SSN_CUED = re.compile(
    r"(?i)\b(?:ssn|ssns|social[ _\-]?security(?:[ _\-]?(?:number|numbers|no|num|#))?)\b"
    r"\W{0,12}(?<![\d-])(\d{9})(?![\d-])(?!\.\d)"
)


def _luhn_ok(digits):
    s = 0
    for i, d in enumerate(reversed(digits)):
        d = int(d)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


# Characters that are invisible or blank-but-not-a-space: a secret spliced across one of these
# (sk-ant-<ZWSP>KEY...) dodges every pattern below, and an invisible glyph riding into the store
# is itself an injection/surveillance vector (the braille-blank U+2800 evasion). Strip them FIRST.
#
# THE CANONICAL SET (convergence pass 3, Q6): Unicode's own Default_Ignorable_Code_Point property
# (derived from DerivedCoreProperties.txt, Unicode 15.0.0 - the version this build's `unicodedata`
# is compiled against), plus U+2800 BRAILLE PATTERN BLANK, which Unicode does NOT mark ignorable (a
# braille cell is meaningful text) but which renders indistinguishable from whitespace and is a
# documented screen-reader-abuse / surveillance-marker vector. This is the ONE set this product
# owns; scripts/store.py imports it rather than keeping its own copy, and Spirit's separate wheel
# pins its own copy to the SAME ranges (see parsica_spirit/enrich/scrub.py).
#
# ENUMERATED, never a blanket category rule: Default_Ignorable is a curated Unicode property, not
# `category(ch) == "Mn"` or `== "Cf"`. Verified (scripts/test_scrub.py) that this set touches NO
# combining mark that carries real meaning - "cafe"+U+0301, Devanagari vowel signs, and every
# accented/Indic script are untouched - and that it is STRICTLY MORE PRECISE than the bare
# `category(ch) == "Cf"` check it replaces: every Cf code point NOT in this set (verified against
# the full Unicode codespace) is real script punctuation - ARABIC END OF AYAH, SYRIAC ABBREVIATION
# MARK, KAITHI NUMBER SIGN, the Egyptian hieroglyph layout controls - not invisible filler, so the
# narrower enumerated set stops silently mangling that text instead of widening what it catches.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180D), (0x180E, 0x180E), (0x180F, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2065, 0x2065),
    (0x2066, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0000), (0xE0001, 0xE0001), (0xE0002, 0xE001F), (0xE0020, 0xE007F),
    (0xE0080, 0xE00FF), (0xE0100, 0xE01EF), (0xE01F0, 0xE0FFF),
)
INVISIBLE_CODE_POINTS = frozenset(
    cp for lo, hi in _DEFAULT_IGNORABLE_RANGES for cp in range(lo, hi + 1)
) | frozenset({0x2800})


def _denormalize_invisibles(text):
    if text.isascii():
        return text                     # ASCII carries no invisibles/compat forms - fast path
    out = [ch for ch in text if ord(ch) not in INVISIBLE_CODE_POINTS]
    # NFKC folds compatibility forms (full-width -> ascii), closing the full-width-char evasion.
    return unicodedata.normalize("NFKC", "".join(out))


def scrub(text, *, scrub_pii=True, refusal=None):
    """Return (clean_text, n_redactions). Secrets always; PII when scrub_pii; plus any user
    `refusal` terms (a list of words/phrases) redacted to <redacted> as a final pass. Invisible
    and blank-glyph characters are stripped first so a secret can't hide across them."""
    if not text:
        return text, 0
    text = _denormalize_invisibles(text)   # invisibles/blank-glyphs FIRST, before any pattern
    counter = [0]

    def _pem_repl(m):
        counter[0] += 1
        return _mask("private_key")

    text = _PEM_BLOCK.sub(_pem_repl, text)

    for label, pat in _TOKEN_PATTERNS:
        def _repl(m, _label=label):
            counter[0] += 1
            return _mask(_label)

        text = pat.sub(_repl, text)

    def _sig_repl(m):
        counter[0] += 1
        return m.group(1) + "[REDACTED:signature]"

    text = _SIGNED_URL_PARAM.sub(_sig_repl, text)

    def _auth_repl(m):
        val = m.group("val")
        if not _auth_header_value_is_secret(val):
            return m.group(0)
        counter[0] += 1
        return m.group(0)[: m.start("val") - m.start(0)] + "[REDACTED:auth_header]"

    text = _AUTH_HEADER.sub(_auth_repl, text)

    text = _redact_block_scalars(text, counter)  # multi-line YAML/indented secret blocks
    text = _redact_assignments(text, counter)
    text = _redact_marker_glue(text, counter)   # secret glued to a marker via a value-terminator char

    if scrub_pii:
        def _email_repl(m):
            counter[0] += 1
            return PII_MARKERS[0]

        text = _EMAIL.sub(_email_repl, text)

        def _ssn_repl(m):
            counter[0] += 1
            return PII_MARKERS[1]

        text = _SSN.sub(_ssn_repl, text)

        def _ssn_cued_repl(m):
            # replace only the captured digits, so the cue word stays and the reader can see
            # WHAT was removed rather than just that something was
            counter[0] += 1
            return m.group(0).replace(m.group(1), PII_MARKERS[1])

        text = _SSN_CUED.sub(_ssn_cued_repl, text)

        def _cc_repl(m):
            raw = m.group(0)
            digits = re.sub(r"\D", "", raw)
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                counter[0] += 1
                return PII_MARKERS[2]
            return raw

        text = _CC.sub(_cc_repl, text)

        def _phone_repl(m):
            counter[0] += 1
            return PII_MARKERS[3]

        text = _PHONE.sub(_phone_repl, text)

    # user refusal list runs LAST (independent of scrub_pii): redact the caller's own terms
    # to <redacted>, so continuity capture can proceed with the sensitive words fenced out.
    # MARKER-AWARE (see _refuse_outside_markers). This pass runs LAST, so by the time it executes
    # the secret/PII passes above have already emitted their markers into `text` - a refusal term
    # that is a substring of a marker word ("mail" in [EMAIL], "red" in <redacted>) would rewrite
    # them, corrupting the audit trail on the FIRST pass, before any question of re-entry.
    if refusal:
        pat = _get_refusal(refusal)
        if pat is not None:
            text = _refuse_outside_markers(text, pat, counter)

    return text, counter[0]


def looks_clean(text):
    """Cheap probe: True if no secret pattern fires (ignores PII)."""
    _, n = scrub(text, scrub_pii=False)
    return n == 0


if __name__ == "__main__":
    import sys
    data = sys.stdin.read()
    clean, n = scrub(data)
    sys.stderr.write(f"[scrub] {n} redaction(s)\n")
    sys.stdout.write(clean)
