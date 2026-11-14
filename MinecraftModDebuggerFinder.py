#!/usr/bin/env python3
"""
mod_bisect.py - find which Minecraft mod(s) crash your game on launch.
Works with Modrinth App profiles (incl. imported .mrpack packs), Prism/MultiMC
instances, CurseForge, or a plain .minecraft folder. Fabric, Quilt, Forge, NeoForge.

  python mod_bisect.py                 # auto-finds Modrinth/Prism instances, lets you pick
  python mod_bisect.py "<path to mods folder>"
  python mod_bisect.py <mods> --keep somelib      # always keep jars containing "somelib"
  python mod_bisect.py <mods> --restore           # undo everything if a run was interrupted

Dependencies: each jar is looked up on Modrinth by file hash (only the hash is sent) to get
the exact required dependencies the mod author declared. Jar metadata is used as a fallback
for mods that aren't on Modrinth. Use --no-api to stay fully offline.

While it runs you can:
  * type  k  at the launch prompt to add a mod to the "always keep enabled" list
    (e.g. a dependency the tool missed). The search restarts with that mod kept on.
  * enable/disable mods by hand during a trial: the tool notices and asks what to do.

What it does to your files:
  * Disables a mod by renaming  x.jar -> x.jar.disabled   (same as the Modrinth App does).
  * Never deletes anything. Mods you had ALREADY disabled are never touched.
  * When finished (or on Ctrl+C / error) everything is restored. At the end it
    tells you the culprit(s) and asks if you want them left disabled.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

DIS = ".disabled"
STATE = ".mod_bisect_state.json"


# ----------------------------------------------------------------- finding mods folders
def find_mod_dirs():
    home = Path.home()
    appdata = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else None
    mac = home / "Library" / "Application Support"
    linux = home / ".local" / "share"

    modrinth_roots = [p for p in [
        appdata and appdata / "ModrinthApp" / "profiles",
        appdata and appdata / "com.modrinth.theseus" / "profiles",
        mac / "ModrinthApp" / "profiles",
        mac / "com.modrinth.theseus" / "profiles",
        linux / "ModrinthApp" / "profiles",
        linux / "com.modrinth.theseus" / "profiles",
    ] if p]
    prism_roots = [p for p in [
        appdata and appdata / "PrismLauncher" / "instances",
        appdata and appdata / "MultiMC" / "instances",
        mac / "PrismLauncher" / "instances",
        linux / "PrismLauncher" / "instances",
    ] if p]

    found = []
    for root in modrinth_roots:
        if root.is_dir():
            for prof in sorted(root.iterdir()):
                if (prof / "mods").is_dir():
                    found.append((f"Modrinth: {prof.name}", prof / "mods"))
    for root in prism_roots:
        if root.is_dir():
            for inst in sorted(root.iterdir()):
                for sub in ("minecraft", ".minecraft"):
                    if (inst / sub / "mods").is_dir():
                        found.append((f"Prism/MultiMC: {inst.name}", inst / sub / "mods"))
    for p in [
        appdata and appdata / ".minecraft" / "mods",
        mac / "minecraft" / "mods",
        home / ".minecraft" / "mods",
    ]:
        if p and p.is_dir():
            found.append(("Vanilla launcher .minecraft", p))
    return found


def pick_mods_dir():
    found = find_mod_dirs()
    if not found:
        sys.exit("Couldn't auto-find any instances. Pass the mods folder path explicitly.")
    print("Found these mod folders:")
    for i, (label, path) in enumerate(found, 1):
        print(f"  {i}. {label}\n       {path}")
    while True:
        a = input("Pick a number: ").strip()
        if a.isdigit() and 1 <= int(a) <= len(found):
            return found[int(a) - 1][1]


# ----------------------------------------------------------------- dependency reading
def read_meta(jar: Path):
    """Return (provided_ids, required_ids) for a mod jar. Best effort."""
    provides, requires = set(), set()
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                d = json.loads(z.read("fabric.mod.json").decode("utf-8", "ignore"), strict=False)
                provides.add(d.get("id", ""))
                provides.update(x for x in d.get("provides", []) if isinstance(x, str))
                requires.update((d.get("depends") or {}).keys())
            if "quilt.mod.json" in names:
                q = json.loads(z.read("quilt.mod.json").decode("utf-8", "ignore"), strict=False)
                ql = q.get("quilt_loader", {})
                provides.add(ql.get("id", ""))
                for x in ql.get("provides", []):
                    provides.add(x if isinstance(x, str) else x.get("id", ""))
                for x in ql.get("depends", []):
                    if isinstance(x, str):
                        requires.add(x)
                    elif not x.get("optional"):
                        requires.add(x.get("id", ""))
            for tname in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if tname in names and tomllib:
                    t = tomllib.loads(z.read(tname).decode("utf-8", "ignore"))
                    for m in t.get("mods", []):
                        provides.add(m.get("modId", ""))
                    for deps in (t.get("dependencies") or {}).values():
                        for dep in deps:
                            req = dep.get("mandatory", dep.get("type", "required") == "required")
                            if req is True or req == "required":
                                requires.add(dep.get("modId", ""))
    except Exception:
        pass
    provides.discard("")
    requires.discard("")
    return provides, requires


API = "https://api.modrinth.com/v2"
UA = "mod_bisect/1.1 (local crash-finding script)"
CACHE = Path.home() / ".mod_bisect_cache.json"


def sha1_file(path: Path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ModrinthAPI:
    """Tiny Modrinth client. Sends only file hashes / ids. Results are cached on disk."""

    def __init__(self):
        self.cache = {"files": {}, "versions": {}, "projects": {}}
        try:
            self.cache.update(json.loads(CACHE.read_text()))
        except Exception:
            pass

    def save(self):
        try:
            CACHE.write_text(json.dumps(self.cache))
        except Exception:
            pass

    def _req(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data,
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def lookup(self, files):
        """files: {name: Path}. Returns {name: {"project_id", "deps": [...]} or None if unknown}."""
        hashes = {n: sha1_file(p) for n, p in files.items()}
        need = sorted({h for h in hashes.values() if h not in self.cache["files"]})
        for i in range(0, len(need), 100):
            res = self._req("/version_files", {"hashes": need[i:i + 100], "algorithm": "sha1"})
            for h, v in res.items():
                self.cache["files"][h] = {
                    "project_id": v["project_id"],
                    "deps": [{"p": d.get("project_id"), "v": d.get("version_id"),
                              "t": d.get("dependency_type")} for d in v.get("dependencies", [])],
                }
        # Some dependencies only name a version id; resolve those to project ids.
        vids = sorted({d["v"] for h in hashes.values()
                       for d in (self.cache["files"].get(h) or {}).get("deps", [])
                       if not d["p"] and d["v"] and d["v"] not in self.cache["versions"]})
        for i in range(0, len(vids), 50):
            q = urllib.parse.quote(json.dumps(vids[i:i + 50]))
            for v in self._req(f"/versions?ids={q}"):
                self.cache["versions"][v["id"]] = v["project_id"]
        self.save()
        return {n: self.cache["files"].get(h) for n, h in hashes.items()}

    def project_id_of_dep(self, d):
        return d["p"] or self.cache["versions"].get(d["v"])

    def titles(self, pids):
        pids = sorted(set(pids))
        need = [p for p in pids if p not in self.cache["projects"]]
        try:
            for i in range(0, len(need), 50):
                q = urllib.parse.quote(json.dumps(need[i:i + 50]))
                for p in self._req(f"/projects?ids={q}"):
                    self.cache["projects"][p["id"]] = p["title"]
            self.save()
        except Exception:
            pass
        return {p: self.cache["projects"].get(p, p) for p in pids}


class DepGraph:
    def __init__(self, mods_dir, universe, use_api=True):
        self.dir = mods_dir
        self.providers = {}          # mod id -> jar (from jar metadata)
        self.requires = {}           # jar -> required mod ids (from jar metadata)
        self.api = ModrinthAPI() if use_api else None
        self.provider_by_proj = {}   # Modrinth project id -> jar
        self.api_req = {}            # jar -> required Modrinth project ids
        self.api_incompat = {}       # jar -> incompatible Modrinth project ids
        self.recognized = set()
        for name in universe:
            prov, req = read_meta(mods_dir / name)
            for p in prov:
                self.providers[p] = name
            self.requires[name] = req
        if self.api:
            self._load_api(list(universe))

    def _load_api(self, names):
        try:
            info = self.api.lookup({n: self.dir / n for n in names})
        except Exception as e:
            print(f"Note: couldn't reach Modrinth ({type(e).__name__}); using jar metadata only.")
            self.api = None
            return
        for n, v in info.items():
            if not v:
                continue
            self.recognized.add(n)
            self.provider_by_proj[v["project_id"]] = n
        for n, v in info.items():
            if not v:
                continue
            req, bad = set(), set()
            for d in v["deps"]:
                pid = self.api.project_id_of_dep(d)
                if not pid:
                    continue
                if d["t"] == "required":
                    req.add(pid)
                elif d["t"] == "incompatible":
                    bad.add(pid)
            self.api_req[n], self.api_incompat[n] = req, bad

    def report(self, universe):
        """Print what the lookup learned, including likely-missing dependencies."""
        if not self.api:
            return
        print(f"Modrinth recognised {len(self.recognized)} of {len(universe)} mods.")
        unknown = [m for m in universe if m not in self.recognized]
        if unknown:
            print("  Not found on Modrinth (jar metadata only): " + ", ".join(unknown[:8])
                  + (f" ... and {len(unknown) - 8} more" if len(unknown) > 8 else ""))
        missing = {}
        for n, pids in self.api_req.items():
            for pid in pids:
                if pid not in self.provider_by_proj:
                    missing.setdefault(pid, []).append(n)
        if missing:
            titles = self.api.titles(missing)
            print("\nHeads-up: these required libraries don't seem to be among your enabled mods"
                  "\n(they might be bundled inside a jar, or the mod may have an optional fallback):")
            for pid, users in missing.items():
                print(f"   - {titles[pid]}  (needed by {', '.join(users[:4])}"
                      + (f" +{len(users) - 4} more" if len(users) > 4 else "") + ")")
        for n, pids in self.api_incompat.items():
            for pid in pids:
                other = self.provider_by_proj.get(pid)
                if other:
                    print(f"\nHeads-up: {n} declares itself INCOMPATIBLE with {other}.")

    def add(self, mods_dir, name):
        prov, req = read_meta(mods_dir / name)
        for p in prov:
            self.providers[p] = name
        self.requires[name] = req
        if self.api:
            self._load_api([name])

    def close(self, names):
        """Add every mod that the given mods (transitively) require."""
        result, stack = set(names), list(names)
        while stack:
            cur = stack.pop()
            wanted = []
            for rid in self.requires.get(cur, ()):
                prov = self.providers.get(rid)
                if prov is None and rid.startswith("fabric"):  # Fabric API submodules (fallback)
                    prov = self.providers.get("fabric-api")
                wanted.append(prov)
            for pid in self.api_req.get(cur, ()):
                wanted.append(self.provider_by_proj.get(pid))
            for prov in wanted:
                if prov and prov not in result:
                    result.add(prov)
                    stack.append(prov)
        return result


# ----------------------------------------------------------------- enabling / disabling
def set_enabled(mods_dir: Path, enabled: set, universe):
    for name in universe:
        on, off = mods_dir / name, mods_dir / (name + DIS)
        if name in enabled:
            if off.exists() and not on.exists():
                off.rename(on)
        else:
            if on.exists() and not off.exists():
                on.rename(off)


def restore_from_state(mods_dir: Path):
    sp = mods_dir / STATE
    if not sp.exists():
        return False
    universe = json.loads(sp.read_text())["universe"]
    set_enabled(mods_dir, set(universe), universe)
    sp.unlink()
    return True


class RestartSearch(Exception):
    """Raised when the keep-list changed, so earlier results are no longer valid."""


def ask(prompt, choices):
    while True:
        a = input(prompt).strip().lower()
        if a in choices:
            return a
        print(f"  Please enter one of: {', '.join(sorted(choices))}")


# ----------------------------------------------------------------- the search
class Bisector:
    def __init__(self, mods_dir, universe, keep, graph):
        self.dir, self.universe, self.keep, self.graph = mods_dir, universe, set(keep), graph
        self.cache, self.tests = {}, 0
        self.manually_added = set()   # mods that were disabled at start but you enabled by hand

    # -- folder-drift detection -------------------------------------------------
    def actual_enabled(self):
        on = {n for n in self.universe if (self.dir / n).exists()}
        unknown = {p.name for p in self.dir.iterdir()
                   if p.name.endswith(".jar") and p.name not in self.universe}
        return on, unknown

    def adopt(self, names, why):
        """Add mods (e.g. manually enabled ones) to the universe and keep list."""
        for n in names:
            if n not in self.universe:
                self.manually_added.add(n)
                self.universe.append(n)
                self.graph.add(self.dir, n)
        self.universe.sort()
        self.keep |= set(names)
        (self.dir / STATE).write_text(json.dumps({"universe": self.universe}))
        print(f"\nNow always keeping enabled ({why}):")
        for n in sorted(names):
            print(f"   [kept] {n}")
        print("Earlier results are no longer valid, so the search restarts.")
        raise RestartSearch

    def handle_keep_command(self):
        sub = input("Part of the mod's filename to always keep enabled (blank = cancel): ").strip().lower()
        if not sub:
            return
        matches = [m for m in self.universe if sub in m.lower() and m not in self.keep]
        if not matches:
            print("  No un-kept mod matches that.")
            return
        for m in matches:
            print(f"   {m}")
        if ask(f"Keep these {len(matches)} always enabled and restart the search? [y/n]: ", {"y", "n"}) == "y":
            self.adopt(set(matches), "added by you")

    def check_drift(self, expected):
        """Compare the real folder to what we set. Return True if the result is usable."""
        on, unknown = self.actual_enabled()
        extra_on = (on - expected) | unknown
        extra_off = expected - on
        if not extra_on and not extra_off:
            return True
        print("\n!! The mods folder doesn't match what this trial was supposed to use:")
        for m in sorted(extra_on):
            print(f"   + enabled by you: {m}")
        for m in sorted(extra_off):
            print(f"   - disabled by you: {m}")
        opts = {"r", "i"}
        menu = "[r]edo this trial with the correct set / [i]gnore (trust the result as-is)"
        if extra_on:
            opts.add("a")
            menu = "[a]lways keep the enabled ones from now on (restarts search) / " + menu
        c = ask(f"{menu}: ", opts)
        if c == "a":
            self.adopt(extra_on, "you enabled them manually")
        return c == "i"  # 'r' -> False -> caller redoes the trial

    # -- one trial --------------------------------------------------------------
    def crashes(self, suspects) -> bool:
        key = frozenset(suspects)
        if key in self.cache:
            return self.cache[key]
        counted = False
        while True:
            active = self.graph.close(self.keep | key)
            set_enabled(self.dir, active, self.universe)
            if not counted:
                self.tests += 1
                counted = True
            extra = len(active) - len(self.keep | key)
            print(f"\n--- Test #{self.tests}: {len(active)} mods enabled "
                  f"({len(key)} suspects, {len(self.keep)} kept"
                  f"{f', {extra} auto-added as dependencies' if extra else ''}) ---")
            if len(key) <= 12:
                for m in sorted(key):
                    print(f"   + {m}")
            while True:
                a = ask("Launch the game now. Did it CRASH? "
                        "[c]rash / [o]k / [l]ist enabled / [k]eep a mod always on / [q]uit: ",
                        {"c", "o", "l", "k", "q"})
                if a == "l":
                    for m in sorted(active):
                        print(f"   + {m}")
                elif a == "k":
                    self.handle_keep_command()   # may raise RestartSearch
                elif a == "q":
                    raise KeyboardInterrupt
                else:
                    break
            if self.check_drift(active):
                break
        self.cache[key] = (a == "c")
        return self.cache[key]

    def minimize(self, suspects, fixed):
        """Invariant: fixed+suspects crashes. Return a minimal crashing subset of suspects."""
        suspects = list(suspects)
        if len(suspects) <= 1:
            return suspects
        mid = len(suspects) // 2
        a, b = suspects[:mid], suspects[mid:]
        if self.crashes(fixed + a):
            return self.minimize(a, fixed)
        if self.crashes(fixed + b):
            return self.minimize(b, fixed)
        print("\n  (Neither half crashed alone -> looks like a mod conflict. Narrowing both halves...)")
        ra = self.minimize(a, fixed + b)
        rb = self.minimize(b, fixed + ra)
        return ra + rb


def search(bis, graph):
    """Run the full search. Returns list of culprit groups, or None if nothing to find."""
    suspects = [m for m in bis.universe if m not in bis.keep]
    print(f"\n{len(bis.universe)} mods. Always kept: {len(bis.keep)}. Suspects: {len(suspects)}.")
    print("\nStep 1: confirm the crash with ALL mods enabled.")
    if not bis.crashes(suspects):
        print("Launched fine with everything enabled. Nothing to find!")
        return None
    if bis.keep:
        print("\nStep 2: confirm the always-kept mods alone don't crash.")
        if bis.crashes([]):
            print("Crashes even with only the kept mods. Use a smaller keep list.")
            return None
    groups, remaining = [], suspects
    while True:
        found = bis.minimize(remaining, [])
        groups.append(found)
        print("\n=== Found a crash cause ===")
        for m in found:
            print(f"   * {m}")
        if len(found) > 1:
            print("   (crashes only when these are used TOGETHER; removing any one may fix it)")
        pulled = graph.close(found) - set(found)
        if pulled:
            print("   Note: these were also loaded as required dependencies, in case the real\n"
                  "   cause is one of them: " + ", ".join(sorted(pulled)))
        remaining = [m for m in remaining if m not in found]
        if not remaining:
            break
        print("\nChecking if anything else still crashes with those removed...")
        if not bis.crashes(remaining):
            break
        print("Still crashing; searching for the next culprit.")
    return groups


def main():
    ap = argparse.ArgumentParser(description="Find the mod crashing Minecraft on launch.")
    ap.add_argument("mods_dir", nargs="?", type=Path)
    ap.add_argument("--keep", action="append", default=[],
                    help="substring of jar names to ALWAYS keep enabled. Repeatable.")
    ap.add_argument("--no-api", action="store_true", help="don't query Modrinth (fully offline)")
    ap.add_argument("--no-deps", action="store_true", help="don't auto-enable required dependencies")
    ap.add_argument("--restore", action="store_true", help="re-enable mods from an interrupted run")
    args = ap.parse_args()

    mods_dir = args.mods_dir or pick_mods_dir()
    if not mods_dir.is_dir():
        sys.exit(f"Not a folder: {mods_dir}")

    if restore_from_state(mods_dir):
        print("Restored mods from an earlier interrupted run.")
        if args.restore:
            return
    elif args.restore:
        print("Nothing to restore.")
        return

    universe = sorted(p.name for p in mods_dir.iterdir() if p.name.endswith(".jar"))
    if not universe:
        sys.exit("No enabled .jar mods found in that folder.")
    already_off = [p.name for p in mods_dir.iterdir() if p.name.endswith(".jar" + DIS)]
    if already_off:
        print(f"Note: {len(already_off)} mods you'd already disabled will be left alone.")

    (mods_dir / STATE).write_text(json.dumps({"universe": universe}))

    print("Reading dependencies...")
    graph = DepGraph(mods_dir, universe, use_api=not (args.no_api or args.no_deps))
    if args.no_deps:
        graph.requires, graph.api_req = {}, {}
    else:
        graph.report(universe)
        if tomllib is None:
            print("Note: Python <3.11 without 'tomli': Forge/NeoForge jar-metadata dependencies "
                  "won't be read (Modrinth data still works).")

    keep = {m for m in universe if any(k.lower() in m.lower() for k in args.keep)}
    for m in sorted(keep):
        print(f"   [kept] {m}")

    bis = Bisector(mods_dir, universe, keep, graph)
    groups = []
    leave_disabled = set()

    try:
        while True:
            try:
                groups = search(bis, graph)
                break
            except RestartSearch:
                bis.cache.clear()
        if not groups:
            return

        print("\n########## RESULT ##########")
        for i, g in enumerate(groups, 1):
            print(f"Culprit {i}: {' + '.join(g)}")
        print(f"({bis.tests} launches for {len(universe)} mods.)")

        if ask("\nLeave the culprit(s) disabled so the game launches? [y/n]: ", {"y", "n"}) == "y":
            for g in groups:
                if len(g) == 1:
                    leave_disabled.add(g[0])
                else:
                    print("\nThese conflict with each other; disabling one is enough:")
                    for i, m in enumerate(g, 1):
                        print(f"  {i}. {m}")
                    c = ask("Which to disable? (number, or 'a' for all): ",
                            {str(i) for i in range(1, len(g) + 1)} | {"a"})
                    leave_disabled.update(g if c == "a" else [g[int(c) - 1]])

    except KeyboardInterrupt:
        print("\nAborted.")
    finally:
        set_enabled(mods_dir, set(universe) - leave_disabled, universe)
        sp = mods_dir / STATE
        if sp.exists():
            sp.unlink()
        print("\nAll your mods are back to how they were" + ("," if leave_disabled else "."))
        if bis.manually_added:
            print("NOTE: these were disabled before, but you enabled them by hand, so they're left ENABLED:")
            for m in sorted(bis.manually_added):
                print(f"   + {m}")
        if leave_disabled:
            print("EXCEPT these, which I left DISABLED (renamed to .jar.disabled):")
            for m in sorted(leave_disabled):
                print(f"   - {m}")
            print("Re-enable them by renaming back, or toggling them on in the Modrinth App.")


if __name__ == "__main__":
    main()
