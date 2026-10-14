"""Regression tests for the one-version-per-mod policy in nexus_core.deployer.

A manifest key is an archive stem, so two versions of one mod used to be two unrelated
entries: deploying v1.1.0 added a key and left v1.0.4's key AND files in place, both
claiming the same paths. Whichever ran last decided what was on disk — that is what
downgraded RED4ext to 1.29.1 and what left DisclosedHiddenGems with both `Core/` and
`core/`, declaring every type twice and aborting the whole redscript compile.

The counter-risk is over-matching: one Nexus mod-id routinely hosts many separately
downloadable files (mod 27740 ships one fridge per apartment, all sharing an id, a name
and a version string). Retiring those as "older versions" would uninstall real mods. So
identity only nominates candidates; overlapping paths decide. Both directions are pinned
here. Pure stdlib — no PySide6, no network.
"""
import io
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def test_identity_groups_versions_of_one_mod():
    a = D.mod_identity("Disclosed Hidden Gems-29021-1-0-4-1778545650")
    b = D.mod_identity("Disclosed Hidden Gems 29021 1.1.0 2026-07-13T00-22Z NEnvE3ZGK")
    assert a == b == ("29021", "disclosedhiddengems")


def test_identity_folds_punctuation_drift():
    """The same download listed twice, differing only by a double space in the name."""
    assert (D.mod_identity("Use Your Fridge -  Las Palapas Tower-27740-1-0-1772069597")
            == D.mod_identity("Use Your Fridge - Las Palapas Tower-27740-1-0-1772069597"))


def test_identity_reads_the_real_mod_id_not_a_number_in_the_name():
    # framework_mod_id() reads this as mod '01'; the id is 11077.
    assert D.mod_identity("Additional Stuff 01-11077-2-29-1766316494")[0] == "11077"
    # a 'v'-prefixed version must still resolve
    assert D.mod_identity("Audioware-12001-v1-9-2-1775355328") == ("12001", "audioware")
    # not a Nexus name at all -> no identity, never grouped on a coincidence
    assert D.mod_identity("e6_07_60_e607608f-8bd3-410f-b9d3-0fa262afa3e2") is None


def test_identity_parses_version_strings_containing_letters():
    """Nexus version strings carry revision letters ('1.4d', '3.2.1b1'). A version group
    that only accepted digits left these two mods unparsed, so their duplicate installs
    were invisible to the policy."""
    assert (D.mod_identity("Undermine Quest Series-17186-1-4d-1765164582")
            == D.mod_identity("Undermine Quest Series 17186 1.4f 2026-06-21T08-07Z KRvfRxCC2")
            == ("17186", "underminequestseries"))
    assert (D.mod_identity("NightCityVisualsNulled-13524-3-2-1b1-1771410361")
            == ("13524", "nightcityvisualsnulled"))


def test_identity_does_not_split_on_a_number_inside_the_name():
    """The version group may hold letters but must START with a digit — otherwise the
    first '-<digits>-' in a name wins and the id is read from the wrong place."""
    assert D.mod_identity("Mod-2-Something-12345-1-0-1770000000") == ("12345", "mod2something")


def test_sibling_files_of_one_mod_page_are_not_versions():
    """Mod 27740's fridges: same id, same name, same version string, disjoint files.
    Grouping by identity alone would retire nineteen working mods."""
    mods = {
        "Use Your Fridge-27740-1-0-1771724274": ["archive/pc/mod/japantown_fridge.archive"],
        "Use Your Fridge-27740-1-0-1771724348": ["archive/pc/mod/heywood_fridge.archive"],
        "Use Your Fridge-27740-1-0-1771767336": ["archive/pc/mod/judy_lakehouse_fridge.archive"],
    }
    assert D.find_version_duplicates(mods) == []


