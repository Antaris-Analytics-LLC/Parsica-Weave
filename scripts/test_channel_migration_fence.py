"""W2 + RC5-D regression: legacy channel-registry migration is preserve-everything for VALID ids
(scrub names/aux in place, never drop the record) and quarantine for INVALID keys (a key that is not
a minted ch-<hex> id was never minted by us, so no live chunk references it - refuse it as a live key,
never import an arbitrary string that could ride a secret/refusal-term/path onto disk and the manifest).

W2 covered the VALUES (names, aux metadata); a legacy registry's KEYS are untrusted import data too.
Uses FAKE sentinels.
"""
import json
import os
import tempfile
import time

# STORE_ROOT is computed at import from this env var, so it must be set before importing store.
os.environ["PARSICA_WEAVE_ROOT"] = tempfile.mkdtemp(prefix="w2_")
os.environ.pop("PARSICA_ISOLATED", None)
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

REFUSAL = "ZZSWORDFISHZZ"
SECRET = "sk-ant-api03-" + "a" * 40
PHONE = "415-555-1212"


def _reset():
    """Fresh registry + a workspace config carrying the REFUSAL sentinel (tests share one STORE_ROOT)."""
    store.STORE_ROOT.mkdir(parents=True, exist_ok=True)
    p = store.channels_path()
    if p.exists():
        p.unlink()
    (store.STORE_ROOT / "config.json").write_text(
        json.dumps({"refusal": [REFUSAL]}), encoding="utf-8")


def test_valid_ids_scrub_in_place_and_are_preserved():
    _reset()
    legacy = {"channels": {
        "ch-deadbeef": {"name": REFUSAL, "created": "2026-07-01T00:00:00Z"},   # refusal term in NAME
        "ch-cafe1234": {"name": SECRET, "aws": "AKIAIOSFODNN7EXAMPLE"},         # secret name + aux
        "ch-0badf00d": {"name": "billing", "note": "reach ops at %s" % PHONE},  # PII in aux metadata
        "ch-baddca11": ["totally", "malformed", "meta"],                        # malformed meta, VALID id
        "ch-abcdef01": {"name": "ops-lane"},                                    # already clean
    }}

    manifest = store.merge_channels(legacy)
    reg = store.load_channels()["channels"]

    # PRESERVE-EVERYTHING for valid ids: every one is present after migration (scrub in place, never drop).
    for cid in legacy["channels"]:
        assert cid in reg, "migration dropped valid channel %r (must scrub in place, never skip)" % cid

    blob = json.dumps(reg, ensure_ascii=False)
    assert REFUSAL not in blob, "a refusal-term channel name landed raw in channels.json"
    assert SECRET not in blob, "a credential-shaped channel name landed raw in channels.json"
    assert "AKIAIOSFODNN7EXAMPLE" not in blob, "a credential in aux metadata landed raw"
    assert PHONE not in blob, "PII in aux metadata landed raw"
    assert "ops-lane" in blob, "a clean channel name must survive migration unchanged"

    mblob = json.dumps(manifest, ensure_ascii=False)
    assert REFUSAL not in mblob and SECRET not in mblob and PHONE not in mblob, \
        "the migration manifest echoed a raw unsafe value"


