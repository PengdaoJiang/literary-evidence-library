"""Read or append an explicitly authored hold event. Never creates judgments."""
import argparse
import json
from pathlib import Path

from literary.tools import alignment as a, alignment_release as r
from literary.tools.alignment_holds import HoldStore
from literary.tools.alignment_activate import Publisher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "append", "recover"))
    parser.add_argument("--root", type=Path, default=a.ROOT)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    try:
        context = a.load_context(args.root)
        store = HoldStore(context.alignment_root)
        if args.command == "list":
            events = store.events(context)
            current = Publisher(context.alignment_root).current(context)
            result = {"event_count": len(events), "active": store.active(context, current), "fingerprint": store.fingerprint(context)}
        elif args.command == "recover":
            result = {"recovered_event_count": len(store.recover(context)), "judgments_created": False}
        else:
            a._require(args.input is not None, "append requires a genuinely authored --input event")
            event = a.load_json(args.input)
            view = r.Store(context.alignment_root).load(context, event["release_sha256"]) if event.get("event_type") == "resolve" else None
            result = store.append(context, event, view)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (a.AlignmentError, OSError, ValueError, KeyError) as exc:
        print("FAIL: " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