def test_overlapping_paths_make_it_a_version_duplicate():
    mods = {
        "Stealthrunner-7616-1-10-0-1769268397": ["r6/scripts/SR/a.reds", "r6/scripts/SR/gone.reds"],
        "Stealthrunner 7616 1.11.1 2026-06-14T16-57Z lcXrcZgk3": ["r6/scripts/SR/a.reds",
                                                                  "r6/scripts/SR/new.reds"],
    }
    groups = D.find_version_duplicates(mods)
    assert len(groups) == 1
    assert groups[0]["keep"]["ver"] == "1.11.1"
    assert [d["entry"]["ver"] for d in groups[0]["drops"]] == ["1.10.0"]


def test_case_only_path_difference_still_counts_as_overlap():
    """Codeware's two builds owned `Scripts/` and `scripts/` — one path to the game through
    Wine, two to this filesystem. Comparing exactly would call them disjoint and miss the
    collision that strips `class JobQueue` from the compile."""
    mods = {
        "Codeware-7780-1-19-0-1769434058": ["red4ext/plugins/Codeware/scripts/Codeware.Global.reds"],
        "Codeware-7780-1-20-3-1777845006": ["red4ext/plugins/Codeware/Scripts/Codeware.Global.reds"],
    }
    groups = D.find_version_duplicates(mods)
    assert len(groups) == 1 and groups[0]["keep"]["ver"] == "1.20.3"