def test_hostile_key_is_quarantined_never_becomes_a_live_key():
    _reset()
    legacy = {"channels": {
        "ch-%s" % REFUSAL: {"name": "ops"},           # refusal term IN THE KEY
        "ch-x-%s" % SECRET: {"name": "ops2"},         # credential IN THE KEY
        "../../etc/passwd": {"name": "traverse"},     # path fragment as a KEY
        "ch-NOTHEXXX": {"name": "uppercase-nonhex"},  # right prefix, wrong charset
        "ch-deadbeef": {"name": "legit-lane"},        # the only genuine minted-shape id
    }}

    manifest = store.merge_channels(legacy)
    reg = store.load_channels()["channels"]

    # only the genuine ch-<hex> id becomes a live registry key; every malformed key is refused.
    assert "ch-deadbeef" in reg and reg["ch-deadbeef"]["name"] == "legit-lane"
    for bad in ("ch-%s" % REFUSAL, "ch-x-%s" % SECRET, "../../etc/passwd", "ch-NOTHEXXX"):
        assert bad not in reg, "a non-minted key became a live channel key: %r" % bad

    # the raw hostile key content rides NOWHERE - not the registry, not the returned manifest
    # (fence_output is refusal-only, so a keyed secret must be stopped at the source, and it is).
    both = json.dumps(reg, ensure_ascii=False) + json.dumps(manifest, ensure_ascii=False)
    assert REFUSAL not in both and SECRET not in both and "etc/passwd" not in both, \
        "a raw hostile channel KEY leaked into the registry or manifest"

    # quarantine is REPORTED (opaque, distinguishable fingerprint), never a silent drop.
    quarantined = [row for row in manifest if row[2].startswith("quarantined")]
    assert len(quarantined) == 4, manifest
    assert all(row[0].startswith("ch-quarantined-") for row in quarantined), quarantined
    # distinct hostile keys get distinct fingerprints (audit can tell them apart)
    assert len({row[0] for row in quarantined}) == 4, "fingerprints collided across distinct keys"


def test_silo_only_refusal_term_is_fenced_via_cfg():
    # A term fenced ONLY in the migrating silo's own config (threaded via the cfg arg) must still redact
    # from a migrated name - at merge time the silo's config.json is not yet under any projects/<key>/
    # that the workspace union reads.
    _reset()
    SILO_ONLY = "ZZKRAKENZZ"
    legacy = {"channels": {"ch-deadbeef": {"name": "lane-%s-tail" % SILO_ONLY}}}
    store.merge_channels(legacy, {"refusal": [SILO_ONLY]})
    blob = json.dumps(store.load_channels()["channels"], ensure_ascii=False)
    assert SILO_ONLY not in blob, "a silo-only refusal term leaked into a migrated channel name"
    assert "ch-deadbeef" in store.load_channels()["channels"], "the record itself must be preserved"


def test_meta_keys_are_allowlisted_never_imported_raw():
    """Round-4 3b: the meta-KEY half of the values-vs-keys sibling (RC5-D closed the top-level-key
    half). _scrub_record cleans string VALUES but passes dict KEYS verbatim, so a hostile key INSIDE
    a valid channel's meta (top-level, nested, or smuggled via a malformed list-of-dicts) must never
    ride into channels.json. The stored record is BUILT from the allowlisted minted fields
    ({name, created}); everything else drops, while the untouched legacy registry stays in the silo."""
    _reset()
    legacy = {"channels": {
        "ch-abcdef01": {"name": "realchan", "created": "2026-07-01T00:00:00Z",
                        SECRET: "v1",                      # credential AS A META KEY
                        REFUSAL: "v2",                     # refusal term AS A META KEY
                        "nested": {SECRET: "deep"}},       # nested dict smuggling a hostile KEY
        "ch-deadbe02": ["not", {SECRET: "in-list"}],       # malformed meta: the list-of-dicts sibling
    }}
    manifest = store.merge_channels(legacy)
    reg = store.load_channels()["channels"]

    # preserve-everything for VALID ids is unchanged: both records import ...
    assert "ch-abcdef01" in reg and "ch-deadbe02" in reg
    # ... but each record carries ONLY the minted shape
    assert set(reg["ch-abcdef01"].keys()) <= {"name", "created"}, reg["ch-abcdef01"]
    assert reg["ch-abcdef01"]["name"] == "realchan"
    assert reg["ch-abcdef01"]["created"] == "2026-07-01T00:00:00Z"
    blob = json.dumps(reg, ensure_ascii=False) + json.dumps(manifest, ensure_ascii=False)
    for hostile in (SECRET, REFUSAL, "deep", "in-list"):
        assert hostile not in blob, \
            "a hostile meta KEY (or a value riding beside one) leaked: %r" % hostile


