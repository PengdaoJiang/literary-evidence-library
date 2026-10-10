"""Complete-release publication with explicit compare-and-swap and recovery.

Candidates are immutable and inert. Only current.json selects effective input;
all protected legacy files stay byte-for-byte intact. Hold state is independent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from literary.tools import alignment as a, alignment_release as r
from literary.tools.alignment_review import validate_review_gate
from literary.tools.alignment_transaction import writer_lock, atomic_write


def _fresh(context):
    fresh = a.load_context(context.root, context.alignment_root)
    a._require(fresh.manifest_file_sha256 == context.manifest_file_sha256, "source/structure manifest changed after context load")
    return fresh


def _source_inventory(context):
    """Actual source, index and chunk bytes, not a cached Context assertion."""
    manifest = context.manifest
    paths = {manifest["source"]["path"], manifest["topology"]["chapter_index_path"], manifest["topology"]["chunk_index_path"]}
    paths.update(row["path"] for chunks in context.chunks_by_chapter.values() for row in chunks)
    return {name: r.sha(a._repo_path(context.root, name).read_bytes()) for name in sorted(paths)}


class Publisher:
    def __init__(self, root: Path):
        self.root = root
        self.store = r.Store(root)
        self.current_path = a._repo_path(root, "releases/current.json")
        self.registration_path = a._repo_path(root, "releases/registration.json")

    def _registration(self):
        value = a.load_json(self.registration_path)
        a._require(isinstance(value, dict) and set(value) == {"schema_version", "record_type", "baseline_release_sha256", "initial_pointer_sha256", "record_sha256"}
                   and value.get("schema_version") == 1 and value.get("record_type") == "alignment_registration"
                   and a._is_sha256(value.get("baseline_release_sha256")) and a._is_sha256(value.get("initial_pointer_sha256"))
                   and value.get("record_sha256") == a.record_sha256(value), "registration schema/hash mismatch")
        a._require(self.registration_path.read_bytes() == r.json_bytes(value), "immutable registration bytes changed")
        return value

    @staticmethod
    def _activation_shape(value):
        expected = {"schema_version", "record_type", "sequence", "action", "release_sha256",
                    "previous_pointer_sha256", "reason", "authority_fingerprint", "record_sha256"}
        a._require(isinstance(value, dict) and set(value) == expected and value.get("schema_version") == 1 and
                   value.get("record_type") == "alignment_current" and type(value.get("sequence")) is int and value["sequence"] >= 0 and
                   value.get("action") in {"register", "activate", "rollback"} and
                   isinstance(value.get("reason"), str) and bool(value["reason"].strip()) and
                   a._is_sha256(value.get("authority_fingerprint")) and a._is_sha256(value.get("release_sha256")) and
                   (value.get("previous_pointer_sha256") is None or a._is_sha256(value["previous_pointer_sha256"])),
                   "activation schema/reason/fingerprint invalid")

    def pointer(self):
        if not self.current_path.exists():
            a._require(not self.registration_path.exists(), "registered current pointer missing; explicit recovery required")
            history = self.store.root / "activations"
            a._require(not history.exists() or (history.is_dir() and not any(history.iterdir())),
                       "activation history remains without current/registration; explicit recovery required")
            return None
        a._require(self.registration_path.is_file(), "current has no immutable registration")
        current = a.load_json(self.current_path)
        registration = self._registration()
        a._require(isinstance(current, dict) and isinstance(registration, dict), "activation and registration must be objects")
        a._require(self.current_path.read_bytes() == r.json_bytes(current), "current pointer byte format changed")
        a._require(registration.get("record_sha256") == a.record_sha256(registration), "registration hash mismatch")
        seen = set()
        cursor = current
        expected_digest = current.get("record_sha256")
        expected_sequence = current.get("sequence")
        a._require(type(expected_sequence) is int and expected_sequence >= 0, "invalid activation sequence")
        while True:
            a._require(isinstance(cursor, dict) and bool(cursor), "activation history node is missing or empty")
            self._activation_shape(cursor)
            digest = cursor.get("record_sha256")
            a._require(a._is_sha256(digest) and digest == expected_digest and digest == a.record_sha256(cursor) and digest not in seen, "activation hash/name/cycle mismatch")
            seen.add(digest)
            a._require(cursor.get("record_type") == "alignment_current" and cursor.get("schema_version") == 1 and
                       cursor.get("sequence") == expected_sequence and a._is_sha256(cursor.get("release_sha256")), "invalid activation history")
            stored = a.load_json(self.store._path("activations", digest))
            a._require(stored == cursor, "current does not match immutable activation history")
            a._require(self.store._path("activations", digest).read_bytes() == r.json_bytes(cursor), "immutable activation bytes changed")
            previous = cursor.get("previous_pointer_sha256")
            if expected_sequence == 0:
                a._require(previous is None and cursor.get("action") == "register" and
                           registration.get("initial_pointer_sha256") == digest and
                           registration.get("baseline_release_sha256") == cursor["release_sha256"], "registration lineage mismatch")
                break
            a._require(cursor.get("action") in {"activate", "rollback"} and a._is_sha256(previous), "activation lacks predecessor")
            cursor = a.load_json(self.store._path("activations", previous))
            expected_digest = previous
            expected_sequence -= 1
        return current

    def current(self, context):
        pointer = self.pointer()
        if pointer is None:
            return None
        cache = {}
        checked_reviews = set()

        def load(digest):
            if digest not in cache:
                view = self.store.load(context, digest, baseline_root=self.root)
                while view:
                    cache[view.digest] = view
                    view = view.parent
            return cache[digest]

        selected = load(pointer["release_sha256"])
        cursor = pointer
        while cursor:
            view = load(cursor["release_sha256"])
            predecessor = cursor["previous_pointer_sha256"]
            previous = a.load_json(self.store._path("activations", predecessor)) if predecessor else None
            if cursor["action"] == "register":
                a._require(view.parent is None and previous is None, "registration must select original baseline history")
            elif cursor["action"] == "activate":
                a._require(previous is not None and view.parent is not None and
                           view.parent.digest == previous["release_sha256"], "activation selected a release with the wrong actual parent")
                if view.digest not in checked_reviews:
                    validate_review_gate(context, view)
                    checked_reviews.add(view.digest)
            else:
                prior = load(previous["release_sha256"]).parent if previous else None
                while prior and prior.digest != view.digest:
                    prior = prior.parent
                a._require(prior is not None, "recorded rollback is not to an actual ancestor")
            cursor = previous
        return selected

    def _inventory(self, context, candidate=None, *, initial=False):
        from literary.tools.alignment_holds import HoldStore
        authority = r.authority_files(context, self.root)
        values = {"authority": r.inventory_fingerprint(authority),
                  "authority_member_presence": {name: (self.root / name).exists() for name in sorted(authority)},
                  "source": _source_inventory(context), "holds": HoldStore(self.root).fingerprint(context),
                  "current": None if initial else self.pointer(),
                  "registration": r.sha(self.registration_path.read_bytes()) if self.registration_path.exists() and not initial else None}
        if candidate:
            selected = {}
            view = candidate
            while view:
                # Re-read the actual submitted objects rather than hashing only
                # references from a possibly stale in-memory candidate.
                selected[view.digest] = self.store.get_manifest(view.digest)
                for digest in set(view.manifest["files"].values()):
                    self.store.get_object(digest)
                view = view.parent
            values["submitted_candidate_closure"] = selected
        return r.sha(r.json_bytes(values))

    def register(self, context, *, reason: str):
        """Select only the independently validated existing baseline, not a new chain."""
        a._require(isinstance(reason, str) and bool(reason.strip()), "registration reason required")
        with writer_lock(self.root):
            a._require(not (self.root / ".publish.lock").exists(), "legacy publication still owns its lock")
            a._require(self.pointer() is None, "release selection already registered")
            context = _fresh(context)
            before = self._inventory(context)
            view = r.baseline(context, self.root)
            self.store.save(view)
            a._require(self._inventory(_fresh(context)) == before, "authority changed during registration")
            pointer = a.with_record_hash({"schema_version": 1, "record_type": "alignment_current", "sequence": 0,
                        "action": "register", "release_sha256": view.digest, "previous_pointer_sha256": None,
                        "reason": reason, "authority_fingerprint": before})
            self.store._put(self.store._path("activations", pointer["record_sha256"]), r.json_bytes(pointer))
            registration = a.with_record_hash({"schema_version": 1, "record_type": "alignment_registration",
                            "baseline_release_sha256": view.digest, "initial_pointer_sha256": pointer["record_sha256"]})
            self.store._put(self.registration_path, r.json_bytes(registration))
            atomic_write(self.current_path, r.json_bytes(pointer))
            return pointer

    def prepare(self, context, digest, *, action="activate"):
        """Read-only frozen comparison ticket. Any authority change makes it stale."""
        a._require(action in {"activate", "rollback"}, "unknown publication action")
        context = _fresh(context)
        pointer = self.pointer()
        a._require(pointer is not None, "register validated existing history first")
        current = self.current(context)
        candidate = self.store.load(context, digest, baseline_root=self.root)
        if action == "activate":
            a._require(candidate.parent is not None and candidate.parent.digest == current.digest, "stale candidate parent")
            affected = validate_review_gate(context, candidate)
            from literary.tools.alignment_holds import HoldStore
            holds = HoldStore(self.root).active(context, candidate)
            touched = {chapter for scope in affected["batches"].values() for chapter in scope["reading_chapter_ids"]}
            a._require(not any(touched & set(hold["chapter_ids"]) for hold in holds.values()), "affected release has unresolved review holds")
        else:
            ancestor = current.parent
            while ancestor and ancestor.digest != digest:
                ancestor = ancestor.parent
            a._require(ancestor is not None, "rollback target must be an actual ancestor; no arbitrary branch switching")
        return {"schema_version": 1, "action": action, "candidate_release_sha256": digest,
                "expected_parent_release_sha256": current.digest,
                "expected_pointer_sha256": pointer["record_sha256"],
                "authority_fingerprint": self._inventory(context, candidate)}

    def activate(self, context, ticket, *, reason: str):
        a._require(isinstance(reason, str) and bool(reason.strip()), "activation/rollback requires an explicit reason")
        with writer_lock(self.root):
            a._require(not (self.root / ".publish.lock").exists(), "legacy publication still owns its lock")
            fresh = self.prepare(context, ticket["candidate_release_sha256"], action=ticket.get("action"))
            a._require(fresh == ticket, "authority inventory or current changed after candidate validation")
            previous = self.pointer()
            pointer = a.with_record_hash({"schema_version": 1, "record_type": "alignment_current",
                        "sequence": previous["sequence"] + 1, "action": ticket["action"],
                        "release_sha256": ticket["candidate_release_sha256"],
                        "previous_pointer_sha256": previous["record_sha256"], "reason": reason,
                        "authority_fingerprint": ticket["authority_fingerprint"]})
            self.store._put(self.store._path("activations", pointer["record_sha256"]), r.json_bytes(pointer))
            # A concurrent draft is not authority; a selected object, source,
            # inventory member, checkpoint or hold change is always a conflict.
            candidate = self.store.load(context, ticket["candidate_release_sha256"], baseline_root=self.root)
            a._require(self._inventory(_fresh(context), candidate) == ticket["authority_fingerprint"], "authority changed before atomic activation")
            atomic_write(self.current_path, r.json_bytes(pointer))
            return pointer

    def recover_registration(self, context):
        """Only an interrupted FIRST pointer can be reconstructed automatically."""
        with writer_lock(self.root):
            a._require(not self.current_path.exists(), "not an interrupted first registration")
            events = list((self.store.root / "activations").iterdir())
            a._require(len(events) == 1 and a._is_sha256(events[0].name), "ambiguous missing current; later activation history exists")
            digest = events[0].name
            pointer = a.load_json(events[0])
            self._activation_shape(pointer)
            a._require(events[0].read_bytes() == r.json_bytes(pointer), "immutable initial activation bytes changed")
            registration = self._registration() if self.registration_path.exists() else a.with_record_hash({
                "schema_version": 1, "record_type": "alignment_registration", "baseline_release_sha256": pointer["release_sha256"],
                "initial_pointer_sha256": digest})
            a._require(registration["initial_pointer_sha256"] == digest and pointer["record_sha256"] == digest,
                       "initial activation filename/registration hash mismatch")
            a._require(pointer.get("record_sha256") == a.record_sha256(pointer) and pointer.get("sequence") == 0 and
                       pointer.get("previous_pointer_sha256") is None and
                       pointer.get("action") == "register" and pointer.get("release_sha256") == registration.get("baseline_release_sha256"), "invalid initial activation")
            fresh = _fresh(context)
            frozen = self.store.load(fresh, pointer["release_sha256"], baseline_root=self.root)
            live = r.baseline(fresh, self.root)
            a._require(frozen.digest == live.digest, "initial authority changed; cannot resume registration")
            from literary.tools.alignment_holds import HoldStore
            HoldStore(self.root).events(fresh)
            a._require(self._inventory(fresh, initial=True) == pointer.get("authority_fingerprint"),
                       "registration recovery source/authority inventory changed")
            self.store._put(self.registration_path, r.json_bytes(registration))
            atomic_write(self.current_path, r.json_bytes(pointer))
            return pointer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("current", "check", "register", "activate", "rollback", "recover-registration"))
    parser.add_argument("--root", type=Path, default=a.ROOT)
    parser.add_argument("--release")
    parser.add_argument("--reason")
    parser.add_argument("--expected-parent")
    parser.add_argument("--expected-inventory")
    parser.add_argument("--action", choices=("activate", "rollback"), default="activate", help="Action to validate with check")
    args = parser.parse_args()
    try:
        context = a.load_context(args.root)
        publisher = Publisher(context.alignment_root)
        if args.command == "current":
            result = publisher.pointer()
        elif args.command == "register":
            result = publisher.register(context, reason=args.reason)
        elif args.command == "recover-registration":
            result = publisher.recover_registration(context)
        else:
            action = args.action if args.command == "check" else ("rollback" if args.command == "rollback" else "activate")
            result = publisher.prepare(context, args.release, action=action)
            if args.command != "check":
                a._require(args.expected_parent == result["expected_parent_release_sha256"] and
                           args.expected_inventory == result["authority_fingerprint"], "pass the exact parent and inventory returned by check")
                result = publisher.activate(context, result, reason=args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (a.AlignmentError, OSError, ValueError, KeyError) as exc:
        print("FAIL: " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