def _write(root, rel, data="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def test_retiring_a_version_archives_only_its_leftovers(tmp_path):
    """The old version's stray files are what leave a second case-variant of a script
    folder on disk; the shared ones belong to the kept version and must not be touched."""
    game = tmp_path
    _write(game, "r6/scripts/DHG/core/main.reds", "old")       # only the old version has this
    _write(game, "r6/scripts/DHG/Core/main.reds", "new")       # shared path, new casing
    old = "Disclosed Hidden Gems-29021-1-0-4-1778545650"
    new = "Disclosed Hidden Gems 29021 1.1.0 2026-07-13T00-22Z NEnvE3ZGK"
    mods = {
        old: ["r6/scripts/DHG/core/main.reds", "r6/scripts/DHG/Core/main.reds"],
        new: ["r6/scripts/DHG/Core/main.reds"],
    }
    with contextlib.redirect_stdout(io.StringIO()):
        acts = D.dedupe_mod_versions(game, mods, dry_run=False)
    assert old not in mods and new in mods
    assert acts[0]["orphans_moved"] == 1
    assert not (game / "r6/scripts/DHG/core/main.reds").exists()   # the stray casing is gone
    # and the emptied folder goes with it, or it keeps registering as a case-collision
    assert not (game / "r6/scripts/DHG/core").exists()
    assert (game / "r6/scripts/DHG/Core/main.reds").read_text() == "new"   # kept, untouched
    assert list((game / D.DUP_BACKUP_DIRNAME).rglob("main.reds"))          # reversible


def test_retiring_never_deletes_a_file_another_installed_mod_owns(tmp_path):
    game = tmp_path
    _write(game, "r6/scripts/shared.reds", "s")
    mods = {
        "Thing-100-1-0-1700000000": ["r6/scripts/shared.reds", "r6/scripts/a.reds"],
        "Thing-100-2-0-1780000000": ["r6/scripts/a.reds"],
        "Other Mod-200-1-0-1700000000": ["r6/scripts/shared.reds"],
    }
    with contextlib.redirect_stdout(io.StringIO()):
        D.dedupe_mod_versions(game, mods, dry_run=False)
    assert "Thing-100-1-0-1700000000" not in mods
    assert (game / "r6/scripts/shared.reds").exists()   # still owned by 'Other Mod'


def test_dry_run_mutates_nothing(tmp_path):
    game = tmp_path
    _write(game, "r6/scripts/old_only.reds", "old")
    mods = {
        "Thing-100-1-0-1700000000": ["r6/scripts/old_only.reds", "r6/scripts/a.reds"],
        "Thing-100-2-0-1780000000": ["r6/scripts/a.reds"],
    }
    before = {k: list(v) for k, v in mods.items()}
    with contextlib.redirect_stdout(io.StringIO()):
        acts = D.dedupe_mod_versions(game, mods, dry_run=True)
    assert mods == before
    assert acts[0]["orphans_moved"] == 1               # reported, not performed
    assert (game / "r6/scripts/old_only.reds").exists()


def test_drop_superseded_drops_older_even_when_it_has_extra_files(tmp_path, monkeypatch):
    """The old rule kept such an archive as 'ambiguous' and deployed BOTH versions —
    the direct cause of two case-variant script folders coexisting."""
    old = tmp_path / "Disclosed Hidden Gems-29021-1-0-4-1778545650.zip"
    new = tmp_path / "Disclosed Hidden Gems-29021-1-1-0-1783902120.zip"
    listings = {old.name: {"r6/scripts/dhg/core/main.reds", "r6/scripts/dhg/a.reds"},
                new.name: {"r6/scripts/dhg/a.reds", "r6/scripts/dhg/new.reds"}}
    monkeypatch.setattr(D, "archive_listing", lambda p: listings[p.name])
    kept, superseded, ambiguous = D.drop_superseded([old, new])
    assert kept == [new]
    assert [o.name for o, _ in superseded] == [old.name]
    assert ambiguous == []


def test_drop_superseded_keeps_disjoint_sibling_downloads(tmp_path, monkeypatch):
    a = tmp_path / "Use Your Fridge-27740-1-0-1771724274.zip"
    b = tmp_path / "Use Your Fridge-27740-1-0-1771724348.zip"
    listings = {a.name: {"archive/pc/mod/japantown_fridge.archive"},
                b.name: {"archive/pc/mod/heywood_fridge.archive"}}
    monkeypatch.setattr(D, "archive_listing", lambda p: listings[p.name])
    kept, superseded, ambiguous = D.drop_superseded([a, b])
    assert set(kept) == {a, b}            # both installed
    assert superseded == [] and ambiguous == []


def test_drop_superseded_is_ambiguous_when_a_listing_cannot_be_read(tmp_path, monkeypatch):
    old = tmp_path / "Thing-100-1-0-1700000000.zip"
    new = tmp_path / "Thing-100-2-0-1780000000.zip"
    monkeypatch.setattr(D, "archive_listing", lambda p: set())   # bsdtar failed
    kept, superseded, ambiguous = D.drop_superseded([old, new])
    assert set(kept) == {old, new}        # refuses to guess
    assert len(ambiguous) == 1 and superseded == []


def test_fix_versions_end_to_end_and_health_reports_it(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    _write(game, "r6/scripts/old_only.reds", "old")
    _write(game, "r6/scripts/a.reds", "a")
    D.save_manifest(game, {
        "Thing-100-1-0-1700000000": ["r6/scripts/old_only.reds", "r6/scripts/a.reds"],
        "Thing-100-2-0-1780000000": ["r6/scripts/a.reds"],
    })
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_version_health(D.load_manifest(game)["mods"])
    assert n == 1 and "[versions] 1 mod(s) installed at more than one version" in buf.getvalue()

    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_versions(game, dry_run=False)
    mods = D.load_manifest(game)["mods"]
    assert set(mods) == {"Thing-100-2-0-1780000000"}
    assert not (game / "r6/scripts/old_only.reds").exists()
    assert (game / "r6/scripts/a.reds").exists()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert D.report_version_health(mods) == 0
    assert "[versions] 0 mod(s)" in buf.getvalue()


if __name__ == "__main__":
    import inspect
    import tempfile

    class _MP:
        """Minimal monkeypatch stand-in for the no-pytest runner."""
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo): setattr(obj, name, val)

    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        params = inspect.signature(fn).parameters
        kw = {}
        if "tmp_path" in params: kw["tmp_path"] = Path(tempfile.mkdtemp())
        mp = _MP()
        if "monkeypatch" in params: kw["monkeypatch"] = mp
        try: fn(**kw)
        finally: mp.undo()
        print("PASS", name); n += 1
    print(f"{n} version-policy tests passed")