def test_meta_allowlist_preserves_the_minted_shape_byte_for_byte():
    """False-positive direction: a legit product-minted record ({name, created} - the exact shape
    channel_id_for writes) merges with both fields preserved verbatim. The allowlist drops the
    unknown, never the minted.

    RC5-L: this fixture used to carry `"created": 1720000000`, an epoch int, while calling itself
    "the exact shape channel_id_for writes" - and channel_id_for has always written a STRING,
    time.strftime("%Y-%m-%dT%H:%M:%SZ"). The companion was guarding a shape the product never
    mints, which is precisely why it never noticed that any scalar rode through. A false-positive
    test that pins the wrong legitimate value cannot catch an over-permissive rule."""
    _reset()
    minted = time.strftime(store._CREATED_FORMAT, time.gmtime())
    legacy = {"channels": {"ch-77aa88bb": {"name": "ops-lane", "created": minted}}}
    manifest = store.merge_channels(legacy)
    reg = store.load_channels()["channels"]
    assert reg["ch-77aa88bb"] == {"name": "ops-lane", "created": minted}
    assert list(reg["ch-77aa88bb"]) == ["name", "created"], "stored in minted key order"
    assert "omitted" not in manifest[0][2], "a schema-exact record reports no refusal: %r" % (manifest,)


def test_conv_created_field_is_fenced_against_the_workspace_union_too():
    """CONVERGENCE MINOR (the 3b allowlist's oracle asymmetry): the name went through
    _safe_migrated_name (workspace union + this silo's terms) while the allowlisted `created` value
    went through _scrub_record with only cfg's refusal list - so a term fenced in a THIRD project
    rode into the workspace-level channels.json via `created` while the identical term in `name`
    was redacted. One field, two oracles. Both must fence against the same union.

    RC5-L makes this structurally impossible for THIS field rather than merely scrubbed: `created`
    now has a schema, and a canonical UTC stamp is 20 fixed characters with no room to carry a
    term at all - so the free-text value below is OMITTED before any oracle question arises. The
    test is kept (and strengthened to assert omission) because the asymmetry it found is a property
    of the fencing path, not of this one field: the next allowlisted field that admits free text
    inherits the same two-oracle hazard, and _channel_field_terms is what answers it."""
    _reset()                                   # workspace config carries REFUSAL
    legacy = {"channels": {
        "ch-aabbccdd": {"name": "ops", "created": "minted during %s" % REFUSAL},
    }}
    # cfg WITHOUT the term (a silo whose own config never listed it): only the workspace union,
    # which _reset wrote to STORE_ROOT/config.json, can catch it.
    manifest = store.merge_channels(legacy, {"refusal": [], "scrub_pii": True})
    blob = json.dumps(store.load_channels()["channels"], ensure_ascii=False)
    assert REFUSAL not in blob, "a refusal term rode into channels.json via the created field"
    # ...and it is REFUSED, not silently transformed into something that looks like a stamp
    assert "created" not in store.load_channels()["channels"]["ch-aabbccdd"]
    assert "created" in manifest[0][2] and "omitted" in manifest[0][2], manifest


def test_conv_dropped_nonschema_fields_are_reported_in_the_manifest():
    """The allowlist is a refusal like any other, so it must be VISIBLE: a legacy record carrying
    benign extra metadata reports the drop count (the untouched original stays in the silo). Only
    the COUNT - the field names are the untrusted part and never ride the manifest."""
    _reset()
    legacy = {"channels": {
        "ch-11223344": {"name": "ops", "created": "2026-07-01T00:00:00Z",
                        "note": "a benign hand-added field"},
        "ch-55667788": {"name": "clean", "created": "2026-07-02T00:00:00Z"},
    }}
    manifest = store.merge_channels(legacy)
    rows = {row[0]: row[2] for row in manifest}
    assert "dropped" in rows["ch-11223344"], rows
    assert "dropped" not in rows["ch-55667788"], "a schema-exact record reports no drop"
    assert "note" not in json.dumps(manifest), "the manifest must not echo untrusted field NAMES"


