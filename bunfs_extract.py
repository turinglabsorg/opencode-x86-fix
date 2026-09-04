#!/usr/bin/env python3
"""Extract the embedded module graph from a Bun standalone executable.

Bun's `bun build --compile` appends a StandaloneModuleGraph to the runtime
executable. On macOS it lives in the Mach-O `__BUN,__bun` section; on Linux
and Windows it is appended to the file. In all cases the region ends with:

    [graph payload = byte_count bytes][Offsets 32B][TRAILER 16B][total u64 8B]

where TRAILER is the magic bytes "\\n---- Bun! ----\\n". The Offsets struct
(repr(C), from Bun's src/standalone_graph/StandaloneModuleGraph.rs):

    byte_count: u64                 -- length of the graph payload
    modules_ptr: {offset u32, length u32}
    entry_point_id: u32
    compile_exec_argv_ptr: {offset u32, length u32}
    flags: u32

All StringPointer offsets are relative to the start of the payload, i.e.
base = trailer_pos - 32 - byte_count.

Each module table record (CompiledModuleGraphFile, repr(C), 52 bytes):

    name, contents, sourcemap, bytecode,
    module_info, bytecode_origin_path : StringPointer x6 (48 bytes)
    encoding, loader, module_format, side : u8 x4

Verified against Bun 1.4.x binaries. Stdlib only, no dependencies.
"""

import argparse
import os
import re
import struct
import sys

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32
RECORD_SIZE = 52
BUNFS_PREFIXES = ("/$bunfs/root/", "B:\\~BUN\\root\\", "/$bunfs/")
ENC_UTF16 = 2

# `import("<chunk>.js", { with: { type: "file" } })` — Bun's bundler emits this
# for a native asset imported as a file: the chunk is a shim whose default
# export is the asset's path. Inside a compiled executable the standalone graph
# resolves the attribute to the asset; run the same chunks on a plain Bun and
# `type: "file"` instead yields the path of the *chunk*, so the consumer
# dlopen()s a .js file and fails ("not a mach-o file"). Dropping the attribute
# makes the chunk evaluate as a module, whose default export is the real path.
# Only .js specifiers are touched — on a genuine asset the attribute is correct.
ASSET_IMPORT_RE = re.compile(
    r"""import\(\s*(["'])([^"']+\.js)\1\s*,\s*\{\s*with\s*:\s*"""
    r"""\{\s*type\s*:\s*["']file["']\s*\}\s*,?\s*\}\s*\)"""
)


def fix_asset_imports(text):
    return ASSET_IMPORT_RE.subn(lambda m: f"import({m.group(1)}{m.group(2)}{m.group(1)})", text)


def parse_graph(data):
    """Return (base, records, entry_point_id).

    Each record is a dict with name, contents, encoding, loader.
    """
    pos = data.rfind(TRAILER)
    if pos < 0:
        sys.exit("error: trailer not found — not a Bun standalone executable?")
    off_pos = pos - OFFSETS_SIZE
    byte_count, mod_off, mod_len, entry_id = struct.unpack(
        "<QIII", data[off_pos:off_pos + 20]
    )
    base = off_pos - byte_count
    if base < 0 or base + mod_off + mod_len > len(data):
        sys.exit("error: inconsistent offsets — unknown or newer graph format")
    if mod_len % RECORD_SIZE != 0:
        sys.exit(
            f"error: module table length {mod_len} is not a multiple of "
            f"{RECORD_SIZE} — unknown or newer graph format"
        )

    tbl = base + mod_off
    records = []
    for i in range(mod_len // RECORD_SIZE):
        rec = data[tbl + i * RECORD_SIZE: tbl + (i + 1) * RECORD_SIZE]
        ptrs = struct.unpack("<12I", rec[:48])
        enc, loader, _mfmt, _side = struct.unpack("<4B", rec[48:52])
        name = data[base + ptrs[0]: base + ptrs[0] + ptrs[1]].decode(
            "utf-8", "replace"
        )
        if i == 0 and not (name.startswith("/") or name.startswith("B:")):
            sys.exit(
                "error: first module name does not look like a path "
                f"({name[:40]!r}) — unknown or newer graph format"
            )
        records.append({
            "name": name,
            "contents": (base + ptrs[2], ptrs[3]),
            "encoding": enc,
            "loader": loader,
        })
    return data, records, entry_id


TS_SPEC_RE = re.compile(r"""(["'])((?:\.{0,2}/)[A-Za-z0-9_./-]+?)\.ts\1""")


def fix_ts_specifiers(outdir, base):
    """Point build-time-injected .ts source paths at the emitted .js files.

    Bun's `define` bakes source specifiers into the bundle — opencode injects
    OPENCODE_WORKER_PATH as "./src/cli/tui/worker.ts". Inside a compiled
    executable the standalone graph resolves that name to the emitted module;
    on a real filesystem only the transpiled worker.js exists, so the
    `new Worker(...)` never starts. The TUI then waits for a worker that will
    never come up and exits on its startup timeout — with no error printed.

    Rewrite such specifiers to the absolute path of the emitted .js file,
    resolving them both against the referencing file and against the output
    root (the compiled binary resolved them against the bunfs root, which is
    not where the referencing chunk necessarily sits).
    """
    existing = set()
    for root, _, names in os.walk(outdir):
        for n in names:
            if n.endswith(".js"):
                rel = os.path.relpath(os.path.join(root, n), outdir)
                existing.add(rel.replace(os.sep, "/"))

    base = base.rstrip("/")
    total = 0
    for root, _, names in os.walk(outdir):
        for n in names:
            if not n.endswith(".js"):
                continue
            path = os.path.join(root, n)
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            here = os.path.dirname(os.path.relpath(path, outdir)).replace(os.sep, "/")
            hits = []

            def repl(m):
                quote, spec = m.group(1), m.group(2)
                if spec.endswith(".d"):  # .d.ts type declarations
                    return m.group(0)
                cands = []
                if here:
                    cands.append(os.path.normpath(f"{here}/{spec}"))
                cands.append(os.path.normpath(spec))
                for cand in cands:
                    rel = cand.replace(os.sep, "/").lstrip("./")
                    if f"{rel}.js" in existing:
                        hits.append(rel)
                        return f"{quote}{base}/{rel}.js{quote}"
                return m.group(0)

            new_text = TS_SPEC_RE.sub(repl, text)
            if hits:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_text)
                total += len(hits)
    return total


