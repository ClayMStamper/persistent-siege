"""Read and write Dungeon Siege 1 tank files (.dsres / .dsmap).

Layout (all little-endian), as used by the retail tanks:

  RawHeader (0x310 bytes) + optional description
    'DSig' 'Tank', header version, dir-set offset, file-set offset,
    index size, data offset, product/minimum version, priority, flags,
    creator id, GUID, index/data CRC32, UTC build time, copyright,
    build, title and author text (UTF-16, fixed width).
  Data area   at data offset: file payloads, raw or chunk-compressed.
  Index       at dir-set offset: DirSet then FileSet. Every offset inside
              the index is relative to the dir-set offset.

Compression formats: 0 raw, 1 zlib, 2 LZO1X. Compressed files carry a
chunk table; chunks whose compressed size equals their uncompressed size
are stored raw.
"""

import argparse
import fnmatch
import os
import struct
import sys
import time
import uuid
import zlib

HEADER_SIZE = 0x310
CHUNK_SIZE = 0x4000
CHUNK_EXTRA = 16
DATA_OFFSET = 0x324
FORMAT_RAW, FORMAT_ZLIB, FORMAT_LZO = 0, 1, 2

# Tank priorities used by the engine when the same path is in several tanks.
PRIORITY_FACTORY = 0x0000
PRIORITY_LANGUAGE = 0x1000
PRIORITY_EXPANSION = 0x2000
PRIORITY_PATCH = 0x3000
PRIORITY_USER = 0x4000


def _nstring(buf, pos):
    """Read a length-prefixed ASCII string padded to 4-byte alignment."""
    (n,) = struct.unpack_from("<H", buf, pos)
    s = buf[pos + 2 : pos + 2 + n].decode("latin-1")
    total = 2 + n + 1  # trailing NUL counts toward the 4-byte alignment
    total += (4 - total % 4) % 4
    return s, pos + total


def _pack_nstring(s):
    raw = s.encode("latin-1")
    out = struct.pack("<H", len(raw)) + raw + b"\0"
    out +=b"\0" * ((4 - len(out) % 4) % 4)
    return out


def _wstr(buf, pos, nchars):
    raw = buf[pos : pos + nchars * 2].decode("utf-16-le")
    return raw.split("\0", 1)[0]


def _pack_wstr(s, nchars):
    raw = s[: nchars - 1].encode("utf-16-le")
    return raw + b"\0" * (nchars * 2 - len(raw))


def lzo1x_decompress(src, out_len):
    """Pure-Python LZO1X decompressor (safe variant)."""
    dst = bytearray()
    ip = 0
    n = len(src)

    def take_len(base, mask_bits):
        nonlocal ip
        length = 0
        while src[ip] == 0:
            length += 255
            ip += 1
        length += base + src[ip]
        ip += 1
        return length

    state = 0
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        dst += src[ip : ip + t]
        ip += t
        state = 4 if t >= 4 else t
    while ip < n:
        t = src[ip]
        ip += 1
        if t >= 64:
            # M2: 3..8 bytes, distance up to 2 KB
            length = (t >> 5) - 1 + 2
            dist = ((t >> 2) & 7) + (src[ip] << 3) + 1
            ip += 1
            nlit = t & 3
        elif t >= 32:
            length = t & 31
            if length == 0:
                length = take_len(31, 0)
            length += 2
            dist = (src[ip] >> 2) + (src[ip + 1] << 6) + 1
            nlit = src[ip] & 3
            ip += 2
        elif t >= 16:
            length = t & 7
            if length == 0:
                length = take_len(7, 0)
            length += 2
            dist = ((t & 8) << 11) + (src[ip] >> 2) + (src[ip + 1] << 6)
            nlit = src[ip] & 3
            ip += 2
            if dist == 0:
                break  # end of stream
            dist += 0x4000
        else:
            if state == 0:
                # literal run
                length = t
                if length == 0:
                    length = take_len(15, 0)
                length += 3
                dst += src[ip : ip + length]
                ip += length
                state = 4
                continue
            elif state < 4:
                # M1 short match after a short literal run
                length = 2
                dist = (t >> 2) + (src[ip] << 2) + 1
            else:
                # M1 after a long literal run
                length = 3
                dist = (t >> 2) + (src[ip] << 2) + 2049
            nlit = t & 3
            ip += 1
        start = len(dst) - dist
        if start < 0:
            raise ValueError("LZO: bad back-reference")
        for i in range(length):
            dst.append(dst[start + i])
        dst += src[ip : ip + nlit]
        ip += nlit
        state = nlit
    if len(dst) != out_len:
        raise ValueError("LZO: expected %d bytes, got %d" % (out_len, len(dst)))
    return bytes(dst)


