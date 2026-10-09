"""Minimal parser for Dungeon Siege .gas files.

Keeps the source text of every block so callers can copy blocks through
unchanged and only rewrite what they need.

    [t:type,n:name]        block header (type and name optional: [name])
    {
      x key = value;       optional one-letter type prefix
      key = [[ ... ]];     raw skrit, may contain braces and semicolons
      [child] { ... }
    }
"""

import re

_HEADER = re.compile(r"\[\s*(?:t:([^,\]]+),\s*n:([^\]]+)|([^\]]+))\]")
_TYPED_KEY = re.compile(r"^(?:([a-z])\s+)?([^\s=]+)$")


class Block:
    __slots__ = ("type", "name", "items", "start", "end", "src")

    def __init__(self, type_, name, start, src):
        self.type, self.name, self.items = type_, name, []
        self.start, self.end, self.src = start, None, src

    @property
    def raw(self):
        return self.src[self.start:self.end]

    @property
    def body(self):
        """Text between the braces."""
        raw = self.raw
        return raw[raw.index("{") + 1: raw.rindex("}")]

    def blocks(self, name=None):
        for it in self.items:
            if isinstance(it, Block) and (name is None or (it.name or "").lower() == name.lower()):
                yield it

    def block(self, name):
        return next(self.blocks(name), None)

    def entries(self):
        for it in self.items:
            if isinstance(it, Entry):
                yield it

    def get(self, key, default=None):
        key = key.lower()
        for e in self.entries():
            if e.key.lower() == key:
                return e.value
        return default

    def walk(self):
        yield self
        for b in self.blocks():
            yield from b.walk()

    def __repr__(self):
        return "Block(%s:%s)" % (self.type, self.name)


class Entry:
    __slots__ = ("type", "key", "value", "start", "end")

    def __init__(self, type_, key, value, start, end):
        self.type, self.key, self.value, self.start, self.end = type_, key, value, start, end

    def __repr__(self):
        return "Entry(%s=%r)" % (self.key, self.value)


class ParseError(ValueError):
    pass


def _skip_ws(s, i):
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n\x1a﻿":
            i += 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _read_value(s, i):
    """Read up to the terminating ';' (respecting quotes and [[ ]]). Returns (value, index after ';')."""
    n = len(s)
    out = []
    while i < n:
        if s.startswith("[[", i):
            j = s.find("]]", i + 2)
            if j < 0:
                raise ParseError("unterminated [[ at %d" % i)
            out.append(s[i:j + 2])
            i = j + 2
        elif s[i] == '"':
            # strings never span lines; an unpaired quote ends at the newline
            j = s.find('"', i + 1)
            nl = s.find("\n", i + 1)
            if j < 0 or (0 <= nl < j):
                j = (nl if nl >= 0 else n) - 1
            out.append(s[i:j + 1])
            i = j + 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif s[i] == ";":
            return "".join(out).strip(), i + 1
        elif s[i] == "}":
            # tolerate a missing ';' before a closing brace
            return "".join(out).strip(), i
        else:
            out.append(s[i])
            i += 1
    return "".join(out).strip(), i


def _parse_items(s, i, parent, depth=0):
    """Parse items until the closing '}' of `parent`.

    Returns the index of that '}', or of a typed top-level header found in
    column 0 while nested. Some retail files are truncated mid-block; the
    header resync recovers the following templates the same way the engine
    keeps loading past the damage.
    """
    n = len(s)
    while True:
        i = _skip_ws(s, i)
        if i >= n:
            return i
        c = s[i]
        if c == "}":
            return i
        if c == "[":
            m = _HEADER.match(s, i)
            if not m:
                raise ParseError("bad header at %d: %r" % (i, s[i:i + 40]))
            if m.group(1) is not None:
                t, name = m.group(1).strip(), m.group(2).strip()
                if depth > 0 and (i == 0 or s[i - 1] == "\n") and t.lower() == "template":
                    return i
            else:
                t, name = None, m.group(3).strip()
            b = Block(t, name, i, s)
            j = _skip_ws(s, m.end())
            if j >= n or s[j] != "{":
                # damaged header with no body (present in some retail files): skip it
                i = m.end()
                continue
            j = _parse_items(s, j + 1, b, depth + 1)
            parent.items.append(b)
            if j < n and s[j] == "}":
                b.end = j + 1
                i = j + 1
            else:
                b.end = j  # truncated block; resume at the resync point
                if depth > 0:
                    return j
                i = j
            continue
        if c == ";":
            i += 1
            continue
        # key = value;
        start = i
        eq = i
        while eq < n and s[eq] not in "=;{}\n":
            eq += 1
        if eq < n and s[eq] == "=":
            keytxt = s[i:eq].strip()
            value, i = _read_value(s, eq + 1)
        else:
            keytxt = s[i:eq].strip()
            value, i = "", (eq + 1 if eq < n and s[eq] == ";" else eq)
            if eq < n and s[eq] in "{}":
                i = eq if s[eq] == "}" else eq + 1
        m = _TYPED_KEY.match(keytxt)
        t, key = (m.group(1), m.group(2)) if m else (None, keytxt)
        parent.items.append(Entry(t, key, value, start, i))


def parse(text):
    root = Block(None, None, 0, text)
    i = _parse_items(text, 0, root)
    while i < len(text) and text[i] == "}":
        # stray closing brace at top level: skip it like the engine does
        i = _parse_items(text, i + 1, root)
    root.end = len(text)
    return root


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    return v


def decode(data):
    return data.decode("latin-1")


def encode(text):
    return text.encode("latin-1")