def strip_prefix(name):
    for prefix in BUNFS_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("\\", "/").lstrip("/")


def main():
    ap = argparse.ArgumentParser(
        description="Extract embedded files from a Bun standalone executable."
    )
    ap.add_argument("binary", help="path to the bun-compiled executable")
    ap.add_argument("outdir", nargs="?", help="output directory (omit with --list)")
    ap.add_argument("-l", "--list", action="store_true",
                    help="list embedded files without extracting")
    ap.add_argument("--rewrite-prefix", metavar="DIR",
                    help="rewrite embedded /$bunfs/root/ path references in "
                         "extracted text files to DIR (absolute path), so "
                         "import statements resolve on the real filesystem")
    args = ap.parse_args()

    if not args.list and not args.outdir:
        ap.error("outdir is required unless --list is given")

    with open(args.binary, "rb") as f:
        data = f.read()
    data, records, entry_id = parse_graph(data)

    if args.list:
        for i, r in enumerate(records):
            marker = "  <- entry point" if i == entry_id else ""
            print(f"{r['contents'][1]:>12}  {strip_prefix(r['name'])}{marker}")
        print(f"\n{len(records)} embedded files")
        return

    rewrite_old = rewrite_new = None
    if args.rewrite_prefix:
        target = os.path.abspath(args.rewrite_prefix)
        rewrite_old = b"/$bunfs/root/"
        rewrite_new = target.encode() + b"/"

    entry_rel = None
    asset_imports = 0
    for i, r in enumerate(records):
        rel = strip_prefix(r["name"]) or f"unnamed_{i}"
        off, length = r["contents"]
        contents = data[off: off + length]
        if r["encoding"] == ENC_UTF16:
            contents = contents.decode("utf-16-le", "replace").encode("utf-8")
        if rewrite_old and not rel.endswith(".node") and rewrite_old in contents:
            contents = contents.replace(rewrite_old, rewrite_new)
        if rel.endswith(".js"):
            try:
                fixed, n = fix_asset_imports(contents.decode())
            except UnicodeDecodeError:
                n = 0
            if n:
                contents = fixed.encode()
                asset_imports += n
        dest = os.path.join(args.outdir, rel)
        os.makedirs(os.path.dirname(dest) or args.outdir, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(contents)
        if i == entry_id:
            entry_rel = rel

    if args.rewrite_prefix:
        n_ts = fix_ts_specifiers(args.outdir, os.path.abspath(args.rewrite_prefix))
        if n_ts:
            print(f"fixed {n_ts} build-time .ts source path(s)")

    print(f"extracted {len(records)} files to {args.outdir}")
    if asset_imports:
        print(f"fixed {asset_imports} native-asset import(s)")
    print(f"entry point: {entry_rel}")


if __name__ == "__main__":
    main()
