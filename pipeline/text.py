"""Caption sanitising shared by every platform.

Prompts ask the models not to emit hashtags or links, but a prompt is a
request, not a guarantee -- across ~36 posts a day something eventually slips
through. So the rule is enforced here, at post time, for every destination.

Links matter for more than tidiness: X bills $0.015 per post but $0.20 if it
contains a link, so one stray URL costs 13x.
"""

import re

URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)

# A hash followed by a LETTER. Deliberately not `#\w+`, which would also eat
# "#1" and mangle ordinary copy like "ranked #1 by revenue".
#
# `&` is allowed inside a tag because "#M&A" is common in finance copy, and
# without it the tag splits into "#M" + "&A" -- which silently breaks the
# trailing-run match below and lets a whole block of tags through.
TAG = r"(?<!\w)#[A-Za-z][\w&]*"
HASHTAG_RE = re.compile(TAG)

# A trailing run of tags -- the usual "...text. #VC #AI" shape. The tail
# allows punctuation and emoji so a stray character after the last tag
# doesn't stop the whole run from matching.
TRAILING_TAGS_RE = re.compile(rf"(?:\s*{TAG})+[\s\W]*$")


def sanitize(text: str, limit: int | None = None) -> str:
    """Strip links and hashtags, tidy whitespace, optionally truncate.

    Hashtags are handled in two passes, because the two positions mean
    different things. A trailing run is pure tagging and is deleted whole.
    An inline one is usually a word doing real grammatical work -- "ahead of
    #Intel and AMD" -- so only the '#' is dropped and the word survives;
    deleting it would leave "ahead of and AMD".
    """
    text = URL_RE.sub("", text)
    text = TRAILING_TAGS_RE.sub("", text)
    text = HASHTAG_RE.sub(lambda m: m.group(0)[1:], text)

    # Removing trailing tags leaves dangling separators and double spaces.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip().rstrip("-–—,;:· ").strip()

    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return text
