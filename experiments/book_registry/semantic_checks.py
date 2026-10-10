"""Representation checks, not a semantic truth or recall evaluator.

Only unambiguous contract violations are errors. Warnings need source review;
they never rewrite a model's assertion. Free predicates remain extensible.
"""
import re

VERSION = "literary-representation-v3"
SOURCE_ID = re.compile(r"SU-\d{6}(?:-P\d{4})?")
STRUCTURAL_LINKS = frozenset(('speaker','addressee','content','utterance','in_scene',
    'location','located_at','agent','patient','theme','participant','recipient','item',
    'before','after','simultaneous','overlaps','occurs_at'))


def granularity_candidates(payload):
    """Triage only: a role/content shell can still carry consequential speech."""
    candidates=[]
    incident={}
    for f in payload['facts']:
        for ref in {f['subject'],f['object'],f['speaker']} - {None}:
            incident.setdefault(ref,[]).append(f)
    for entry in payload['entries']:
        if entry['kind'] not in ('utterance','proposition','event'):
            continue
        ref=entry['ref']
        facts=incident.get(ref,[])
        # A bare participant of another event is not a fully formed micro-event.
        if not any(f['subject']==ref for f in facts):
            continue
        if facts and all(f['type']!='identity' and f['predicate'] in STRUCTURAL_LINKS for f in facts):
            candidates.append({'ref':ref,'kind':entry['kind'],'name':entry['name'],
                'fact_refs':[f['ref'] for f in facts],
                'reason':'Only role/content/placement/order links are encoded; review the source for independent information gain before promoting this node.'})
    return candidates


def inspect(payload):
    entries = {e["ref"]: e for e in payload["entries"]}
    issues = []
    linked = set()

    def issue(level, code, ref, message):
        issues.append(dict(level=level, code=code, ref=ref, message=message))

    for f in payload["facts"]:
        subject = entries.get(f["subject"], {})
        target = entries.get(f["object"], {})
        sk, tk, predicate = subject.get("kind"), target.get("kind"), f["predicate"]
        linked.update(x for x in (f["subject"], f["object"], f["speaker"]) if x)
        if f["story_time"] and SOURCE_ID.search(f["story_time"]):
            issue("error", "narrative_position_is_not_story_time", f["ref"],
                  "story_time contains a source position ID; use null unless story time is known. Evidence already stores position.")
        if f["type"] == "relation":
            if predicate in ("location", "located_at") and tk == "scene":
                issue("error", "scene_is_not_place", f["ref"], "A scene uses in_scene; location/located_at requires a place, not a scene.")
            if predicate == "in_scene" and tk != "scene":
                issue("error", "in_scene_target", f["ref"], "in_scene must target a scene.")
            if predicate == "item" and tk in ("utterance", "proposition", "text_feature", "scene", "event"):
                issue("error", "abstract_content_is_not_item", f["ref"], "Use utterance/content/participant or an exact extended role; item is a material object.")
            if predicate == "patient" and tk in ("scene", "event", "proposition", "utterance"):
                issue("warning", "check_patient_scope", f["ref"], "Check the actually affected entity; perception/counting targets are not automatically patients.")
            if predicate == "agent" and tk in ("manifestation", "phenomenon", "place", "location"):
                issue("warning", "check_volitional_actor", f["ref"], "Nonvolitional motion/change normally uses theme; do not assign agency from grammatical subject alone.")
        if f["attribution"] == "character" and f["speaker"] is None:
            issue("warning", "character_without_speaker", f["ref"], "Name the source character or explicitly preserve the unknown attribution.")
        if f["type"] == "temporal" and f["predicate"] in ("before", "after") and f["frame"] == "unknown":
            issue("warning", "uncertain_temporal_frame", f["ref"], "Check story order independently of textual order and retain uncertain frames.")
    for ref, entry in entries.items():
        if entry["kind"] in ("event", "scene", "proposition", "utterance", "text_feature", "state") and ref not in linked:
            issue("warning", "unconnected_reified_node", ref, "A reified node has no facts; connect its supported role/content or keep only the mention.")
    for candidate in granularity_candidates(payload):
        issue('warning','independent_information_gain_unchecked',candidate['ref'],candidate['reason'])
    return {"version": VERSION, "issues": issues,
            "errors": sum(i["level"] == "error" for i in issues),
            "warnings": sum(i["level"] == "warning" for i in issues),
            "limitation": "Passing checks does not establish truth, identity, role correctness or source coverage."}


def require_valid(payload):
    report = inspect(payload)
    errors = [i for i in report["issues"] if i["level"] == "error"]
    if errors:
        raise ValueError("Representation contract: " + "; ".join(f"{i['ref']}:{i['code']}" for i in errors))
    return report
