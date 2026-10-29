"""Stable, URL-safe identifiers shared by the web API and the Subsonic layer.

The Subsonic protocol requires that the ids returned by listing endpoints
(``getArtists`` / ``getIndexes`` / ``getAlbumList2`` ...) can be fed back into
the detail endpoints (``getArtist`` / ``getMusicDirectory`` / ``getAlbum`` ...).
All of those endpoints therefore have to derive ids with *exactly* the same
function.  Historically the database layer used a naive ``lower().replace(' ')``
while the Subsonic layer used a stricter slug, which made any artist or album
containing ``/``, ``、``, ``（`` or an apostrophe unresolvable and broke client
library syncs (e.g. Symfonium).

Keep this module dependency-free so both layers can import it without cycles.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# Everything that is not a latin letter/digit, a CJK ideograph or one of the
# explicitly allowed punctuation characters becomes a separator.
_DISALLOWED = re.compile(r'[^0-9a-z\u4e00-\u9fff._()\-]+')
_REPEATED_DASH = re.compile(r'-{2,}')


def slugify(value: str, fallback_prefix: str = 'item') -> str:
    """Return a deterministic, URL-safe slug for ``value``.

    The transformation is intentionally identical to the historical
    ``_slugify`` helper in ``app.api.subsonic`` so already published ids keep
    working for names that did not contain special characters.
    """
    text = unicodedata.normalize('NFC', str(value or '')).strip().lower()
    text = re.sub(r'\s+', '-', text)
    text = _DISALLOWED.sub('-', text)
    text = _REPEATED_DASH.sub('-', text).strip('-')
    if text:
        return text
    digest = hashlib.sha1(unicodedata.normalize('NFC', str(value or '')).encode('utf-8')).hexdigest()[:8]
    return f'{fallback_prefix}-{digest}'


def artist_id(name: str) -> str:
    return slugify(name, 'artist')


def album_id(name: str) -> str:
    return slugify(name, 'album')