class TankFile:
    __slots__ = ("path", "size", "offset", "crc", "filetime", "format", "flags", "chunks")


class TankReader:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.buf = f.read()
        b = self.buf
        if b[0:8] != b"DSigTank":
            raise ValueError("%s is not a Dungeon Siege tank" % path)
        (
            self.header_version,
            self.dirset_offset,
            self.fileset_offset,
            self.index_size,
            self.data_offset,
        ) = struct.unpack_from("<5I", b, 8)
        self.product_version = b[0x1C:0x28]
        self.minimum_version = b[0x28:0x34]
        self.priority, self.flags = struct.unpack_from("<2I", b, 0x34)
        self.creator = b[0x3C:0x40]
        self.guid = b[0x40:0x50]
        self.title = _wstr(b, 0x1F8, 100)
        self.author = _wstr(b, 0x2C0, 40)
        self._dirs = {}
        self.files = {}
        self._read_index()

    def _dir_path(self, off):
        parts = []
        while off and off in self._dirs:
            parent, name = self._dirs[off]
            if name:
                parts.append(name)
            if parent == off:
                break
            off = parent
        return "/".join(reversed(parts))

    def _read_index(self):
        b, base = self.buf, self.dirset_offset
        (ndirs,) = struct.unpack_from("<I", b, base)
        for i in range(ndirs):
            (doff,) = struct.unpack_from("<I", b, base + 4 + 4 * i)
            p = base + doff
            parent, _count = struct.unpack_from("<2I", b, p)
            name, _ = _nstring(b, p + 16)
            self._dirs[doff] = (parent, name)
        fbase = self.fileset_offset
        (nfiles,) = struct.unpack_from("<I", b, fbase)
        for i in range(nfiles):
            (foff,) = struct.unpack_from("<I", b, fbase + 4 + 4 * i)
            p = fbase + foff
            tf = TankFile()
            parent, tf.size, tf.offset, tf.crc = struct.unpack_from("<4I", b, p)
            tf.filetime = b[p + 16 : p + 24]
            tf.format, tf.flags = struct.unpack_from("<2H", b, p + 24)
            name, p = _nstring(b, p + 28)
            tf.chunks = []
            if tf.format != FORMAT_RAW:
                _csize, _chunk_size = struct.unpack_from("<2I", b, p)
                p += 8
                nchunks = (tf.size + _chunk_size - 1) // _chunk_size if _chunk_size else 1
                for _ in range(max(nchunks, 1)):
                    tf.chunks.append(struct.unpack_from("<4I", b, p))
                    p += 16
            d = self._dir_path(parent)
            tf.path = (d + "/" + name) if d else name
            self.files[tf.path.lower()] = tf

    def read(self, path):
        tf = self.files[path.lower()]
        start = self.data_offset + tf.offset
        if tf.format == FORMAT_RAW:
            return self.buf[start : start + tf.size]
        out = bytearray()
        for usize, csize, extra, coff in tf.chunks:
            # A compressed chunk inflates to (usize - extra) bytes; the last
            # `extra` bytes of the chunk are stored raw after the stream.
            raw = self.buf[start + coff : start + coff + csize + extra]
            if usize == csize:
                out += raw[:usize]
            elif tf.format == FORMAT_ZLIB:
                out += zlib.decompress(raw[:csize]) + raw[csize:csize + extra]
            elif tf.format == FORMAT_LZO:
                out += lzo1x_decompress(raw[:csize], usize - extra) + raw[csize:csize + extra]
            else:
                raise ValueError("unknown tank format %d" % tf.format)
        return bytes(out)

    def list(self, pattern="*"):
        pat = pattern.lower()
        return sorted(tf.path for k, tf in self.files.items() if fnmatch.fnmatch(k, pat))


