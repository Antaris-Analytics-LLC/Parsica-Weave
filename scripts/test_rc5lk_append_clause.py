#!/usr/bin/env python3
"""The append gateway must refuse a redirect BY ITS OWN CLAUSE, not by the OS's accident.

_safe_append_open ran its reparse check inside `except FileExistsError:`. On Windows an
O_CREAT|O_EXCL create against a name occupied by a directory JUNCTION fails with EACCES
(PermissionError), never FileExistsError - so control skipped the branch holding the containment
check and landed in the generic OSError handler. The call still refused, because opening a
directory for writing fails at the OS level, so every test that only asked "did it raise?"
passed, and deleting the containment check would have changed nothing observable. The operating
system was silently holding the property the check is named for.

That is why these tests assert the REFUSAL SENTENCE. A junction test that checked only the
exception type, or only that the filename appeared in the message (it appears in the OS's message
too), cannot tell the two apart and would have stayed green through the whole defect.
"""
import os
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="appendclause_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

WINDOWS = os.name == "nt"

CONTAINMENT = "is a link or redirect"          # the gateway's own sentence
GENERIC = "could not be created safely"        # the OS-accident fallback


def _redirect(link, target):
    """Plant a directory redirect at `link`. Confirmed present before returning True - a failed
    mklink must never read as "the guard refused it"."""
    os.makedirs(str(target), exist_ok=True)
    if WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    else:
        try:
            os.symlink(str(target), str(link))
        except OSError:
            return False
    return store.is_reparse_point(link)


def test_a_directory_redirect_is_refused_by_the_containment_clause(tmp_path):
    """The load-bearing case. Pre-fix this refused with the GENERIC message on Windows, because
    the create failed EACCES and never reached the reparse check."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    outside = tmp_path / "outside"
    target = root / "journal.md"
    if not _redirect(target, outside):
        pytest.skip("directory redirects unavailable here")
    with pytest.raises(store.MigrationRefused) as exc:
        store._safe_append_open(target, str(root))
    assert CONTAINMENT in str(exc.value), \
        "refused, but by the OS rather than by the gateway's containment clause: %s" % exc.value


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable on this platform")
def test_a_file_symlink_out_of_the_root_is_refused_by_the_same_clause(tmp_path):
    """The sibling shape: a FILE symlink pointing outside. On POSIX this is the ordinary case;
    on Windows it needs privilege, hence the skip when it cannot be created."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    outside = tmp_path / "outside.md"
    outside.write_text("pre-existing\n", encoding="utf-8")
    link = root / "lessons.md"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError) as e:
        pytest.skip("cannot create a file symlink here: %s" % e)
    with pytest.raises(store.MigrationRefused) as exc:
        store._safe_append_open(link, str(root))
    assert CONTAINMENT in str(exc.value)
    assert outside.read_text(encoding="utf-8") == "pre-existing\n", "appended through the link"


# --- false-positive companions: the legit defaults must still PASS -------------

def test_first_time_create_still_works(tmp_path):
    root = tmp_path / "ws"
    os.makedirs(str(root))
    fd = store._safe_append_open(root / "new.md", str(root))
    os.write(fd, b"hello\n")
    os.close(fd)
    assert (root / "new.md").read_text(encoding="utf-8") == "hello\n"


def test_append_to_an_existing_ordinary_file_still_works(tmp_path):
    """This is the path that genuinely goes through FileExistsError. Widening the handler to any
    OSError must not have cost it - if this broke, every append in the store broke."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    p = root / "journal.md"
    p.write_text("first\n", encoding="utf-8")
    fd = store._safe_append_open(p, str(root))
    os.write(fd, b"second\n")
    os.close(fd)
    assert p.read_text(encoding="utf-8") == "first\nsecond\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
