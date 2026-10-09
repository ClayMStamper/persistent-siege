"""Build the Persistent Siege content tank (pss_content_v<N>.dsres).

Reads the retail tanks of a licensed install and writes one tank that
overrides, for both the Kingdom of Ehb and the Utraean Peninsula:

  * region actor files: each eligible static monster becomes a
    pss_monster_generator with the same scid and placement (bosses get a
    long timer and only respawn with no player nearby);
  * region special/index files: a pss_checkpoint at every start group;
  * the playable hero templates: + [pss_player] (respawn, host parking);
  * formulas.gas: multiplayer scaling shifted so the parked host avatar
    does not count as a player;
plus the mod's own Skrit components and templates from content/.

Monsters stay static (one-shot until the next scheduled restart) when they
are referenced by scid anywhere in the map (triggers, commands, quests,
conversations), carry instance triggers, or are excluded in the config.
Instance overrides such as quest items in [inventory] are baked into a
derived template so the respawned monster carries them again.

    python build_content.py --game "C:/.../Dungeon Siege 1" --out ../Resources
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gas  # noqa: E402
import tank  # noqa: E402

PKG = os.path.dirname(HERE)
CONTENT = os.path.join(PKG, "content")

WORLDS = [
    # (key, map tank, map dir name, template prefix)
    ("ehb", "Maps/World.dsmap", "map_world", "pss_ehb"),
    ("utrae", "Maps/MpWorld.dsmap", "multiplayer_world", "pss_utr"),
]

HEX = re.compile(r"0x[0-9a-fA-F]{8}\b")
SCID_HEADER = re.compile(r"^\s*\[t:[^,\]]+,n:(0x[0-9a-fA-F]+)\]", re.M)

# Instance sub-blocks that can be baked into a derived template.
BAKEABLE = {"inventory", "aspect", "mind", "actor", "body", "attack", "defend", "physics", "common"}
# [common] keys that are editor-only or harmless to drop.
COMMON_DROP = {"dev_instance_text", "is_single_player"}


def log(*a):
    print(*a, flush=True)


class Templates:
    def __init__(self):
        self.by_name = {}   # lower name -> (Block, tank path)

    def add_file(self, path, text):
        try:
            root = gas.parse(text)
        except gas.ParseError as e:
            log("  warn: skipping %s: %s" % (path, e))
            return
        for b in root.blocks():
            if (b.type or "").lower() == "template" and b.name:
                self.by_name.setdefault(b.name.lower(), (b, path))

    def has(self, name):
        return name.lower() in self.by_name

    def chain(self, name):
        out, seen = [], set()
        name = name.lower()
        while name and name in self.by_name and name not in seen:
            seen.add(name)
            b, _ = self.by_name[name]
            out.append(b)
            name = gas.unquote(b.get("specializes", "")).lower()
        return out

    def lookup(self, name, block, key):
        for b in self.chain(name):
            sub = b.block(block)
            if sub is not None:
                v = sub.get(key)
                if v is not None:
                    return gas.unquote(v)
        return None

    def is_monster(self, name):
        align = (self.lookup(name, "actor", "alignment") or "").lower()
        if align:
            return align == "aa_evil"
        member = (self.lookup(name, "common", "membership") or "").lower()
        return "monster" in [m.strip() for m in member.split(",")]

    def max_life(self, name):
        v = self.lookup(name, "aspect", "max_life")
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0


def strip_tier(name):
    return re.sub(r"^[23]w_", "", name, flags=re.I)


class World:
    def __init__(self, key, reader, mapdir, prefix, extra_ref_text):
        self.key, self.t, self.prefix = key, reader, prefix
        self.base = "world/maps/%s" % mapdir
        self.out = {}            # tank path -> bytes
        self.derived = []        # derived template text
        self.stats = Counter()
        self.kept = Counter()    # reason -> count
        self.bosses = Counter()
        self.refs = self._collect_refs(extra_ref_text)

    def text(self, path):
        return gas.decode(self.t.read(path))

    def _collect_refs(self, extra):
        """Every scid-looking value referenced by map content (not headers or positions)."""
        refs = set()
        for path in self.t.list(self.base + "/*"):
            # index files list every object by scid; they are not references
            if not path.lower().endswith((".gas", ".skrit")) or "/index/" in path.lower():
                continue
            for line in self.text(path).splitlines():
                if "position" in line or SCID_HEADER.match(line) or "guid" in line:
                    continue
                refs.update(int(h, 16) for h in HEX.findall(line))
        for txt in extra:
            refs.update(int(h, 16) for h in HEX.findall(txt))
        return refs

    def regions(self):
        regs = set()
        for p in self.t.list(self.base + "/regions/*/main.gas"):
            regs.add(p.split("/")[-2])
        return sorted(regs)


def instance_override_blocks(inst):
    """Return (bakeable blocks, initial_command, reason-to-keep or None)."""
    blocks, initial_command = [], None
    for b in inst.blocks():
        name = (b.name or "").lower()
        if name == "placement":
            continue
        if name not in BAKEABLE:
            return None, None, "instance block [%s]" % name
        if name == "common":
            if b.block("instance_triggers") is not None:
                return None, None, "instance triggers"
            if (b.get("is_multi_player") or "").strip().lower() == "false":
                return None, None, "single-player only"
            keep = [e for e in b.entries() if e.key.lower() not in COMMON_DROP]
            if not keep and not list(b.blocks()):
                continue
        if name == "aspect":
            ls = (b.get("life_state") or "").lower()
            if ls and ls != "ls_alive_conscious":
                return None, None, "placed with life_state %s" % ls
        if name == "mind":
            ic = b.get("initial_command")
            if ic:
                initial_command = ic.strip()
            body = [e for e in b.entries() if e.key.lower() != "initial_command"]
            if not body and not list(b.blocks()):
                continue
            blocks.append(_rebuild_without(b, {"initial_command"}))
            continue
        if name == "common":
            blocks.append(_rebuild_without(b, COMMON_DROP))
            continue
        blocks.append(b.raw)
    return blocks, initial_command, None


def _rebuild_without(block, drop_keys):
    s = block.src
    parts = []
    for it in block.items:
        if isinstance(it, gas.Entry):
            if it.key.lower() in drop_keys:
                continue
            parts.append("\t\t" + s[it.start:it.end].strip())
        else:
            parts.append("\t\t" + it.raw.strip())
    return "[%s]\n\t{\n%s\n\t}" % (block.name, "\n".join(parts))


def convert_actor_file(world, path, templates, cfg, region):
    text = world.text(path)
    try:
        root = gas.parse(text)
    except gas.ParseError as e:
        world.kept["unparseable file"] += 1
        log("  warn: %s left unchanged (%s)" % (path, e))
        return
    boss_names = {n.lower() for n in cfg["boss_templates"]}
    never = {n.lower() for n in cfg["never_respawn_templates"]}
    pats = [p.lower() for p in cfg["boss_name_patterns"]]

    out, changed = [], 0
    for inst in root.blocks():
        tmpl, scid_s = inst.type, inst.name
        keep_reason = None
        try:
            scid = int(scid_s, 16)
        except (TypeError, ValueError):
            scid, keep_reason = None, "no scid"
        if keep_reason is None and not (tmpl and templates.has(tmpl)):
            keep_reason = "unknown template"
        if keep_reason is None and not templates.is_monster(tmpl):
            keep_reason = "not a monster"
        if keep_reason is None and strip_tier(tmpl).lower() in never:
            keep_reason = "excluded in config"
        if keep_reason is None and scid in world.refs:
            keep_reason = "referenced by scid (scripted)"
        placement = inst.block("placement")
        if keep_reason is None and placement is None:
            keep_reason = "no placement"
        blocks = initial_command = None
        if keep_reason is None:
            blocks, initial_command, keep_reason = instance_override_blocks(inst)

        if keep_reason is not None:
            if keep_reason != "not a monster":
                world.kept[keep_reason] += 1
            out.append(inst.raw)
            continue

        base = strip_tier(tmpl).lower()
        boss = (base in boss_names or any(p in base for p in pats)
                # judge strength on the regular-tier template: veteran/elite
                # variants of ordinary monsters have boss-sized life pools
                or templates.max_life(base if templates.has(base) else tmpl) >= cfg["boss_min_life"])
        child = tmpl
        if blocks:
            child = "%s_%08x" % (world.prefix, scid)
            world.derived.append(
                "[t:template,n:%s]\n{\n\tspecializes = %s;\n\tdoc = \"%s at %s (scid 0x%08X)\";\n\t%s\n}\n"
                % (child, tmpl, tmpl, region, scid, "\n\t".join(blocks)))
            world.stats["carriers (derived template)"] += 1

        props = ['\t\tchild_template_name = "%s";' % child]
        if initial_command:
            props.append("\t  x initial_command = %s;" % initial_command)
        if boss:
            props.append("\t  b boss = true;")
            props.append("\t  f respawn_delay = %g;" % cfg["boss_respawn_delay"])
            props.append("\t  f encounter_range = %g;" % cfg["boss_encounter_range"])
            world.bosses[strip_tier(tmpl)] += 1
        else:
            props.append("\t  f respawn_delay = %g;" % cfg["monster_respawn_delay"])
        props.append("\t  f leash_range = %g;" % cfg["leash_range"])
        out.append("[t:pss_monster_generator,n:%s]\n{\n\t[pss_monster_gen]\n\t{\n%s\n\t}\n\t%s\n}"
                   % (scid_s, "\n".join(props), placement.raw.strip()))
        changed += 1
        world.stats["bosses" if boss else "monsters"] += 1

    if changed:
        world.out[path] = gas.encode("\n".join(out) + "\n")


def object_dirs(world, region):
    """Object directories of a region: flat, or one per world tier."""
    base = "%s/regions/%s/objects" % (world.base, region)
    tiers = sorted({p.split("/")[-2] for p in world.t.list(base + "/*/*.gas")})
    if tiers:
        return [(base + "/" + tr, "%s/regions/%s/index/%s" % (world.base, region, tr)) for tr in tiers]
    return [(base, "%s/regions/%s/index" % (world.base, region))]


def place_checkpoints(world, cfg):
    sp_path = world.base + "/info/start_positions.gas"
    root = gas.parse(world.text(sp_path))
    node_region = {}
    for p in world.t.list(world.base + "/regions/*/terrain_nodes/nodes.gas"):
        region = p.split("/")[-3]
        for m in re.finditer(r"\[t:snode,n:(0x[0-9a-fA-F]+)\]", world.text(p)):
            node_region[int(m.group(1), 16)] = region

    per_region = defaultdict(list)
    for grp in root.walk():
        if (grp.type or "").lower() != "start_group":
            continue
        if (grp.get("dev_only") or "").strip().lower() == "true":
            continue
        positions = list(grp.blocks("start_position"))
        if not positions:
            continue
        positions.sort(key=lambda b: int(b.get("id", "0") or 0))
        # retail data has typos like "x,y,z.0xNODE"; rebuild a clean SiegePos
        # from the first readable position of the group
        for sp in positions:
            raw = sp.get("position") or ""
            m = re.match(r"\s*(-?[\d.e+-]+)\s*,\s*(-?[\d.e+-]+)\s*,\s*(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\D*?(0x[0-9a-fA-F]+)", raw)
            if m:
                break
            log("  warn: start group %s: unreadable position %r" % (grp.name, raw))
        else:
            continue
        node = int(m.group(4), 16)
        pos = "%s,%s,%s,0x%08x" % (m.group(1), m.group(2), m.group(3), node)
        region = node_region.get(node)
        if region is None:
            log("  warn: start group %s: node 0x%08x not in any region" % (grp.name, node))
            continue
        per_region[region].append((grp.name, pos, node))

    for region, groups in sorted(per_region.items()):
        main = gas.parse(world.text("%s/regions/%s/main.gas" % (world.base, region)))
        rblock = next(main.blocks())
        scid_range = int(rblock.get("scid_range"), 0)
        used = set()
        for p in world.t.list("%s/regions/%s/objects/*" % (world.base, region)):
            if p.endswith(".gas"):
                used.update(int(h, 16) for h in SCID_HEADER.findall(world.text(p)))
        nxt = (scid_range << 20) | 0xF0000
        alloc = []
        for g in groups:
            while nxt in used:
                nxt += 1
            alloc.append((g, nxt))
            used.add(nxt)
            nxt += 1

        for objdir, indexdir in object_dirs(world, region):
            special = objdir + "/special.gas"
            txt = world.out.get(special)
            txt = gas.decode(txt) if txt else (world.text(special) if special.lower() in world.t.files else "")
            add = []
            for (gname, pos, node), scid in alloc:
                add.append("[t:pss_checkpoint,n:0x%08X]\n{\n\t[pss_checkpoint]\n\t{\n\t\tstart_group = \"%s\";\n"
                           "\t  f reach_range = %g;\n\t}\n\t[placement]\n\t{\n\t  p position = %s;\n\t}\n}\n"
                           % (scid, gname, cfg["checkpoint_reach_range"], pos))
            world.out[special] = gas.encode(txt.rstrip() + "\n" + "".join(add))

            index = indexdir + "/streamer_node_content_index.gas"
            itxt = world.text(index)
            cut = itxt.rstrip().rfind("}")
            lines = "".join("  x 0x%08x = 0x%08X;\n" % (node, scid) for (_, _, node), scid in alloc)
            world.out[index] = gas.encode(itxt[:cut] + lines + itxt[cut:])
        world.stats["checkpoints"] += len(alloc)


def patch_player_templates(logic, templates, cfg, out):
    """Add [pss_player] to the playable hero templates (in their own files)."""
    edits = defaultdict(list)
    for name in cfg["player_templates"]:
        if not templates.has(name):
            log("  warn: player template %s not found" % name)
            continue
        b, path = templates.by_name[name.lower()]
        edits[path].append(b)
    props = ("\n\t[pss_player]\n\t{\n\t  f respawn_delay = %g;\n\t  f respawn_life_ratio = %g;\n\t}\n"
             % (cfg["player_respawn_delay"], cfg["player_respawn_life_ratio"]))
    for path, blocks in edits.items():
        text = gas.decode(logic.read(path))
        for b in sorted(blocks, key=lambda b: b.end, reverse=True):
            close = b.end - 1
            assert text[close] == "}", (path, b.name)
            text = text[:close] + props + text[close:]
        out[path] = gas.encode(text)
        log("  patched %s: %s" % (path, ", ".join(b.name for b in blocks)))


def patch_formulas(logic, cfg, out):
    path = "world/global/formula/formulas.gas"
    text = gas.decode(logic.read(path))
    if not cfg["shift_mp_scaling_for_host_avatar"]:
        return

    def shift(m):
        vals = [v.strip() for v in m.group(2).split(",")]
        new = [vals[0]] + vals[:-1]  # n humans incl. host scale like n-1 players
        return "%s[[ %s ]]" % (m.group(1), ", ".join(new))

    block = re.search(r"\[mp_scaling\]\s*\{.*?\}", text, re.S)
    patched = re.sub(r"(\b(?:life|xp)\s*=\s*)\[\[([^\]]*)\]\]", shift, block.group(0))
    text = text[:block.start()] + patched + text[block.end():]
    out[path] = gas.encode(text)
    log("  mp_scaling: %s" % " ".join(patched.split()))


def main():
    ap = argparse.ArgumentParser(description="Build the Persistent Siege content tank.")
    ap.add_argument("--game", required=True, help="Dungeon Siege install directory")
    ap.add_argument("--out", default=os.path.join(PKG, "Resources"), help="output directory")
    ap.add_argument("--config", default=os.path.join(CONTENT, "pss_content.json"))
    ap.add_argument("--report", help="write a JSON build report here")
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    logic = tank.TankReader(os.path.join(a.game, "Resources", "Logic.dsres"))
    log("Logic.dsres: %d files" % len(logic.files))

    templates = Templates()
    for p in logic.list("world/contentdb/templates/*"):
        if p.lower().endswith(".gas"):
            templates.add_file(p, gas.decode(logic.read(p)))
    for root, _, files in os.walk(os.path.join(CONTENT, "world", "contentdb", "templates")):
        for f in files:
            if f.endswith(".gas"):
                templates.add_file(f, open(os.path.join(root, f), encoding="latin-1").read())
    log("templates: %d" % len(templates.by_name))

    out = {}
    report = {"worlds": {}}
    extra_refs = [gas.decode(logic.read(p)) for p in logic.list("world/maps/*")
                  if p.lower().endswith((".gas", ".skrit"))]

    for key, tank_rel, mapdir, prefix in WORLDS:
        reader = tank.TankReader(os.path.join(a.game, *tank_rel.split("/")))
        w = World(key, reader, mapdir, prefix, extra_refs)
        log("%s (%s): %d regions, %d referenced scids" % (key, mapdir, len(w.regions()), len(w.refs)))
        for region in w.regions():
            for objdir, _ in object_dirs(w, region):
                actor = objdir + "/actor.gas"
                if actor.lower() in reader.files:
                    convert_actor_file(w, actor, templates, cfg, region)
        place_checkpoints(w, cfg)
        if w.derived:
            out["world/contentdb/templates/pss/pss_derived_%s.gas" % key] = gas.encode(
                "// Generated by build_content.py: quest-carrier templates for %s\n\n" % mapdir
                + "\n".join(w.derived))
        out.update(w.out)
        for k, v in sorted(w.stats.items()):
            log("  %-28s %d" % (k, v))
        for k, v in w.kept.most_common():
            log("  kept static: %-40s %d" % (k, v))
        report["worlds"][key] = {"converted": dict(w.stats), "kept_static": dict(w.kept),
                                 "bosses": dict(w.bosses)}

    patch_player_templates(logic, templates, cfg, out)
    patch_formulas(logic, cfg, out)

    # The mod's own components and templates.
    for root, _, files in os.walk(os.path.join(CONTENT, "world")):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, CONTENT).replace(os.sep, "/")
            out[rel] = open(src, "rb").read()

    os.makedirs(a.out, exist_ok=True)
    name = "pss_content_v%d.dsres" % cfg["version"]
    dest = os.path.join(a.out, name)
    tank.write_tank(dest, out, title="Persistent Siege Server content v%d" % cfg["version"],
                    author="Persistent Siege", priority=tank.PRIORITY_USER,
                    product_version=logic.product_version, minimum_version=logic.minimum_version)
    log("wrote %s (%d files, %.1f MB)" % (dest, len(out), os.path.getsize(dest) / 1e6))

    # Read the tank back to catch writer bugs before anyone ships it.
    check = tank.TankReader(dest)
    for p, data in out.items():
        if check.read(p) != data:
            raise SystemExit("verification failed for %s" % p)
    log("verified %d files" % len(out))

    if a.report:
        report["files"] = sorted(out)
        json.dump(report, open(a.report, "w"), indent=2)


if __name__ == "__main__":
    main()
