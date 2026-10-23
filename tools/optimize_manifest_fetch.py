#!/usr/bin/env python3
"""Avoid fetching an extra upstream snapshot for explicitly pinned repo projects.

repo 2.16 project.py _RemoteFetch appends both the commit and upstream to a
depth-limited fetch. Removing upstream from an explicitly pinned project keeps
its checkout unchanged. Branch/tag projects retain their original upstream.
Unknown manifest constructs are left completely untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET

from set_kernel_ref import _load_manifests


COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
ROOT_NODES = frozenset(("remote", "default", "project", "include", "superproject",
                        "notice", "repo-hooks", "contactinfo", "manifest-server"))
PROJECT_NODES = frozenset(("linkfile", "copyfile", "annotation"))


class UnsupportedManifest(ValueError):
    """A layout needs explicit review before its fetch settings can change."""


class ManifestWriteError(RuntimeError):
    """Rollback could not restore every manifest; keep original backups."""


def _layout(manifests):
    defaults, projects = [], []
    for path, tree in manifests:
        for node in tree.getroot():
            if not isinstance(node.tag, str):  # Preserve XML comments.
                continue
            if node.tag not in ROOT_NODES:
                raise UnsupportedManifest(f"unsupported manifest element: {node.tag}")
            if node.tag != "project" and any(isinstance(child.tag, str) for child in node):
                raise UnsupportedManifest("nested manifest layout requires separate review")
            if node.tag == "remote" and "revision" in node.attrib:
                raise UnsupportedManifest("remote-level revision requires separate review")
            if node.tag == "default":
                defaults.append(node)
            elif node.tag == "include":
                if set(node.attrib) - {"name", "groups"}:
                    raise UnsupportedManifest("include attributes require separate review")
            elif node.tag == "project":
                if not node.get("name"):
                    raise UnsupportedManifest("project without a name")
                if any(isinstance(child.tag, str) and child.tag not in PROJECT_NODES for child in node.iter() if child is not node):
                    raise UnsupportedManifest("nested project or unsupported project child")
                projects.append(node)
    if len(defaults) > 1:
        raise UnsupportedManifest("multiple default elements require separate review")
    return defaults[0] if defaults else None, projects


def _semantics(default, projects):
    default_revision = default.get("revision", "") if default is not None else ""
    default_upstream = default.get("upstream", "") if default is not None else ""
    return [(project.get("revision") or default_revision,
             project.get("upstream") or default_upstream,
             {key: value for key, value in project.attrib.items() if key != "upstream"},
             tuple(ET.tostring(child) for child in project)) for project in projects]


def _write_all(changes):
    # Validate/serialize everything before touching a manifest. Each rename is
    # atomic; staged original copies also allow rollback if a later rename fails.
    staged, originals, replaced = [], [], []
    retained = set()
    try:
        for destination, content, original in changes:
            pair = []
            for prefix, payload in ((".gki-fetch-", content), (".gki-fetch-original-", original)):
                fd, name = tempfile.mkstemp(prefix=prefix, suffix=".xml", dir=destination.parent)
                temporary = Path(name)
                pair.append(temporary)
                # Register immediately so serialization or permission failures
                # cannot leave temporary files in the manifest repository.
                (staged if len(pair) == 1 else originals).append((temporary, destination))
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                temporary.chmod(stat.S_IMODE(destination.stat().st_mode))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            replaced.append(destination)
    except OSError:
        backups = {destination: temporary for temporary, destination in originals}
        for destination in reversed(replaced):
            try:
                os.replace(backups[destination], destination)
            except OSError:
                retained.add(backups[destination])
        if retained:
            raise ManifestWriteError(
                "Manifest write and rollback failed; original backups retained: "
                + ", ".join(str(path) for path in sorted(retained))) from None
        raise
    finally:
        active_error = sys.exc_info()[1]
        cleanup_failed = []
        for temporary, _ in staged + originals:
            if temporary not in retained:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed.append(temporary)
        if cleanup_failed and not isinstance(active_error, ManifestWriteError):
            # A filesystem cleanup failure must not replace the fatal rollback
            # exception above with an OSError that optional skip would swallow.
            raise ManifestWriteError(
                "Manifest temporary cleanup failed; inspect retained files before building: "
                + ", ".join(str(path) for path in cleanup_failed)) from None


def optimize_manifest_fetch(manifest_root: Path | str) -> dict:
    result = {"status": "unchanged", "optimized_projects": 0,
              "preserved_upstreams": 0, "changed_files": 0}
    try:
        supplied = Path(manifest_root).absolute()
        # _load_manifests validates every include component. Validate the root
        # as well, so a symlink cannot redirect this optional optimization.
        if any(path.is_symlink() or getattr(path, "is_junction", lambda: False)()
               for path in (supplied, *supplied.parents)):
            raise UnsupportedManifest("symlinked manifest root")
        root = supplied.resolve(strict=True)
        manifests = _load_manifests(root)
        default, projects = _layout(manifests)
        original_trees = [(path, copy.deepcopy(tree)) for path, tree in manifests]
        before = _semantics(default, projects)
        candidates = [index for index, (project, state) in enumerate(zip(projects, before))
                      if COMMIT.fullmatch(project.get("revision", "")) and state[1]]
        if not candidates:
            return result
        default_upstream = default.get("upstream", "") if default is not None else ""
        preserved = 0
        if default is not None:
            default.attrib.pop("upstream", None)
        for index, project in enumerate(projects):
            if COMMIT.fullmatch(project.get("revision", "")):
                project.attrib.pop("upstream", None)
            elif not project.get("upstream") and default_upstream:
                project.set("upstream", default_upstream)
                preserved += 1
        after = _semantics(default, projects)
        for index, (old, new) in enumerate(zip(before, after)):
            fixed = COMMIT.fullmatch(projects[index].get("revision", ""))
            expected_upstream = "" if fixed else old[1]
            if old[0] != new[0] or old[2:] != new[2:] or new[1] != expected_upstream:
                raise UnsupportedManifest("manifest checkout semantics could not be preserved")
        changes = []
        for (path, tree), (_, original_tree) in zip(manifests, original_trees):
            if ET.tostring(tree.getroot()) != ET.tostring(original_tree.getroot()):
                changes.append((path, ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True), path.read_bytes()))
        _write_all(changes)
        result.update(status="optimized", optimized_projects=len(candidates),
                      preserved_upstreams=preserved, changed_files=len(changes))
    except (UnsupportedManifest, ValueError, OSError, ET.ParseError) as error:
        # This is optional acceleration, not a reason to break an otherwise
        # valid upstream build. Unsupported layouts receive no partial edits.
        result.update(status="skipped", reason=str(error))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = optimize_manifest_fetch(args.manifest_root)
    except ManifestWriteError as error:
        print("::error::" + str(error))
        return 1
    print("Manifest fetch optimization: " + json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
