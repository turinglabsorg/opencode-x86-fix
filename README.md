# opencode-x86-fix

Run [opencode](https://opencode.ai) on an Intel Mac without AVX2 — Ivy Bridge
and older: Mac Pro 2013, 2012 iMacs and MacBook Pros, older Xeons.

```
$ opencode
zsh: illegal hardware instruction  opencode     # exit code 132
```

Related upstream reports:
[#29039](https://github.com/anomalyco/opencode/issues/29039),
[#24876](https://github.com/anomalyco/opencode/issues/24876),
[#8345](https://github.com/anomalyco/opencode/issues/8345),
[#45869](https://github.com/anomalyco/opencode/issues/45869).

## Root cause

opencode's installer does the right thing: it checks `hw.optional.avx2_0` and
downloads `opencode-darwin-x64-baseline.zip` when the CPU has no AVX2. The
problem is that this asset is not actually a baseline build.

Measured on release v1.18.27 (Xeon E5-1620 v2, Ivy Bridge, no AVX2, macOS 12.7.6):

| what | sha256 (first 32) | runs without AVX2 |
|---|---|---|
| `opencode-darwin-x64.zip` → binary | `27ae61d47f7c3a136eb2fc4e308206fc` | no — SIGILL |
| `opencode-darwin-x64-baseline.zip` → binary | `27ae61d47f7c3a136eb2fc4e308206fc` | no — SIGILL |

The two assets are different zip files containing a **byte-identical binary**.
That binary embeds `Bun v1.3.14 (0d9b296a)`, and the reason the baseline build
is not distinct goes one level further up — Bun 1.3.14 itself had no working
macOS-x64 baseline runtime:

| Bun compile runtime | sha256 (first 32) | runs without AVX2 |
|---|---|---|
| `@oven/bun-darwin-x64@1.3.14` | `ea2f223e94bb2f4bf3050895113c3cf3` | no — SIGILL |
| `@oven/bun-darwin-x64-baseline@1.3.14` | `ea2f223e94bb2f4bf3050895113c3cf3` | no — SIGILL |
| `@oven/bun-darwin-x64@1.4.0` | `ca8a18d0116d7b6b19f53bb0d8c48e48` | **yes** |
| `@oven/bun-darwin-x64-baseline@1.4.0` | `ca8a18d0116d7b6b19f53bb0d8c48e48` | **yes** |
| `@oven/bun-darwin-x64@1.4.1` | `a96f31f7f3cb2dd8…` | **yes** |

So on macOS x64 Bun ships a single binary and the `-baseline` name is an alias
for it. In 1.3.14 that binary required AVX2; since 1.4.0 it does not. opencode
pins `"packageManager": "bun@1.3.14"`, so `bun build --compile
--target=bun-darwin-x64-baseline` could only embed the AVX2-requiring runtime,
and both release assets came out the same.

**Linux is unaffected**: Bun 1.3.14's `@oven/bun-linux-x64-baseline`
(`a8f9ebd1770ddc8e…`) *is* a distinct build from `@oven/bun-linux-x64`
(`9fd36f87e4b90b07…`), so `opencode-linux-x64-baseline.tar.gz` is a genuine
baseline build. Install opencode normally there.

**The upstream fix is a one-line version bump** — see
[the PR](https://github.com/anomalyco/opencode/pulls) bumping the pinned Bun to
1.4.1. This repo is the workaround until a release ships with it.

## The workaround

opencode's binary is a Bun *standalone executable*: the Bun runtime with the
app's JS bundle embedded. So extract the bundle and run it on a Bun that works
on this CPU:

```bash
git clone https://github.com/turinglabsorg/opencode-x86-fix
cd opencode-x86-fix
./install.sh
opencode --version    # 1.18.27
```

Re-run `./install.sh` to update (it exits early if you are current). The
launcher goes to `~/.opencode/bin/opencode` when the official installer's
directory exists — it comes first on `PATH`, so the launcher has to live there
or the crashing binary keeps winning; the original is kept as
`opencode.avx2-broken`. Otherwise it goes to `/usr/local/bin/opencode`.

| Path | Purpose |
|---|---|
| `~/.opencode-x86/app/` | extracted JS bundle (entry `src/index.js`) |
| `~/.opencode-x86/runtime/bun` | Bun 1.4.1 runtime |
| `~/.opencode-x86/app.old/` | previous version, for rollback |

Overrides: `OPENCODE_FIX_HOME`, `OPENCODE_FIX_BIN`, `OPENCODE_VERSION`,
`BUN_VERSION`.

Tested: opencode 1.18.27 on a Mac Pro 2013 (Xeon E5-1620 v2, no AVX2), macOS
12.7.6, Bun 1.4.0 and 1.4.1. The first run is slow (~4 min: cold transpile of
1318 modules with no bytecode cache); subsequent runs start in about 2s.

## Two things the extraction has to fix

**Virtual paths.** The bundle refers to its own files through Bun's virtual
filesystem prefix (`/$bunfs/root/`), so `--rewrite-prefix` points those at the
real install directory.

**Native-asset imports.** Bun's bundler emits native assets as
`import("<chunk>.js", { with: { type: "file" } })`, where the chunk is a shim
whose default export is the asset's path. Inside a compiled executable the
standalone graph resolves that attribute to the asset; running the same chunks
on a plain Bun, `type: "file"` instead returns the path of the *chunk*, so the
consumer `dlopen()`s a `.js` file:

```
Failed to initialize OpenTUI render library: … tried:
'…/app/chunk-zttpctyt.js' (not a mach-o file)
```

The extractor drops the attribute on `.js` specifiers only, so the chunk
evaluates as a module and its default export gives the real `.dylib` path.
Without this the TUI cannot start (`--version` still works, which makes it easy
to think the install is fine).

## Gotcha: a stale Homebrew install shadows the launcher

`~/.opencode/bin` is added to `PATH` by the official installer's shell snippet,
which only runs for interactive shells. If you also have an old
`brew install opencode`, then in scripts, `ssh` one-liners and other
non-interactive shells `/usr/local/bin/opencode` wins and you get the
AVX2-requiring binary — an exit-132 SIGILL that looks like this fix failing.
Check with `command -v opencode` in the same context that failed, and remove
the Homebrew copy (`brew uninstall opencode`) if you have one.

## How the extraction works

[`bunfs_extract.py`](bunfs_extract.py) (stdlib-only Python) parses Bun's
StandaloneModuleGraph out of the executable and applies both fixes above. It is
the same
tool as in [claude-cli-x86-fix](https://github.com/turinglabsorg/claude-cli-x86-fix),
where the binary-format details are documented; it works on any
`bun build --compile` output:

```bash
python3 bunfs_extract.py ./some-bun-app --list
```

## Legal note

This repo contains **no opencode code** — only a format parser and an install
script. The release archive is downloaded from GitHub at install time, on your
machine, and the extraction happens locally so that software you are licensed
to use runs on your own hardware. opencode is MIT licensed; even so,
do not republish the extracted bundle — install from the official source.

## License

MIT (the tooling in this repo).