def _compress_file(payload):
    """Chunk-compress a file exactly the way the retail tanks are built.

    Each CHUNK_SIZE piece is an independent zlib stream. Every chunk except
    the last holds its final CHUNK_EXTRA bytes uncompressed after the stream
    (the engine depends on this: a missing tail on a middle chunk fails with
    Z_DATA_ERROR). The last chunk has no extra bytes and may be stored raw.
    Returns (blob, chunks) or None when the file should be stored raw.
    """
    chunks, blob = [], bytearray()
    starts = list(range(0, len(payload), CHUNK_SIZE))
    for n, pos in enumerate(starts):
        piece = payload[pos:pos + CHUNK_SIZE]
        last = n == len(starts) - 1
        extra = 0 if last else CHUNK_EXTRA
        comp = zlib.compress(piece[:len(piece) - extra], 9)
        if len(comp) + extra >= len(piece):
            if not last:
                return None
            comp, extra = piece, 0  # raw final chunk, as retail does
        chunks.append((len(piece), len(comp), extra, len(blob)))
        blob += comp
        if extra:
            blob += piece[len(piece) - extra:]
    if len(blob) >= len(payload):
        return None
    return bytes(blob), chunks


def _filetime_now():
    # FILETIME: 100 ns intervals since 1601-01-01
    return struct.pack("<Q", int((time.time() + 11644473600) * 10_000_000))


