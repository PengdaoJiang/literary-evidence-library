"""Resolve exact quotes locally so extraction workers need not write offset code.

This only compiles a payload. It does not submit, infer facts, or choose among
ambiguous quote occurrences. Semantic qualifiers remain mandatory.
"""
from copy import deepcopy
from contract import VERSION, validate


def prepare_context_support(registry, source, allowed_paragraphs):
    """Compile separately supplied support; never grant it body coverage."""
    allowed = {p["paragraph_id"]: p["text"] for p in allowed_paragraphs}
    output = deepcopy(source)
    for item in output:
        for index, value in enumerate(item["evidence"]):
            text = allowed.get(value["paragraph_id"])
            if text is None:
                raise ValueError("Context support was not present in the supplied reading material")
            if set(value) == {"paragraph_id"}:
                item["evidence"][index] = dict(paragraph_id=value["paragraph_id"], start=0, end=len(text), quote=text)
                continue
            quote = value["quote"]
            positions, start = [], text.find(quote)
            if not quote.strip():
                raise ValueError("Empty support quote")
            while start >= 0:
                positions.append(start)
                start = text.find(quote, start+1)
            occurrence = value.get("occurrence")
            if occurrence is None:
                if len(positions) != 1:
                    raise ValueError("Ambiguous support quote; specify occurrence")
                occurrence = 0
            if type(occurrence) is not int or not 0 <= occurrence < len(positions):
                raise ValueError("Support quote not found")
            start = positions[occurrence]
            item["evidence"][index] = dict(paragraph_id=value["paragraph_id"], start=start, end=start+len(quote), quote=quote)
    return output


def prepare(registry, task_id, token, source):
    payload = deepcopy(source)
    payload.setdefault("schema_version", VERSION)
    paragraphs = {p["paragraph_id"]: p["text"] for p in registry.task(task_id, token)["body"]}

    def anchor(value):
        if set(value) == {"paragraph_id"}:
            text = paragraphs.get(value["paragraph_id"])
            if text is None:
                raise ValueError("Paragraph anchor must belong to task body")
            return dict(paragraph_id=value["paragraph_id"], start=0, end=len(text), quote=text)
        if set(value) == {"paragraph_id", "start", "end", "quote"}:
            text = paragraphs.get(value["paragraph_id"])
            if text is None or type(value["start"]) is not int or type(value["end"]) is not int or not 0 <= value["start"] < value["end"] <= len(text) or text[value["start"]:value["end"]] != value["quote"]:
                raise ValueError("Invalid exact span in task body")
            return value
        if set(value) not in ({"paragraph_id", "quote"}, {"paragraph_id", "quote", "occurrence"}):
            raise ValueError("Anchor needs paragraph_id, quote, and optional zero-based occurrence")
        text = paragraphs.get(value["paragraph_id"])
        quote = value["quote"]
        if text is None or not isinstance(quote, str) or not quote.strip():
            raise ValueError("Anchor must quote nonempty task body text")
        matches, start = [], text.find(quote)
        while start >= 0:
            matches.append(start)
            start = text.find(quote, start + 1)
        occurrence = value.get("occurrence")
        if occurrence is None:
            if len(matches) != 1:
                raise ValueError(f"Quote has {len(matches)} matches; specify occurrence or a more precise quote")
            occurrence = 0
        if type(occurrence) is not int or not 0 <= occurrence < len(matches):
            raise ValueError("Quote occurrence is outside the matching occurrences")
        start = matches[occurrence]
        return dict(paragraph_id=value["paragraph_id"], start=start, end=start+len(quote), quote=quote)

    for entry in payload.get("entries", []):
        entry.setdefault("existing_id", None)
        entry["evidence"] = [anchor(a) for a in entry["evidence"]]
    for mention in payload.get("mentions", []):
        mention["span"] = anchor(mention["span"])
    for fact in payload.get("facts", []):
        for field in ("object", "value", "speaker", "story_time"):
            fact.setdefault(field, None)
        fact["evidence"] = [anchor(a) for a in fact["evidence"]]
    validate(payload)
    return payload
