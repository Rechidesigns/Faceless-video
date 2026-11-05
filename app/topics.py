"""Per-channel topic queue: take the next topic, refilling with AI-brainstormed ideas when empty."""
from . import llm, store


def refill(cid: str, n: int = 10) -> list[str]:
    ch = store.get_channel(cid)
    if not ch:
        raise RuntimeError("Channel not found")
    if not ch.get("niche"):
        raise RuntimeError(f"Channel '{ch['name']}' has no niche set")
    new = llm.generate_topics(ch["niche"], ch.get("notes", ""), ch.get("language", "English"),
                              avoid=ch.get("used", []) + ch.get("queue", []), n=n)
    store.update_channel(cid, lambda c: c["queue"].extend(new))
    return new


def take_next(cid: str) -> str:
    ch = store.get_channel(cid)
    if ch and not ch.get("queue"):
        refill(cid)
    picked: list[str] = []

    def pop(c):
        if c["queue"]:
            t = c["queue"].pop(0)
            picked.append(t)
            c["used"] = (c.get("used", []) + [t])[-300:]

    store.update_channel(cid, pop)
    if not picked:
        raise RuntimeError("No topic available for this channel")
    return picked[0]