def write_tank(out_path, files, title, author, description="", priority=PRIORITY_USER,
               product_version=None, minimum_version=None, compress=True):
    """Write a tank. `files` maps tank paths ('world/maps/...') to bytes."""
    # Build directory tree.
    tree = {"": {"dirs": {}, "files": []}}
    for path in sorted(files, key=str.lower):
        parts = path.replace("\\", "/").strip("/").split("/")
        node = tree[""]
        for d in parts[:-1]:
            node = node["dirs"].setdefault(d.lower(), {"name": d, "dirs": {}, "files": []})
        node["files"].append((parts[-1], path))

    ft = _filetime_now()

    # Data area. The header's data CRC is the CRC-32 of every file's
    # uncompressed contents, concatenated in data order.
    data = bytearray()
    file_meta = {}
    data_crc = 0
    for path in sorted(files, key=str.lower):
        payload = files[path]
        while len(data) % 4:
            data += b"\0"
        off = len(data)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        data_crc = zlib.crc32(payload, data_crc)
        if compress and len(payload) > 64:
            packed = _compress_file(payload)
            if packed is not None:
                blob, chunks = packed
                data += blob
                file_meta[path] = (off, crc, FORMAT_ZLIB, chunks, len(blob))
                continue
        data += payload
        file_meta[path] = (off, crc, FORMAT_RAW, [], len(payload))

    # Dir set: offsets relative to the dir-set start. Lay out dirs first so
    # every dir's offset is known, then patch child lists.
    dir_nodes = []

    def walk(node, parent_idx):
        idx = len(dir_nodes)
        dir_nodes.append([node, parent_idx, []])
        # children are listed dirs and files interleaved, sorted by name,
        # as in the retail tanks
        kids = [(d["name"].lower(), "dir", d) for d in node["dirs"].values()]
        kids += [(name.lower(), "file", path) for name, path in node["files"]]
        for _, kind, ref in sorted(kids, key=lambda k: k[0]):
            if kind == "dir":
                dir_nodes[idx][2].append(("dir", walk(ref, idx)))
            else:
                dir_nodes[idx][2].append(("file", ref))
        return idx

    walk(tree[""], 0)

    def dir_entry_size(n):
        name = n[0].get("name", "")
        return 16 + len(_pack_nstring(name)) + 4 * len(n[2])

    dir_offsets = []
    pos = 4 + 4 * len(dir_nodes)
    for n in dir_nodes:
        dir_offsets.append(pos)
        pos += dir_entry_size(n)
    dirset_size = pos

    file_paths = sorted(files, key=str.lower)
    file_parent = {}
    for i, n in enumerate(dir_nodes):
        for kind, ref in n[2]:
            if kind == "file":
                file_parent[ref] = i

    def file_entry(path, parent_rel):
        off, crc, fmt, chunks, csize = file_meta[path]
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        e = struct.pack("<4I", parent_rel, len(files[path]), off, crc) + ft
        e += struct.pack("<2H", fmt, 0) + _pack_nstring(name)
        if fmt != FORMAT_RAW:
            e += struct.pack("<2I", csize, CHUNK_SIZE)
            for c in chunks:
                e += struct.pack("<4I", *c)
        return e

    # File set offsets are relative to the dir set too, so we need the file
    # set's position. Entries reference their parent dir by dir-set offset.
    fileset_rel = dirset_size
    fentries = [file_entry(p, dir_offsets[file_parent[p]]) for p in file_paths]
    file_offsets = []
    pos = 4 + 4 * len(fentries)
    for e in fentries:
        file_offsets.append(pos)
        pos += len(e)
    file_abs = {p: fileset_rel + o for p, o in zip(file_paths, file_offsets)}

    dirset = bytearray(struct.pack("<I", len(dir_nodes)))
    for o in dir_offsets:
        dirset += struct.pack("<I", o)
    for i, n in enumerate(dir_nodes):
        parent = dir_offsets[n[1]] if i else 0
        dirset += struct.pack("<2I", parent, len(n[2])) + ft + _pack_nstring(n[0].get("name", ""))
        for kind, ref in n[2]:
            dirset += struct.pack("<I", dir_offsets[ref] if kind == "dir" else file_abs[ref])

    fileset = bytearray(struct.pack("<I", len(fentries)))
    for o in file_offsets:
        fileset += struct.pack("<I", o)
    for e in fentries:
        fileset += e

    index = bytes(dirset + fileset)
    # Retail tanks reserve 0x14 bytes after the fixed header (description
    # string plus padding) and start data at 0x324.
    desc = _pack_nstring(description)
    data_offset = max(HEADER_SIZE + len(desc), DATA_OFFSET)
    data_offset += (4 - data_offset % 4) % 4
    dirset_offset = data_offset + len(data)

    hdr = bytearray(b"DSigTank")
    hdr += struct.pack("<5I", 0x00010002, dirset_offset, dirset_offset + len(dirset),
                       len(index), data_offset)
    hdr += product_version or b"\0" * 12
    hdr += minimum_version or b"\0" * 12
    hdr += struct.pack("<2I", priority, 0)
    hdr += b"PSS!"
    hdr += uuid.uuid4().bytes_le
    hdr += struct.pack("<2I", zlib.crc32(index) & 0xFFFFFFFF, data_crc & 0xFFFFFFFF)
    t = time.gmtime()
    hdr += struct.pack("<8H", t.tm_year, t.tm_mon, (t.tm_wday + 1) % 7, t.tm_mday,
                       t.tm_hour, t.tm_min, t.tm_sec, 0)
    hdr += _pack_wstr("Persistent Siege Server content tank", 100)
    hdr += _pack_wstr(time.strftime("%Y-%m-%d %H:%M UTC", t), 100)
    hdr += _pack_wstr(title, 100)
    hdr += _pack_wstr(author, 40)
    assert len(hdr) == HEADER_SIZE, hex(len(hdr))
    out = bytes(hdr) + desc
    out += b"\0" * (data_offset - len(out))
    out += bytes(data) + index
    with open(out_path, "wb") as f:
        f.write(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("tank")
    p = sub.add_parser("ls"); p.add_argument("tank"); p.add_argument("pattern", nargs="?", default="*")
    p = sub.add_parser("cat"); p.add_argument("tank"); p.add_argument("path")
    p = sub.add_parser("extract"); p.add_argument("tank"); p.add_argument("out")
    p.add_argument("pattern", nargs="?", default="*")
    a = ap.parse_args(argv)

    t = TankReader(a.tank)
    if a.cmd == "info":
        print("title:    %s\nauthor:   %s\npriority: 0x%x\nfiles:    %d"
              % (t.title, t.author, t.priority, len(t.files)))
    elif a.cmd == "ls":
        for path in t.list(a.pattern):
            print(path)
    elif a.cmd == "cat":
        sys.stdout.buffer.write(t.read(a.path))
    elif a.cmd == "extract":
        for path in t.list(a.pattern):
            dest = os.path.join(a.out, *path.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(t.read(path))
        print("extracted %d files" % len(t.list(a.pattern)))


if __name__ == "__main__":
    main()