def test_rc5l_created_must_be_a_canonical_utc_timestamp():
    """RC5-L blocker 2: an allowlist entry is a key plus a TYPE plus a SCHEMA, never a key alone.

    The round-4 3b allowlist named `created` and stopped there, so the field had no type - and
    _scrub_record returns numbers and bools VERBATIM (_clean_reserved_scalar: "Numbers/bools are
    metadata verbatim"). A legacy registry could therefore land an arbitrary numeric payload in the
    workspace-level channels.json under a perfectly valid field name, past the scrub, in a file
    that is read for display. The headline vector is the review's own: a bare card number, which
    the scrub catches as a STRING and passes through untouched as an INT.

    Every vector below must be OMITTED and reported - never retained, and never silently rewritten
    into something that merely looks like a stamp (refusal and omission, not transformation)."""
    _reset()
    CARD = 4111111111111111                      # passes Luhn; caught by scrub only as a string
    vectors = {
        "ch-aa000001": CARD,                     # the review's repro: PII as a number
        "ch-aa000002": float(CARD),              # the float sibling
        "ch-aa000003": True,                     # bool (a dict key's truthiness smuggle)
        "ch-aa000004": 1720000000,               # epoch int - plausible, still not the minted shape
        "ch-aa000005": "1720000000",             # epoch as a string
        "ch-aa000006": "2026-13-45T99:99:99Z",   # right shape, impossible instant
        "ch-aa000007": "2026-07-19T12:34:56Z " + str(CARD),   # canonical prefix + payload
        "ch-aa000008": "2026-07-19T12:34:56+00:00",           # a real ISO form, not OUR form
        "ch-aa000009": REFUSAL,                  # a refusal term as the whole value
    }
    legacy = {"channels": {cid: {"name": "chan-%s" % cid[-2:], "created": v}
                           for cid, v in vectors.items()}}
    manifest = store.merge_channels(legacy)
    reg = store.load_channels()["channels"]
    rows = {row[0]: row[2] for row in manifest}

    for cid in vectors:
        assert cid in reg, "the RECORD still imports - refusal is per-field, never per-record"
        assert "created" not in reg[cid], \
            "a non-canonical `created` was RETAINED for %s: %r" % (cid, reg[cid])
        assert "created" in rows[cid] and "omitted" in rows[cid], \
            "the omission must be REPORTED for %s, not silent: %r" % (cid, rows[cid])
    blob = json.dumps(reg, ensure_ascii=False)
    assert str(CARD) not in blob, "the card number reached channels.json"
    assert REFUSAL not in blob, "a refusal term reached channels.json via created"

    # FALSE-POSITIVE COMPANION: the one legitimate shape survives byte-identical, and surrounding
    # whitespace normalizes rather than being refused (a hand-edited file is not an attack).
    _reset()
    minted = time.strftime(store._CREATED_FORMAT, time.gmtime())
    ok = {"channels": {"ch-bb000001": {"name": "clean", "created": minted},
                       "ch-bb000002": {"name": "padded", "created": "  %s  " % minted}}}
    man2 = {row[0]: row[2] for row in store.merge_channels(ok)}
    reg2 = store.load_channels()["channels"]
    assert reg2["ch-bb000001"]["created"] == minted, "the minted shape must survive unchanged"
    assert reg2["ch-bb000002"]["created"] == minted, "surrounding whitespace normalizes"
    for cid in ("ch-bb000001", "ch-bb000002"):
        assert "omitted" not in man2[cid], "a canonical stamp must not report a refusal: %r" % man2[cid]
