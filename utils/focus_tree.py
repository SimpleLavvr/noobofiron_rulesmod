#!/usr/bin/env python3
"""
HOI4 Focus Tree Toolkit

Unified CLI for parsing, diagnosing, and fixing focus tree layouts.

Subcommands:
  diagnose   — detect all collisions across all scenarios
  fix        — auto-fix ahist=OFF collisions
  parse      — print the full tree with positions
  overlaps   — full overlap analysis
  simulate   — simulate a scenario with offsets
  bboxes     — bounding box visualization
  spacing    — inter-branch spacing analysis by position root
"""
# pylint: disable=too-many-lines  # self-contained CLI toolkit

import re
import sys
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Offset:
    """A conditional offset block."""
    x: int = 0
    y: int = 0
    trigger_raw: str = ""  # raw trigger text for display


@dataclass
class Focus:  # pylint: disable=too-many-instance-attributes
    """A single focus node."""
    id: str = ""
    x: int = 0
    y: int = 0
    relative_position_id: Optional[str] = None
    offsets: list = field(default_factory=list)  # list of Offset
    allow_branch_raw: str = ""  # raw allow_branch text
    has_allow_branch: bool = False
    prerequisite_ids: list = field(default_factory=list)
    mutually_exclusive_ids: list = field(default_factory=list)
    line_number: int = 0
    # computed
    abs_x: Optional[int] = None
    abs_y: Optional[int] = None


@dataclass
class BranchCondition:
    """Parsed visibility condition for a branch."""
    requires_dlc: Optional[str] = None      # DLC name required, or None
    requires_not_dlc: Optional[str] = None   # DLC name that must NOT be present
    requires_noi_ahistorical: bool = False    # needs noi_allow_ahistorical = allowed
    always_hidden: bool = False               # always = no
    requires_flag: Optional[str] = None       # has_country_flag required
    hides_after_focus: list = field(default_factory=list)  # hidden when these focuses completed
    raw: str = ""


COLLISION_DISTANCE = 2
FOCUS_RADIUS = 2       # visual overlap threshold for print_overlap_report
GAP_THRESHOLD = 5      # > 5 empty columns = suspicious gap
PX_PER_Y = 46          # pixels per Y grid unit (continuous_focus_position)
PX_PER_X = 93          # pixels per X grid unit


# ---------------------------------------------------------------------------
# Tokenizer / parser core
# ---------------------------------------------------------------------------

def tokenize(text: str):
    """Tokenize Paradox script into a flat list of tokens (strings, braces, equals)."""
    tokens = []
    i = 0
    while i < len(text):
        c = text[i]
        # skip comments
        if c == '#':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        # whitespace
        if c in ' \t\r\n':
            i += 1
            continue
        # braces and equals
        if c in '{}=<>':
            # handle <= >=
            if c in '<>' and i + 1 < len(text) and text[i + 1] == '=':
                tokens.append(c + '=')
                i += 2
                continue
            tokens.append(c)
            i += 1
            continue
        # quoted string
        if c == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                j += 1
            tokens.append(text[i:j + 1])
            i = j + 1
            continue
        # unquoted word
        j = i
        while j < len(text) and text[j] not in ' \t\r\n{}=<>#"':
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def parse_block(tokens: list, pos: int):
    """
    Parse a block (content between { }) into a list of entries.
    Each entry is either:
      - ("key", value) where value is a string or a parsed sub-block (list)
      - ("value", string) for bare values
    Returns (entries, new_pos).
    """
    entries = []
    while pos < len(tokens) and tokens[pos] != '}':
        token = tokens[pos]
        # Check if next token is = (key-value pair)
        if pos + 1 < len(tokens) and tokens[pos + 1] == '=':
            key = token
            pos += 2  # skip key and =
            if pos < len(tokens) and tokens[pos] == '{':
                pos += 1  # skip {
                sub_block, pos = parse_block(tokens, pos)
                if pos < len(tokens) and tokens[pos] == '}':
                    pos += 1
                entries.append((key, sub_block))
            else:
                entries.append((key, tokens[pos]))
                pos += 1
        elif token == '{':
            pos += 1
            sub_block, pos = parse_block(tokens, pos)
            if pos < len(tokens) and tokens[pos] == '}':
                pos += 1
            entries.append(('_block', sub_block))
        else:
            entries.append(('_value', token))
            pos += 1
    return entries, pos


def block_to_dict(entries: list) -> dict:
    """Convert parsed entries to a dict (last value wins for duplicate keys, except lists)."""
    d = {}
    for key, val in entries:
        if key in d:
            # make it a list
            if not isinstance(d[key], list):
                d[key] = [d[key]]
            d[key].append(val)
        else:
            d[key] = val
    return d


def extract_focus_ids_from_block(entries: list) -> list:
    """Extract focus IDs from a prerequisite/mutually_exclusive block."""
    ids = []
    for key, val in entries:
        if key == 'focus':
            ids.append(val)
    return ids


def entries_to_raw_text(entries: list, indent: int = 0) -> str:
    """Reconstruct approximate raw text from parsed entries."""
    lines = []
    prefix = "\t" * indent
    for key, val in entries:
        if key in ('_value', '_block'):
            if isinstance(val, list):
                lines.append(f"{prefix}{{")
                lines.append(entries_to_raw_text(val, indent + 1))
                lines.append(f"{prefix}}}")
            else:
                lines.append(f"{prefix}{val}")
        elif isinstance(val, list):
            lines.append(f"{prefix}{key} = {{")
            lines.append(entries_to_raw_text(val, indent + 1))
            lines.append(f"{prefix}}}")
        else:
            lines.append(f"{prefix}{key} = {val}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Focus extraction and parsing
# ---------------------------------------------------------------------------

def _parse_offset_block(entries):
    """Parse an offset sub-block into an Offset object."""
    off = Offset()
    for okey, oval in entries:
        if okey == 'x':
            try:
                off.x = int(oval)
            except (ValueError, TypeError):
                pass
        elif okey == 'y':
            try:
                off.y = int(oval)
            except (ValueError, TypeError):
                pass
        elif okey == 'trigger':
            if isinstance(oval, list):
                off.trigger_raw = entries_to_raw_text(oval)
    return off


def _safe_int(val, default=0):
    """Parse an int value, returning default on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def parse_focus(entries: list, line_hint: int = 0) -> Focus:
    """Parse a focus block's entries into a Focus object."""
    f = Focus(line_number=line_hint)
    for key, val in entries:
        if key == 'id':
            f.id = val
        elif key == 'x':
            f.x = _safe_int(val)
        elif key == 'y':
            f.y = _safe_int(val)
        elif key == 'relative_position_id':
            f.relative_position_id = val
        elif key == 'offset':
            if isinstance(val, list):
                f.offsets.append(_parse_offset_block(val))
        elif key == 'allow_branch':
            f.has_allow_branch = True
            new_raw = entries_to_raw_text(val) if isinstance(val, list) else str(val)
            if f.allow_branch_raw:
                f.allow_branch_raw += "\n" + new_raw
            else:
                f.allow_branch_raw = new_raw
        elif key == 'prerequisite':
            if isinstance(val, list):
                f.prerequisite_ids.extend(extract_focus_ids_from_block(val))
        elif key == 'mutually_exclusive':
            if isinstance(val, list):
                f.mutually_exclusive_ids.extend(extract_focus_ids_from_block(val))
    return f


def find_line_number(text: str, char_pos: int) -> int:
    """Find line number for a character position."""
    return text[:char_pos].count('\n') + 1


def extract_focuses_raw(text: str) -> list:
    """
    Extract focus blocks from text using regex to find 'focus = {' at the right indentation,
    then use the tokenizer on each block. Returns list of (focus_entries, line_number).
    """
    results = []
    # Find all top-level focus blocks (inside the focus_tree)
    # Pattern: focus = { at any indent level (some focuses have no indentation)
    pattern = re.compile(r'^[ \t]*focus\s*=\s*\{', re.MULTILINE)

    for match in pattern.finditer(text):
        start = match.start()
        line_num = find_line_number(text, start)

        # Find matching closing brace
        brace_start = match.end() - 1  # position of {
        depth = 1
        i = brace_start + 1
        while i < len(text) and depth > 0:
            c = text[i]
            if c == '#':
                while i < len(text) and text[i] != '\n':
                    i += 1
                continue
            if c == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 1
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            i += 1

        block_text = text[brace_start + 1:i - 1]
        tokens = tokenize(block_text)
        entries, _ = parse_block(tokens, 0)
        results.append((entries, line_num))

    return results


def find_focus_blocks(text: str) -> list:
    """
    Find all focus blocks in text with their exact positions.
    Returns list of (focus_id, start_pos, end_pos, block_text).
    """
    results = []
    pattern = re.compile(r'^[ \t]*focus\s*=\s*\{', re.MULTILINE)

    for match in pattern.finditer(text):
        start = match.start()
        brace_start = match.end() - 1
        depth = 1
        i = brace_start + 1
        in_comment = False
        in_string = False

        while i < len(text) and depth > 0:
            c = text[i]
            if in_comment:
                if c == '\n':
                    in_comment = False
            elif in_string:
                if c == '"':
                    in_string = False
            elif c == '#':
                in_comment = True
            elif c == '"':
                in_string = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            i += 1

        block_text = text[start:i]

        # Extract focus ID
        id_match = re.search(r'\bid\s*=\s*(\S+)', block_text)
        if id_match:
            focus_id = id_match.group(1)
            results.append((focus_id, start, i, block_text))

    return results


# ---------------------------------------------------------------------------
# Offset trigger parsing and evaluation
# ---------------------------------------------------------------------------

def parse_offset_trigger(trigger_raw: str) -> dict:
    """
    Parse an offset trigger into structured conditions.
    Understands NOT { has_completed_focus } blocks.
    Returns dict with keys:
      - requires_completed: list of focus IDs that MUST be completed
      - requires_not_completed: list of focus IDs that must NOT be completed
      - requires_dlc: str or None
      - requires_not_dlc: str or None
      - requires_flag: str or None
      - requires_game_rule: list of (rule, option) tuples
      - obsolete_hide: bool (requires obsolete_focus_branches_visibility = HIDE)
    """
    result = {
        "requires_completed": [],
        "requires_not_completed": [],
        "requires_dlc": None,
        "requires_not_dlc": None,
        "requires_flag": None,
        "requires_game_rule": [],
        "obsolete_hide": False,
    }

    if "obsolete_focus_branches_visibility" in trigger_raw:
        result["obsolete_hide"] = True

    # Parse NOT blocks to understand negation context
    # Find all NOT { ... } blocks and extract has_completed_focus inside them
    not_block_pattern = re.compile(r'NOT\s*=?\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', re.DOTALL)
    not_completed = set()
    not_block_ranges = []
    for m in not_block_pattern.finditer(trigger_raw):
        not_content = m.group(1)
        not_block_ranges.append((m.start(), m.end()))
        for fm in re.finditer(r'has_completed_focus\s*=\s*(\S+)', not_content):
            not_completed.add(fm.group(1))

    # Find all has_completed_focus NOT inside a NOT block
    for m in re.finditer(r'has_completed_focus\s*=\s*(\S+)', trigger_raw):
        focus_id = m.group(1)
        in_not = False
        for ns, ne in not_block_ranges:
            if ns <= m.start() <= ne:
                in_not = True
                break
        if in_not:
            if focus_id not in result["requires_not_completed"]:
                result["requires_not_completed"].append(focus_id)
        else:
            if focus_id not in result["requires_completed"]:
                result["requires_completed"].append(focus_id)

    # DLC: check if inside NOT block
    for m in re.finditer(r'has_dlc\s*=\s*"([^"]+)"', trigger_raw):
        dlc_name = m.group(1)
        in_not = False
        for ns, ne in not_block_ranges:
            if ns <= m.start() <= ne:
                in_not = True
                break
        if in_not:
            result["requires_not_dlc"] = dlc_name
        else:
            result["requires_dlc"] = dlc_name

    # Game rules (for noi_allow_ahistorical etc.)
    for m in re.finditer(r'has_game_rule\s*=\s*\{[^}]*rule\s*=\s*(\S+)[^}]*option\s*=\s*(\S+)[^}]*\}',
                         trigger_raw, re.DOTALL):
        result["requires_game_rule"].append((m.group(1), m.group(2)))

    # Flags
    flag_match = re.search(r'has_country_flag\s*=\s*(\S+)', trigger_raw)
    if flag_match:
        result["requires_flag"] = flag_match.group(1)

    return result


def eval_offset_trigger(parsed_trigger: dict, completed_focuses: set,
                        dlc_owned: set, obsolete_hide: bool = True,
                        game_rules: dict = None) -> bool:
    """Evaluate whether an offset trigger is active given game state."""
    t = parsed_trigger
    if game_rules is None:
        # Default: noi_allow_ahistorical = forbidden (default game rule)
        game_rules = {"noi_allow_ahistorical": "noi_rule_ahistorical_forbidden"}

    if t["obsolete_hide"] and not obsolete_hide:
        return False

    if t["requires_dlc"] and t["requires_dlc"] not in dlc_owned:
        return False

    if t["requires_not_dlc"] and t["requires_not_dlc"] in dlc_owned:
        return False

    # Positive: all listed focuses must be completed
    for cf in t["requires_completed"]:
        if cf not in completed_focuses:
            return False

    # Negative: none of these may be completed
    for cf in t["requires_not_completed"]:
        if cf in completed_focuses:
            return False

    # Game rules
    for rule, option in t["requires_game_rule"]:
        if rule in game_rules:
            if game_rules[rule] != option:
                return False
        elif option.endswith("_forbidden") or option == "HIDE":
            pass  # default is usually the restrictive option
        else:
            return False

    if t["requires_flag"]:
        return False

    return True


# ---------------------------------------------------------------------------
# Position computation
# ---------------------------------------------------------------------------

def compute_positions_map(focuses: dict, completed_focuses: set = None,
                          dlc_owned: set = None, obsolete_hide: bool = True,
                          game_rules: dict = None) -> dict:
    """
    Compute absolute X, Y for each focus. Returns dict {focus_id: (abs_x, abs_y)}.
    Does NOT modify the Focus objects.
    """
    if completed_focuses is None:
        completed_focuses = set()
    if dlc_owned is None:
        dlc_owned = set()

    positions = {}  # focus_id -> (abs_x, abs_y)
    computing = set()  # to detect cycles

    def compute(focus_id: str):
        if focus_id in positions:
            return positions[focus_id]
        if focus_id in computing:
            return None  # circular
        computing.add(focus_id)

        f = focuses.get(focus_id)
        if f is None:
            return None

        # Active conditional offsets
        extra_x, extra_y = 0, 0
        for off in f.offsets:
            parsed = parse_offset_trigger(off.trigger_raw)
            if eval_offset_trigger(parsed, completed_focuses, dlc_owned, obsolete_hide, game_rules):
                extra_x += off.x
                extra_y += off.y

        if f.relative_position_id and f.relative_position_id in focuses:
            parent_pos = compute(f.relative_position_id)
            if parent_pos:
                pos = (parent_pos[0] + f.x + extra_x, parent_pos[1] + f.y + extra_y)
            else:
                pos = (f.x + extra_x, f.y + extra_y)
        elif f.relative_position_id and f.relative_position_id not in focuses:
            pos = (f.x + extra_x, f.y + extra_y)
        else:
            pos = (f.x + extra_x, f.y + extra_y)

        positions[focus_id] = pos
        return pos

    for fid in focuses:
        compute(fid)

    return positions


def compute_absolute_positions(focuses: dict, completed_focuses: set = None,
                               dlc_owned: set = None, obsolete_hide: bool = True):
    """
    Compute absolute positions and store them on the Focus objects.
    Convenience wrapper around compute_positions_map.
    """
    positions = compute_positions_map(focuses, completed_focuses, dlc_owned, obsolete_hide)
    for fid, (ax, ay) in positions.items():
        focuses[fid].abs_x = ax
        focuses[fid].abs_y = ay


# ---------------------------------------------------------------------------
# Branch analysis
# ---------------------------------------------------------------------------

def identify_branches(focuses: dict) -> dict:
    """
    Group focuses by their allow_branch root.
    Returns dict of branch_root_id -> list of focus_ids in that branch.

    A focus that is itself a branch root is NOT absorbed into a parent branch.
    This prevents always_hidden sub-branches from leaking into visible parent branches.
    """
    branch_roots = {fid for fid, f in focuses.items() if f.has_allow_branch}

    branches = {}
    for root_id in branch_roots:
        branch_members = set()
        queue = [root_id]
        while queue:
            current = queue.pop()
            if current in branch_members:
                continue
            # Don't cross into another branch root (except our own root)
            if current != root_id and current in branch_roots:
                continue
            branch_members.add(current)
            # Find focuses that have current as a prerequisite
            for fid, f in focuses.items():
                if current in f.prerequisite_ids and fid not in branch_members:
                    queue.append(fid)
        branches[root_id] = sorted(branch_members)

    return branches


def parse_branch_condition(allow_branch_raw: str) -> BranchCondition:
    """Parse an allow_branch text into structured conditions."""
    cond = BranchCondition(raw=allow_branch_raw)
    text = allow_branch_raw

    if "always = no" in text.lower():
        cond.always_hidden = True
        return cond

    if "noi_allow_ahistorical" in text and "noi_rule_ahistorical_allowed" in text:
        cond.requires_noi_ahistorical = True

    # DLC checks
    dlc_match = re.search(r'has_dlc\s*=\s*"([^"]+)"', text)
    if dlc_match:
        dlc_name = dlc_match.group(1)
        # Check if it's inside a NOT block
        # Simple heuristic: if "NOT" appears before "has_dlc" at a similar or higher scope
        not_pos = text.find("NOT")
        dlc_pos = text.find("has_dlc")
        if not_pos != -1 and not_pos < dlc_pos:
            # Check if the NOT is at the top level (not inside an IF/limit)
            # Count braces to see if NOT is at the top level
            depth = 0
            for ch in text[:not_pos]:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
            if depth == 0:  # NOT is at top level
                cond.requires_not_dlc = dlc_name
            else:
                cond.requires_dlc = dlc_name
        else:
            cond.requires_dlc = dlc_name

    # Country flags
    flag_match = re.search(r'has_country_flag\s*=\s*(\S+)', text)
    if flag_match:
        cond.requires_flag = flag_match.group(1)

    # "hidden after completing" focuses (inside obsolete_focus_branches_visibility IF blocks)
    # These are the mutually exclusive paths that hide each other
    hide_pattern = re.compile(r'has_completed_focus\s*=\s*(\S+)')
    for m in hide_pattern.finditer(text):
        cond.hides_after_focus.append(m.group(1))

    return cond


# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------

def _is_branch_visible(branch_conditions, root_id, dlc_owned, noi_ahistorical,
                       chosen_path=None):
    """Check if a branch is visible given the parameter state."""
    cond = branch_conditions.get(root_id, BranchCondition())
    if cond.always_hidden:
        return False
    if cond.requires_noi_ahistorical and not noi_ahistorical:
        return False
    if cond.requires_dlc and cond.requires_dlc not in dlc_owned:
        return False
    if cond.requires_not_dlc and cond.requires_not_dlc in dlc_owned:
        return False
    if cond.requires_flag:
        return False
    if chosen_path and cond.hides_after_focus:
        if chosen_path in cond.hides_after_focus:
            return False
    return True


def _build_visible_set(branches, branch_conditions, always_visible_ids,
                       dlc_owned, noi_ahistorical, chosen_path=None):
    """Build the set of visible focus IDs for a given config."""
    visible = set(always_visible_ids)
    for root_id, members in branches.items():
        if _is_branch_visible(branch_conditions, root_id, dlc_owned,
                              noi_ahistorical, chosen_path):
            visible.update(members)
    return visible


def _prepare_scenario_params(focuses, branches):
    """Prepare branch conditions, DLC info, and political roots for scenario generation."""
    branch_conditions = {}
    for root_id in branches:
        root = focuses.get(root_id)
        if root and root.has_allow_branch:
            branch_conditions[root_id] = parse_branch_condition(root.allow_branch_raw)
        else:
            branch_conditions[root_id] = BranchCondition()

    dlcs = set()
    for cond in branch_conditions.values():
        if cond.requires_dlc:
            dlcs.add(cond.requires_dlc)
        if cond.requires_not_dlc:
            dlcs.add(cond.requires_not_dlc)

    all_branch_members = set()
    for members in branches.values():
        all_branch_members.update(members)
    always_visible_ids = {f.id for f in focuses.values() if f.id not in all_branch_members}

    political_roots = set()
    for fid, f in focuses.items():
        if f.has_allow_branch and f.mutually_exclusive_ids:
            for me_id in f.mutually_exclusive_ids:
                if me_id in branch_conditions:
                    political_roots.add(fid)
                    political_roots.add(me_id)

    return branch_conditions, dlcs, always_visible_ids, political_roots


def generate_scenarios(focuses: dict, branches: dict):
    """
    Generate all valid visibility scenarios including political path choices.
    Returns list of (scenario_name, visible_focus_ids, positions_map).
    """
    branch_conditions, dlcs, always_visible_ids, political_roots = \
        _prepare_scenario_params(focuses, branches)

    dlc_list = sorted(dlcs)
    dlc_combos = [frozenset(dlc_list[j] for j in range(len(dlc_list)) if i & (1 << j))
                  for i in range(1 << len(dlc_list))]

    scenarios = []
    for dlc_owned in dlc_combos:
        for noi_ahistorical in [False, True]:
            available_paths = {r for r in political_roots
                              if _is_branch_visible(branch_conditions, r, dlc_owned,
                                                    noi_ahistorical)}

            visible = _build_visible_set(branches, branch_conditions, always_visible_ids,
                                         dlc_owned, noi_ahistorical)

            dlc_str = ", ".join(sorted(dlc_owned)) if dlc_owned else "no DLC"
            noi_str = "ahist=ON" if noi_ahistorical else "ahist=OFF"
            game_rules = {
                "noi_allow_ahistorical": "noi_rule_ahistorical_allowed" if noi_ahistorical else "noi_rule_ahistorical_forbidden"
            }
            positions = compute_positions_map(focuses, set(), dlc_owned, obsolete_hide=False,
                                              game_rules=game_rules)
            scenarios.append((f"[{dlc_str}] [{noi_str}] [initial]", visible, positions))

            for chosen in available_paths:
                visible = _build_visible_set(branches, branch_conditions, always_visible_ids,
                                             dlc_owned, noi_ahistorical, chosen)
                positions = compute_positions_map(focuses, {chosen}, dlc_owned, obsolete_hide=True,
                                                  game_rules=game_rules)
                scenarios.append((f"[{dlc_str}] [{noi_str}] [path: {chosen}]", visible, positions))

    return scenarios


# ---------------------------------------------------------------------------
# Display helpers (parser side)
# ---------------------------------------------------------------------------

def print_tree(focuses: dict, branches: dict, show_branches: bool = True):
    """Print the focus tree with positions."""
    # Sort by absolute position (y first, then x)
    sorted_focuses = sorted(
        focuses.values(),
        key=lambda f: (f.abs_y or 0, f.abs_x or 0)
    )

    # Build branch membership lookup
    focus_to_branch = {}
    if show_branches:
        for root_id, members in branches.items():
            for mid in members:
                if mid not in focus_to_branch:
                    focus_to_branch[mid] = root_id

    print(f"\n{'ID':<60} {'abs(x,y)':<15} {'rel_to':<45} {'rel(x,y)':<12} {'branch':<30}")
    print("=" * 170)

    for f in sorted_focuses:
        branch = focus_to_branch.get(f.id, "")
        rel_info = f.relative_position_id or "(absolute)"
        abs_pos = f"({f.abs_x}, {f.abs_y})"
        rel_pos = f"({f.x}, {f.y})"

        line = f"{f.id:<60} {abs_pos:<15} {rel_info:<45} {rel_pos:<12}"
        if branch:
            line += f" [{branch}]"
        if f.has_allow_branch:
            line += " <BRANCH ROOT>"
        if f.offsets:
            off_info = "; ".join(f"offset({o.x},{o.y})" for o in f.offsets)
            line += f" +offsets: {off_info}"

        print(line)


def _deduplicate_scenarios(scenarios):
    """Deduplicate scenarios by visible set + positions. Returns (unique_scenarios, seen_dict)."""
    seen = {}
    unique_scenarios = []
    for name, visible, positions in scenarios:
        pos_key = frozenset((fid, positions.get(fid, (0, 0))) for fid in visible)
        if pos_key in seen:
            seen[pos_key].append(name)
        else:
            seen[pos_key] = [name]
            unique_scenarios.append((name, visible, positions))
    return unique_scenarios, seen


def _scenario_display_name(visible, positions, seen):
    """Get display name for a scenario (including alias count)."""
    aliases = seen[frozenset((fid, positions.get(fid, (0, 0))) for fid in visible)]
    if len(aliases) == 1:
        return aliases[0]
    return f"{aliases[0]} (and {len(aliases)-1} identical)"


def _find_radius_collisions(visible, positions, radius):
    """Find collisions within given X radius at same Y. Returns list of (x1, x2, y, fid1, fid2)."""
    by_y = {}
    for fid in visible:
        pos = positions.get(fid)
        if pos:
            by_y.setdefault(pos[1], []).append((pos[0], fid))

    collisions = []
    for y, entries in by_y.items():
        entries.sort()
        for i, (x1, fid1) in enumerate(entries):
            for x2, fid2 in entries[i + 1:]:
                if x2 - x1 < radius:
                    collisions.append((x1, x2, y, fid1, fid2))
                else:
                    break
    return collisions


def _find_near_overlaps(visible, positions):
    """Find near-overlaps (distance 1-2 on X, same Y)."""
    by_y = {}
    for fid in visible:
        pos = positions.get(fid)
        if pos:
            by_y.setdefault(pos[1], []).append((pos[0], fid))

    near = []
    for y, entries in by_y.items():
        entries.sort()
        for i in range(len(entries) - 1):
            x1, fid1 = entries[i]
            x2, fid2 = entries[i + 1]
            if 0 < x2 - x1 <= 2:
                near.append((x1, x2, y, fid1, fid2))
    return near


def print_overlap_report(focuses: dict, branches: dict):
    """Find overlaps across all valid visibility scenarios."""
    print("\n\n=== SCENARIO-BASED OVERLAP ANALYSIS ===")

    scenarios = generate_scenarios(focuses, branches)
    unique_scenarios, seen = _deduplicate_scenarios(scenarios)
    print(f"\n{len(scenarios)} total combos -> {len(unique_scenarios)} unique states\n")

    any_overlap = False
    for name, visible, positions in unique_scenarios:
        collisions = _find_radius_collisions(visible, positions, FOCUS_RADIUS)
        if collisions:
            any_overlap = True
            all_names = _scenario_display_name(visible, positions, seen)
            print(f"  SCENARIO: {all_names}")
            print(f"    Visible: {len(visible)} focuses, {len(collisions)} collision(s) (within {FOCUS_RADIUS} X units):")
            for x1, x2, y, fid1, fid2 in collisions[:25]:
                dist = x2 - x1
                print(f"      ({x1},{y})<-{dist}->({x2},{y}): {fid1} vs {fid2}")
            if len(collisions) > 25:
                print(f"      ... and {len(collisions) - 25} more")
            print()

    if not any_overlap:
        print("  No overlaps in any scenario!")

    print("\n=== NEAR-OVERLAP ANALYSIS (distance <= 2 on X, same Y) ===")
    any_near = False
    for name, visible, positions in unique_scenarios:
        near_overlaps = _find_near_overlaps(visible, positions)
        if near_overlaps:
            any_near = True
            all_names = _scenario_display_name(visible, positions, seen)
            print(f"\n  SCENARIO: {all_names}")
            for x1, x2, y, fid1, fid2 in near_overlaps[:20]:
                print(f"    ({x1},{y}) {fid1}  <-{x2-x1}->  ({x2},{y}) {fid2}")
            if len(near_overlaps) > 20:
                print(f"    ... and {len(near_overlaps) - 20} more")

    if not any_near:
        print("  No near-overlaps found!")


def find_active_offset_ancestor(focus_id: str, focuses: dict,
                                 completed_focuses: set, dlc_owned: set,
                                 obsolete_hide: bool = True) -> Optional[tuple]:
    """
    Walk up the relative_position_id chain from focus_id.
    Return the first ancestor (including self) that has an active offset in this scenario.
    Returns (ancestor_id, offset_index, offset_obj) or None.
    """
    visited = set()
    current = focus_id
    while current and current not in visited:
        visited.add(current)
        f = focuses.get(current)
        if not f:
            break
        for i, off in enumerate(f.offsets):
            parsed = parse_offset_trigger(off.trigger_raw)
            if eval_offset_trigger(parsed, completed_focuses, dlc_owned, obsolete_hide):
                return (current, i, off)
        current = f.relative_position_id
    return None


def _collect_fix_needs(focuses, branches):
    """Collect fix needs across all scenarios. Returns (fix_needs dict, scenarios list)."""
    scenarios = generate_scenarios(focuses, branches)
    fix_needs = {}

    for name, visible, positions in scenarios:
        pos_groups = {}
        for fid in visible:
            pos = positions.get(fid)
            if pos:
                pos_groups.setdefault(pos, []).append(fid)

        overlaps = {pos: fids for pos, fids in pos_groups.items() if len(fids) > 1}
        if not overlaps:
            continue

        completed = set()
        dlc_owned = set()
        if "[path: " in name:
            path = name.split("[path: ")[1].rstrip("]")
            completed = {path}
        obsolete_hide = bool(completed)

        dlc_part = name.split("] [")[0].lstrip("[")
        if dlc_part != "no DLC":
            dlc_owned = set(dlc_part.split(", "))

        for pos, fids in overlaps.items():
            _classify_overlap_pair(focuses, name, pos, fids, completed,
                                   dlc_owned, obsolete_hide, fix_needs)

    return fix_needs, scenarios


def _classify_overlap_pair(focuses, name, pos, fids, completed, dlc_owned,
                           obsolete_hide, fix_needs):
    """Classify each overlapping pair and add to fix_needs."""
    for i, fid_a in enumerate(fids):
        for fid_b in fids[i + 1:]:
            ancestor_a = find_active_offset_ancestor(
                fid_a, focuses, completed, dlc_owned, obsolete_hide)
            ancestor_b = find_active_offset_ancestor(
                fid_b, focuses, completed, dlc_owned, obsolete_hide)

            if ancestor_a and not ancestor_b:
                key = (ancestor_a[0], ancestor_a[1])
                detail = f"{fid_a} vs {fid_b} at {pos}"
                fix_needs.setdefault(key, []).append((name, detail, fid_a, fid_b))
            elif ancestor_b and not ancestor_a:
                key = (ancestor_b[0], ancestor_b[1])
                detail = f"{fid_b} vs {fid_a} at {pos}"
                fix_needs.setdefault(key, []).append((name, detail, fid_b, fid_a))
            elif ancestor_a and ancestor_b:
                key_a = (ancestor_a[0], ancestor_a[1])
                key_b = (ancestor_b[0], ancestor_b[1])
                detail = f"{fid_a} vs {fid_b} at {pos}"
                fix_needs.setdefault(key_a, []).append((name, detail, fid_a, fid_b))
                fix_needs.setdefault(key_b, []).append((name, detail, fid_b, fid_a))
            else:
                key = (fid_a, -1)
                detail = f"{fid_a} vs {fid_b} at {pos} (no active offset)"
                fix_needs.setdefault(key, []).append((name, detail, fid_a, fid_b))


def _get_descendants(focuses, focus_id, cache):
    """Get all descendant IDs (via relative_position_id) with caching."""
    if focus_id in cache:
        return cache[focus_id]
    desc = {focus_id}
    for other_id, other_f in focuses.items():
        if other_f.relative_position_id == focus_id:
            desc.update(_get_descendants(focuses, other_id, cache))
    cache[focus_id] = desc
    return desc


def _find_min_clear_delta(our_x, y, visible, positions, desc):
    """Find minimum delta to clear an overlap at position (our_x, y)."""
    occupied_xs = set()
    for fid in visible:
        if fid not in desc:
            fpos = positions.get(fid)
            if fpos and fpos[1] == y:
                occupied_xs.add(fpos[0])

    for delta in range(1, 50):
        if (our_x + delta) not in occupied_xs:
            return delta
        if (our_x - delta) not in occupied_xs:
            return delta
    return 0


def _compute_max_delta(scenarios, scenario_names, desc):
    """Compute the max delta needed to clear all overlaps for an offset group."""
    max_delta = 0
    for scenario_name in scenario_names:
        for sname, visible, positions in scenarios:
            if sname != scenario_name:
                continue

            pos_groups = {}
            for fid in visible:
                pos = positions.get(fid)
                if pos:
                    pos_groups.setdefault(pos, []).append(fid)

            for pos, fids in pos_groups.items():
                if len(fids) < 2:
                    continue
                our_fids = [fid for fid in fids if fid in desc]
                their_fids = [fid for fid in fids if fid not in desc]
                if our_fids and their_fids:
                    delta = _find_min_clear_delta(pos[0], pos[1], visible, positions, desc)
                    max_delta = max(max_delta, delta)
            break
    return max_delta


def _print_fix_summary(fix_needs, focuses):
    """Print the strategy summary for fix needs."""
    print("\n=== STRATEGY SUMMARY ===")
    print()
    ranked = sorted(fix_needs.items(), key=lambda x: -len(x[1]))
    print("  Offset blocks ranked by impact (fix these first):")
    for (focus_id, offset_idx), entries in ranked[:10]:
        n_scenarios = len(set(e[0] for e in entries))
        n_collisions = len(set((a, b) for _, _, a, b in entries))
        if offset_idx == -1:
            print(f"    {focus_id} BASE POS: {n_collisions} collisions in {n_scenarios} scenarios")
        else:
            off = focuses[focus_id].offsets[offset_idx]
            print(f"    {focus_id} offset#{offset_idx} (x={off.x}): "
                  f"{n_collisions} collisions in {n_scenarios} scenarios")


def suggest_fixes(focuses: dict, branches: dict):
    """Analyze all overlaps across scenarios and suggest consolidated offset fixes."""
    print("\n\n=== SUGGESTED FIXES ===")

    fix_needs, scenarios = _collect_fix_needs(focuses, branches)
    if not fix_needs:
        print("  No fixes needed!")
        return

    desc_cache = {}
    print(f"\nFound {len(fix_needs)} offset block(s) involved in overlaps:\n")

    for (focus_id, offset_idx), entries in sorted(fix_needs.items(), key=lambda x: -len(x[1])):
        f = focuses[focus_id]
        scenarios_affected = set(e[0] for e in entries)

        if offset_idx == -1:
            print(f"  BASE POSITION: {focus_id} (line {f.line_number})")
            print(f"    Current: x={f.x}, y={f.y}, relative_to={f.relative_position_id}")
            print(f"    Affected scenarios: {len(scenarios_affected)}")
            seen_details = set()
            for _, detail, _, _ in entries:
                if detail not in seen_details:
                    seen_details.add(detail)
                    print(f"      {detail}")
                    if len(seen_details) >= 3:
                        break
            print()
            continue

        off = f.offsets[offset_idx]
        desc = _get_descendants(focuses, focus_id, desc_cache)
        max_delta_needed = _compute_max_delta(scenarios, scenarios_affected, desc)

        suggested = off.x + max_delta_needed
        suggested_neg = off.x - max_delta_needed

        print(f"  OFFSET #{offset_idx} on {focus_id} (line {f.line_number})")
        print(f"    Current: x={off.x}")
        print(f"    Trigger: {off.trigger_raw.strip()[:100]}")
        print(f"    {len(scenarios_affected)} scenarios, {len(set((a,b) for _,_,a,b in entries))} collisions")
        print(f"    Min delta to fix: {max_delta_needed}")
        print(f"    -> Suggested: x={suggested} (shift right) or x={suggested_neg} (shift left)")
        print()

    _print_fix_summary(fix_needs, focuses)


def print_bounding_boxes(focuses: dict, branches: dict):
    """Print ASCII visualization of branch bounding boxes on X axis."""
    print("\n\n=== X-AXIS LAYOUT (bounding boxes) ===")

    # Collect ranges for each group
    groups = {}

    # NOI-hidden branches
    for root_id, members in branches.items():
        root = focuses.get(root_id)
        if not root:
            continue
        is_noi = 'noi_allow_ahistorical' in root.allow_branch_raw or 'always = no' in root.allow_branch_raw
        xs = [focuses[m].abs_x for m in members if m in focuses and focuses[m].abs_x is not None]
        if xs:
            label = f"{'[NOI] ' if is_noi else ''}{root_id}"
            groups[label] = (min(xs), max(xs))

    # Always visible
    all_branch = set()
    for members in branches.values():
        all_branch.update(members)
    visible_xs = [f.abs_x for f in focuses.values() if f.id not in all_branch and f.abs_x is not None]
    if visible_xs:
        groups["[ALWAYS] unbranched"] = (min(visible_xs), max(visible_xs))

    if not groups:
        print("  No groups to display.")
        return

    # Find global range
    all_mins = [v[0] for v in groups.values()]
    all_maxs = [v[1] for v in groups.values()]
    global_min = min(all_mins)
    global_max = max(all_maxs)

    width = 120
    span = max(global_max - global_min, 1)

    def scale(x):
        return int((x - global_min) / span * (width - 1))

    print(f"  X range: {global_min} to {global_max}")
    print()

    for label, (xmin, xmax) in sorted(groups.items(), key=lambda x: x[1][0]):
        left = scale(xmin)
        right = scale(xmax)
        bbox_bar = " " * left + "[" + "=" * max(right - left - 1, 0) + "]"
        print(f"  {label:<60} {bbox_bar}  ({xmin} to {xmax})")


# ---------------------------------------------------------------------------
# Autofixer: collision detection, solving, and fixing
# ---------------------------------------------------------------------------

def build_descendant_map(focuses: dict) -> dict:
    """Build a map from focus_id to the set of all its descendants (via relative_position_id)."""
    children = {}
    for fid, f in focuses.items():
        if f.relative_position_id and f.relative_position_id in focuses:
            children.setdefault(f.relative_position_id, set()).add(fid)

    cache = {}
    def get(fid):
        if fid in cache:
            return cache[fid]
        desc = {fid}
        for c in children.get(fid, set()):
            desc.update(get(c))
        cache[fid] = desc
        return desc

    for fid in focuses:
        get(fid)
    return cache


def find_collisions(visible: set, positions: dict) -> list:
    """Return list of (fid1, fid2, pos1, pos2, dist) for all collisions within COLLISION_DISTANCE."""
    by_y = {}
    for fid in visible:
        pos = positions.get(fid)
        if pos:
            by_y.setdefault(pos[1], []).append((pos[0], fid))

    collisions = []
    for y, entries in by_y.items():
        entries.sort()
        for i, (x1, fid1) in enumerate(entries):
            for x2, fid2 in entries[i + 1:]:
                if x2 - x1 < COLLISION_DISTANCE:
                    collisions.append((fid1, fid2, (x1, y), (x2, y), x2 - x1))
                else:
                    break
    return collisions


def parse_scenario_params(name: str) -> tuple:
    """Parse scenario name string into (completed_focuses, dlc_owned, noi_ahistorical)."""
    completed = set()
    dlc = set()
    noi = False

    if "[path: " in name:
        path = name.split("[path: ")[1].split("]")[0]
        completed = {path}

    dlc_part = name.split("] [")[0].lstrip("[")
    if dlc_part != "no DLC":
        dlc = set(dlc_part.split(", "))

    noi = "ahist=ON" in name
    return completed, dlc, noi


def get_active_offsets(focuses, completed, dlc, obsolete_hide, game_rules=None):
    """Return dict of fid -> [offset_indices] for offsets active in the given game state."""
    active = {}
    for fid, f in focuses.items():
        for i, off in enumerate(f.offsets):
            parsed = parse_offset_trigger(off.trigger_raw)
            if eval_offset_trigger(parsed, completed, dlc, obsolete_hide, game_rules):
                active.setdefault(fid, []).append(i)
    return active


def _compute_forbidden_ranges(movable, fixed, positions):
    """Compute forbidden delta ranges from movable/fixed focus positions."""
    forbidden = []
    by_y_m = {}
    by_y_f = {}
    for mid in movable:
        if mid in positions:
            by_y_m.setdefault(positions[mid][1], []).append(positions[mid][0])
    for fxid in fixed:
        if fxid in positions:
            by_y_f.setdefault(positions[fxid][1], []).append(positions[fxid][0])
    for y, mx_list in by_y_m.items():
        if y not in by_y_f:
            continue
        for mx in mx_list:
            for fx in by_y_f[y]:
                lo = fx - mx - COLLISION_DISTANCE + 1
                hi = fx - mx + COLLISION_DISTANCE - 1
                forbidden.append((lo, hi))
    return forbidden


def solve_offsets_for_scenario(focuses, visible, positions, active_offsets,
                               descendant_map, _verbose=False):
    """
    For each active offset group, compute the delta that resolves all collisions
    with fixed (non-group) focuses. Returns list of (fid, oidx, current_x, new_x).
    """
    # Build offset groups
    offset_groups = {}
    for fid in active_offsets:
        for oidx in active_offsets[fid]:
            affected = descendant_map.get(fid, {fid}) & visible
            if affected:
                offset_groups[(fid, oidx)] = affected

    # Assign each focus to its most specific group
    focus_to_group = {}
    for key, affected in offset_groups.items():
        for fid in affected:
            if fid not in focus_to_group or len(offset_groups[key]) < len(offset_groups[focus_to_group[fid]]):
                focus_to_group[fid] = key

    results = []
    # Solve largest groups first
    for key in sorted(offset_groups, key=lambda k: -len(offset_groups[k])):
        fid, oidx = key
        movable = offset_groups[key]
        fixed = visible - movable
        current_x = focuses[fid].offsets[oidx].x

        forbidden = _compute_forbidden_ranges(movable, fixed, positions)

        if not forbidden:
            continue

        # Merge
        forbidden.sort()
        merged = []
        for lo, hi in forbidden:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))

        # Find best delta closest to 0, prefer negative (compact)
        best_delta = _pick_delta(merged, 0)
        if best_delta != 0:
            new_x = current_x + best_delta
            results.append((fid, oidx, current_x, new_x))
            # Update positions for subsequent solves
            for mid in movable:
                if mid in positions:
                    positions[mid] = (positions[mid][0] + best_delta, positions[mid][1])

    return results


def _pick_delta(forbidden_ranges, target=0):
    """Pick delta closest to target, avoiding forbidden ranges. Prefer negative."""
    if not forbidden_ranges:
        return target

    # Check if target is valid
    for lo, hi in forbidden_ranges:
        if lo <= target <= hi:
            break
    else:
        return target

    # Try expanding outward from target
    for dist in range(1, 200):
        for candidate in [target - dist, target + dist]:
            valid = True
            for lo, hi in forbidden_ranges:
                if lo <= candidate <= hi:
                    valid = False
                    break
            if valid:
                return candidate

    return target  # give up


def detect_centering_issues(focuses, positions, visible):
    """Find parent focuses whose children midpoint deviates more than 1 unit from the parent X."""
    children_of = {}
    for fid, f in focuses.items():
        if f.relative_position_id and fid in visible:
            children_of.setdefault(f.relative_position_id, []).append(fid)

    issues = []
    for parent_id, child_ids in children_of.items():
        if parent_id not in visible or parent_id not in positions:
            continue
        parent_pos = positions[parent_id]
        direct = [(positions[c][0], c) for c in child_ids
                  if c in positions and positions[c][1] == parent_pos[1] + 1]
        if len(direct) < 2:
            continue
        direct.sort()
        midpoint = (direct[0][0] + direct[-1][0]) / 2
        offset = abs(parent_pos[0] - midpoint)
        if offset > 1:
            issues.append((parent_id, [c for _, c in direct],
                          f"parent X={parent_pos[0]}, children midpoint={midpoint:.0f}, off by {offset:.0f}"))
    return issues


def apply_offset_fixes(text: str, fixes: list, _focuses: dict) -> str:
    """Apply offset x value changes to the file text."""
    focus_blocks = {fid: (s, e, bt) for fid, s, e, bt in find_focus_blocks(text)}

    edits = []  # (abs_start, abs_end, new_value)
    for fid, oidx, old_x, new_x in fixes:
        if fid not in focus_blocks:
            continue
        start, end, block = focus_blocks[fid]

        # Find the Nth offset block
        matches = list(re.finditer(r'\n\t\toffset\s*=\s*\{', block))
        if oidx >= len(matches):
            continue

        # Find extent of this offset block
        off_start = matches[oidx].start()
        depth = 0
        oi = block.index('{', off_start + 1)
        depth = 1
        oi += 1
        while oi < len(block) and depth > 0:
            if block[oi] == '{':
                depth += 1
            elif block[oi] == '}':
                depth -= 1
            oi += 1

        offset_text = block[off_start:oi]
        x_match = re.search(r'(x\s*=\s*)(-?\d+)', offset_text)
        if x_match:
            abs_pos = start + off_start + x_match.start(2)
            abs_end = start + off_start + x_match.end(2)
            edits.append((abs_pos, abs_end, str(new_x)))

    # Apply reverse order
    edits.sort(reverse=True)
    for pos, end, val in edits:
        text = text[:pos] + val + text[end:]
    return text


def generate_offset_suggestion(_focus_id, trigger_desc, delta_x):
    """Generate a ready-to-paste offset block."""
    return f"""		offset = {{
			x = {delta_x}
			y = 0
			trigger = {{
				{trigger_desc}
			}}
		}}"""


# ---------------------------------------------------------------------------
# Autofixer: run — helper functions
# ---------------------------------------------------------------------------


def _compute_ahist_hidden_ids(focuses, branches):
    """Compute focus IDs hidden when noi_allow_ahistorical=OFF."""
    ahist_hidden_roots = set()
    for fid, f in focuses.items():
        if f.has_allow_branch:
            ab = f.allow_branch_raw.lower()
            if 'noi_allow_ahistorical' in ab and 'noi_rule_ahistorical_allowed' in ab:
                ahist_hidden_roots.add(fid)
            elif 'always = no' in ab:
                ahist_hidden_roots.add(fid)
    ahist_hidden_ids = set()
    for root_id in ahist_hidden_roots:
        if root_id in branches:
            ahist_hidden_ids.update(branches[root_id])
    return ahist_hidden_ids


def _categorize_collisions(scenarios):
    """Split collisions into ahist=OFF and ahist=ON buckets."""
    ahist_off_collisions = {}
    ahist_on_collisions = {}
    ahist_off_clean = 0
    ahist_on_clean = 0
    for name, visible, positions in scenarios:
        cols = find_collisions(visible, positions)
        if "ahist=OFF" in name:
            if cols:
                ahist_off_collisions[name] = cols
            else:
                ahist_off_clean += 1
        else:
            if cols:
                ahist_on_collisions[name] = cols
            else:
                ahist_on_clean += 1
    return ahist_off_collisions, ahist_on_collisions, ahist_off_clean, ahist_on_clean


def _find_anchor_issues(focuses, ahist_hidden_ids):
    """Find visible focuses anchored through hidden branches. Returns list of issues."""
    anchor_issues = []
    for fid in focuses:
        if fid in ahist_hidden_ids:
            continue
        chain = []
        cur = fid
        visited = set()
        while cur and cur not in visited:
            visited.add(cur)
            chain.append(cur)
            parent = focuses[cur].relative_position_id if cur in focuses else None
            cur = parent
        hidden_ancestors = [a for a in chain[1:] if a in ahist_hidden_ids]
        if hidden_ancestors:
            phantom_x = 0
            for ancestor in chain:
                if ancestor == fid:
                    continue
                if ancestor in ahist_hidden_ids:
                    phantom_x += focuses[ancestor].x
                else:
                    break
            anchor_issues.append((fid, hidden_ancestors, phantom_x, chain))
    return anchor_issues


def _print_anchor_group(focuses, desc_map, ahist_hidden_ids, root_hidden, members):
    """Print analysis for one anchor group."""
    first_visible = None
    for _fid, _phantom_x, chain in members:
        for i, c in enumerate(chain):
            if c not in ahist_hidden_ids and i + 1 < len(chain) and chain[i + 1] in ahist_hidden_ids:
                first_visible = c
                break
        if first_visible:
            break

    if not first_visible:
        return

    visible_desc = desc_map.get(first_visible, set()) - ahist_hidden_ids
    total_phantom = sum(
        focuses[a].x for a in members[0][2]
        if a in ahist_hidden_ids and a != first_visible
    )

    print(f"  Hidden anchor: {root_hidden}")
    print(f"    First visible descendant: {first_visible} ({len(visible_desc)} visible focuses affected)")
    print(f"    Chain: {' -> '.join(members[0][2][:6])}{'...' if len(members[0][2]) > 6 else ''}")
    print(f"    Phantom X from hidden ancestors: {total_phantom}")
    print(f"    -> Needs offset x=-{total_phantom} on {first_visible} when ahist=OFF")
    print(f"       (or adjust base position of {first_visible})")

    has_noi_offset = False
    for off in focuses[first_visible].offsets:
        if 'noi_allow_ahistorical' in off.trigger_raw and 'forbidden' in off.trigger_raw:
            has_noi_offset = True
            print("    -> Already has NOI offset (good)")
            break
    if not has_noi_offset:
        print("    -> NO NOI offset found — needs one!")
    print()


def _analyze_anchors(focuses, branches, desc_map, ahist_hidden_ids):
    """Find and report visible focuses anchored through hidden branches."""
    print("\n=== ANCHOR ANALYSIS ===\n")
    anchor_issues = _find_anchor_issues(focuses, ahist_hidden_ids)

    if not anchor_issues:
        print("  No anchor issues found.")
        return

    by_root = {}
    for fid, hidden_anc, phantom_x, chain in anchor_issues:
        root_hidden = hidden_anc[-1]
        by_root.setdefault(root_hidden, []).append((fid, phantom_x, chain))

    print(f"  {len(anchor_issues)} visible focus(es) anchored through hidden branches:\n")
    for root_hidden, members in sorted(by_root.items(), key=lambda x: -len(x[1])):
        _print_anchor_group(focuses, desc_map, ahist_hidden_ids, root_hidden, members)


def _autofix_ahist_off(text, file_path, focuses, scenarios, desc_map,
                       ahist_off_collisions, dry_run, verbose):
    """Solve and apply offset fixes for ahist=OFF collisions. Returns (all_fixes, text)."""
    all_fixes = {}
    print("\n=== AUTO-FIX ahist=OFF ===\n")

    for name, visible, positions in scenarios:
        if "ahist=OFF" not in name:
            continue

        completed, dlc, _ = parse_scenario_params(name)
        game_rules = {"noi_allow_ahistorical": "noi_rule_ahistorical_forbidden"}
        active = get_active_offsets(focuses, completed, dlc, bool(completed), game_rules)

        pos_copy = dict(positions)
        results = solve_offsets_for_scenario(
            focuses, visible, pos_copy, active, desc_map, _verbose=verbose)

        for fid, oidx, old_x, new_x in results:
            key = (fid, oidx)
            if key not in all_fixes:
                all_fixes[key] = (fid, oidx, old_x, new_x)
            else:
                existing = all_fixes[key]
                if abs(new_x) < abs(existing[3]):
                    all_fixes[key] = (fid, oidx, old_x, new_x)

    if all_fixes:
        print(f"  {len(all_fixes)} offset fix(es):\n")
        for (fid, oidx), (_, _, old_x, new_x) in sorted(all_fixes.items()):
            trigger = focuses[fid].offsets[oidx].trigger_raw.strip()[:80]
            print(f"    {fid} offset#{oidx}: x={old_x} -> x={new_x}")
            print(f"      trigger: {trigger}\n")

        if not dry_run:
            fix_list = list(all_fixes.values())
            new_text = apply_offset_fixes(text, fix_list, focuses)
            Path(file_path).write_text(new_text, encoding='utf-8-sig')
            print(f"  Applied to {file_path}")
            text = new_text

            # Validate
            raw2 = extract_focuses_raw(new_text)
            focuses2 = {}
            for entries, ln in raw2:
                f2 = parse_focus(entries, ln)
                if f2.id:
                    focuses2[f2.id] = f2
            branches2 = identify_branches(focuses2)
            scenarios2 = generate_scenarios(focuses2, branches2)
            remaining = 0
            for name2, vis2, pos2 in scenarios2:
                if "ahist=OFF" not in name2:
                    continue
                cols2 = find_collisions(vis2, pos2)
                remaining += len(cols2)
            if remaining:
                print(f"  WARNING: {remaining} ahist=OFF collision(s) remain!")
            else:
                print("  CLEAN: 0 ahist=OFF collisions after fix.")
        else:
            print("  (dry run, not applied)")
    else:
        print("  No fixes needed for ahist=OFF.")

    return all_fixes, text


def _find_hidden_anchors(focus_ids, focuses, ahist_hidden_ids):
    """Find hidden ancestors in relative_position_id chains."""
    anchored = set()
    for fid in focus_ids:
        cur = fid
        visited = set()
        while cur and cur not in visited:
            visited.add(cur)
            if cur in ahist_hidden_ids and cur != fid:
                anchored.add(cur)
                break
            cur = focuses[cur].relative_position_id if cur in focuses else None
    return anchored


def _detect_gaps(focuses, scenarios, ahist_hidden_ids):
    """Detect suspicious X-axis gaps in ahist=OFF scenarios."""
    print("\n=== GAP DETECTION (ahist=OFF) ===\n")

    for name, visible, positions in scenarios:
        if "ahist=OFF" not in name or "initial" not in name:
            continue

        visible_positions = [(positions[fid][0], positions[fid][1], fid)
                            for fid in visible if fid in positions]
        if not visible_positions:
            continue

        visible_xs = sorted(set(x for x, _y, _fid in visible_positions))
        if len(visible_xs) < 2:
            continue

        gaps = []
        for i in range(len(visible_xs) - 1):
            gap = visible_xs[i + 1] - visible_xs[i]
            if gap <= GAP_THRESHOLD:
                continue

            right_focuses = [fid for x, _y, fid in visible_positions if x >= visible_xs[i + 1]]

            anchored = _find_hidden_anchors(right_focuses, focuses, ahist_hidden_ids)
            gaps.append({
                'left_x': visible_xs[i],
                'right_x': visible_xs[i + 1],
                'size': gap,
                'right_count': len(right_focuses),
                'anchored_hidden': anchored,
            })

        if gaps:
            print(f"  {name}:")
            for g in gaps:
                print(f"    GAP of {g['size']} columns: X={g['left_x']}..{g['right_x']} "
                      f"({g['right_count']} focuses to the right)")
                if g['anchored_hidden']:
                    print(f"      Caused by hidden anchor(s): {', '.join(g['anchored_hidden'])}")
                    print(f"      -> Need NOI compaction offset (x≈-{g['size'] - 2}) on the right-side root")
        else:
            print(f"  {name}: no significant gaps.")
        break  # only check one initial scenario


def _find_active_ancestor(focus_id, focuses, active):
    """Walk up relative_position_id chain to find first focus with an active offset."""
    cur = focus_id
    visited = set()
    while cur and cur not in visited:
        visited.add(cur)
        if cur in active:
            return cur
        cur = focuses[cur].relative_position_id if cur in focuses else None
    return None


def _report_ahist_on(focuses, scenarios, ahist_on_collisions, desc_map):
    """Report ahist=ON collisions with fix suggestions."""
    if not ahist_on_collisions:
        return

    print(f"\n=== AHIST=ON REPORT ({len(ahist_on_collisions)} scenarios with collisions) ===\n")

    for name in sorted(ahist_on_collisions):
        cols = ahist_on_collisions[name]

        for sname, visible, positions in scenarios:
            if sname != name:
                continue

            completed, dlc, _ = parse_scenario_params(name)
            game_rules = {"noi_allow_ahistorical": "noi_rule_ahistorical_allowed"}
            active = get_active_offsets(focuses, completed, dlc, bool(completed), game_rules)

            pos_copy = dict(positions)
            results = solve_offsets_for_scenario(
                focuses, visible, pos_copy, active, desc_map, _verbose=False)

            fixable = len(results)
            paradox_count = 0
            for a, b, _pa, _pb, _d in cols:
                ga = _find_active_ancestor(a, focuses, active)
                gb = _find_active_ancestor(b, focuses, active)
                if not ga and not gb:
                    paradox_count += 1

            print(f"  {name}:")
            print(f"    {len(cols)} collisions, {fixable} fixable via existing offsets, "
                  f"{paradox_count} paradoxes (base position conflicts)")

            if results:
                for fid, oidx, old_x, new_x in results:
                    print(f"    -> {fid} offset#{oidx}: x={old_x} -> x={new_x}")

            if paradox_count:
                path = completed.pop() if completed else "initial"
                print("    -> Needs NEW offset block(s) with trigger:")
                print("       has_game_rule = { rule = noi_allow_ahistorical option = noi_rule_ahistorical_allowed }")
                if completed:
                    print(f"       has_completed_focus = {path}")
                if dlc:
                    print(f"       has_dlc = \"{list(dlc)[0]}\"")
            print()
            break


def _report_continuous_position(text, scenarios):
    """Report suggested continuous_focus_position values."""
    print("\n=== CONTINUOUS FOCUS POSITION ===\n")
    max_visible_y = 0
    min_visible_x = 999
    for name, visible, positions in scenarios:
        if "ahist=OFF" not in name or "initial" not in name:
            continue
        for fid in visible:
            if fid in positions:
                max_visible_y = max(max_visible_y, positions[fid][1])
                min_visible_x = min(min_visible_x, positions[fid][0])

    suggested_y = (max_visible_y + 3) * PX_PER_Y
    suggested_x = max(0, min_visible_x) * PX_PER_X

    current_match = re.search(
        r'continuous_focus_position\s*=\s*\{\s*x\s*=\s*(-?\d+)\s*y\s*=\s*(-?\d+)\s*\}', text)
    if current_match:
        cur_x, cur_y = int(current_match.group(1)), int(current_match.group(2))
        print(f"  Current: x = {cur_x}, y = {cur_y}")
    print(f"  Tree visible range: X={min_visible_x}..max, Y=0..{max_visible_y}")
    print(f"  Suggested: x = {suggested_x}, y = {suggested_y}")
    print("  (place below tree, aligned left, prefer right over far down)")


def _report_centering(focuses, scenarios):
    """Report centering issues across initial scenarios."""
    print("\n=== CENTERING ISSUES ===\n")
    centering_seen = set()
    for name, visible, positions in scenarios:
        if "initial" not in name:
            continue
        issues = detect_centering_issues(focuses, positions, visible)
        for parent, _children, desc in issues:
            if parent not in centering_seen:
                centering_seen.add(parent)
                print(f"  {parent}: {desc}")
    if not centering_seen:
        print("  No centering issues.")


# ---------------------------------------------------------------------------
# Autofixer: run (diagnosis + fix + gap + ahist=ON report + centering)
# ---------------------------------------------------------------------------

def run_diagnose_and_fix(file_path, fix=False, dry_run=False, verbose=False):
    """Full diagnosis + optional auto-fix pipeline for a focus tree file."""
    text = Path(file_path).read_text(encoding='utf-8-sig')

    print(f"Parsing {file_path}...")
    raw = extract_focuses_raw(text)
    focuses = {}
    for entries, ln in raw:
        f = parse_focus(entries, ln)
        if f.id:
            focuses[f.id] = f
    print(f"  {len(focuses)} focuses.")

    branches = identify_branches(focuses)
    desc_map = build_descendant_map(focuses)
    scenarios = generate_scenarios(focuses, branches)
    print(f"  {len(scenarios)} scenarios.\n")

    # Phase 1: Categorize collisions
    ahist_off_collisions, ahist_on_collisions, ahist_off_clean, ahist_on_clean = \
        _categorize_collisions(scenarios)

    total_off = len(ahist_off_collisions) + ahist_off_clean
    total_on = len(ahist_on_collisions) + ahist_on_clean

    print("=== DIAGNOSIS ===\n")
    print(f"  ahist=OFF: {ahist_off_clean}/{total_off} scenarios clean, "
          f"{len(ahist_off_collisions)} with collisions")
    print(f"  ahist=ON:  {ahist_on_clean}/{total_on} scenarios clean, "
          f"{len(ahist_on_collisions)} with collisions")

    # Anchor analysis
    ahist_hidden_ids = _compute_ahist_hidden_ids(focuses, branches)
    _analyze_anchors(focuses, branches, desc_map, ahist_hidden_ids)

    if ahist_off_collisions and verbose:
        print("\n  ahist=OFF collisions:")
        for name, cols in ahist_off_collisions.items():
            print(f"    {name}: {len(cols)} collision(s)")
            for a, b, pa, _pb, d in cols[:5]:
                print(f"      {a} vs {b} at Y={pa[1]}, dist={d}")

    # Phase 2: Auto-fix ahist=OFF
    all_fixes = {}
    if fix and ahist_off_collisions:
        all_fixes, text = _autofix_ahist_off(
            text, file_path, focuses, scenarios, desc_map,
            ahist_off_collisions, dry_run, verbose)
    elif not ahist_off_collisions:
        print("\n  ahist=OFF already clean, no fixes needed.")

    # Re-parse if fixes were applied
    if fix and all_fixes and not dry_run:
        text = Path(file_path).read_text(encoding='utf-8-sig')
        raw = extract_focuses_raw(text)
        focuses = {}
        for entries, ln in raw:
            f = parse_focus(entries, ln)
            if f.id:
                focuses[f.id] = f
        branches = identify_branches(focuses)
        desc_map = build_descendant_map(focuses)
        scenarios = generate_scenarios(focuses, branches)
        ahist_hidden_ids = _compute_ahist_hidden_ids(focuses, branches)

    # Gap detection, ahist=ON report, continuous position, centering
    _detect_gaps(focuses, scenarios, ahist_hidden_ids)
    _report_ahist_on(focuses, scenarios, ahist_on_collisions, desc_map)
    _report_continuous_position(text, scenarios)
    _report_centering(focuses, scenarios)



# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_diagnose(args):
    """CLI handler: run diagnosis only."""
    run_diagnose_and_fix(args.file, fix=False, dry_run=False, verbose=args.verbose)


def cmd_fix(args):
    """CLI handler: run diagnosis + auto-fix."""
    run_diagnose_and_fix(args.file, fix=True, dry_run=args.dry_run, verbose=args.verbose)


def _print_focus_detail(focuses, focus_id):
    """Print detailed info for a single focus."""
    f = focuses.get(focus_id)
    if not f:
        print(f"Focus '{focus_id}' not found.")
        return

    print(f"\n=== Focus: {f.id} (line {f.line_number}) ===")
    print(f"  Position: x={f.x}, y={f.y}")
    print(f"  Relative to: {f.relative_position_id or '(absolute)'}")
    print(f"  Absolute position: ({f.abs_x}, {f.abs_y})")
    print(f"  Prerequisites: {f.prerequisite_ids or 'none'}")
    print(f"  Mutually exclusive: {f.mutually_exclusive_ids or 'none'}")
    if f.has_allow_branch:
        print(f"  allow_branch: {f.allow_branch_raw}")
    if f.offsets:
        for i, off in enumerate(f.offsets):
            print(f"  Offset #{i}: x={off.x}, y={off.y}")
            if off.trigger_raw:
                print(f"    trigger: {off.trigger_raw}")

    print("\n  Position chain:")
    chain = []
    current = f
    while current:
        chain.append(current)
        if current.relative_position_id and current.relative_position_id in focuses:
            current = focuses[current.relative_position_id]
        else:
            break
    for c in reversed(chain):
        rel = c.relative_position_id or "(root)"
        print(f"    {c.id} -> rel_to={rel}, offset=({c.x}, {c.y}), abs=({c.abs_x}, {c.abs_y})")


def _print_branch_detail(focuses, branches, branch_id):
    """Print detailed info for a branch."""
    if branch_id not in branches:
        print(f"Branch '{branch_id}' not found.")
        print(f"Available branches: {', '.join(branches.keys())}")
        return

    members = branches[branch_id]
    print(f"\n=== Branch: {branch_id} ({len(members)} focuses) ===")
    root = focuses[branch_id]
    print(f"  allow_branch: {root.allow_branch_raw}")
    for mid in members:
        f = focuses[mid]
        print(f"  {f.id:<55} abs=({f.abs_x}, {f.abs_y})")


def _build_json_output(focuses):
    """Build JSON-serializable dict for all focuses."""
    output = {}
    for fid, f in focuses.items():
        output[fid] = {
            "x": f.x, "y": f.y,
            "abs_x": f.abs_x, "abs_y": f.abs_y,
            "relative_position_id": f.relative_position_id,
            "prerequisites": f.prerequisite_ids,
            "mutually_exclusive": f.mutually_exclusive_ids,
            "has_allow_branch": f.has_allow_branch,
            "allow_branch": f.allow_branch_raw if f.has_allow_branch else None,
            "offsets": [{"x": o.x, "y": o.y, "trigger": o.trigger_raw} for o in f.offsets],
            "line": f.line_number,
        }
    return output


def cmd_parse(args):
    """CLI handler: parse and display focus tree."""
    text = Path(args.file).read_text(encoding='utf-8-sig')

    log = sys.stderr if args.json else sys.stdout
    print(f"Parsing {args.file}...", file=log)
    raw_focuses = extract_focuses_raw(text)
    print(f"Found {len(raw_focuses)} focus blocks.", file=log)

    focuses = {}
    for entries, line_num in raw_focuses:
        f = parse_focus(entries, line_num)
        if f.id:
            focuses[f.id] = f

    print(f"Parsed {len(focuses)} named focuses.", file=log)
    compute_absolute_positions(focuses)
    branches = identify_branches(focuses)

    if args.focus:
        _print_focus_detail(focuses, args.focus)
    elif args.branch:
        _print_branch_detail(focuses, branches, args.branch)
    elif args.json:
        print(json.dumps(_build_json_output(focuses), indent=2))
    else:
        print_tree(focuses, branches)


def cmd_overlaps(args):
    text = Path(args.file).read_text(encoding='utf-8-sig')

    print(f"Parsing {args.file}...")
    raw_focuses = extract_focuses_raw(text)

    focuses = {}
    for entries, line_num in raw_focuses:
        f = parse_focus(entries, line_num)
        if f.id:
            focuses[f.id] = f

    print(f"Parsed {len(focuses)} named focuses.")

    compute_absolute_positions(focuses)
    branches = identify_branches(focuses)
    print_overlap_report(focuses, branches)
    suggest_fixes(focuses, branches)


def cmd_simulate(args):
    text = Path(args.file).read_text(encoding='utf-8-sig')

    print(f"Parsing {args.file}...")
    raw_focuses = extract_focuses_raw(text)

    focuses = {}
    for entries, line_num in raw_focuses:
        f = parse_focus(entries, line_num)
        if f.id:
            focuses[f.id] = f

    print(f"Parsed {len(focuses)} named focuses.")

    sim_completed = set(args.completed) if args.completed else set()
    sim_dlc = set(args.dlc) if args.dlc else set()

    print(f"Simulating with completed focuses: {sim_completed}")
    print(f"  DLCs: {sim_dlc or 'none'}, obsolete_hide: True")

    compute_absolute_positions(focuses, sim_completed, sim_dlc, obsolete_hide=True)
    branches = identify_branches(focuses)
    print_tree(focuses, branches)


def cmd_bboxes(args):
    text = Path(args.file).read_text(encoding='utf-8-sig')

    print(f"Parsing {args.file}...")
    raw_focuses = extract_focuses_raw(text)

    focuses = {}
    for entries, line_num in raw_focuses:
        f = parse_focus(entries, line_num)
        if f.id:
            focuses[f.id] = f

    print(f"Parsed {len(focuses)} named focuses.")

    compute_absolute_positions(focuses)
    branches = identify_branches(focuses)
    print_bounding_boxes(focuses, branches)


# ---------------------------------------------------------------------------
# Branch spacing analysis (position-root groups)
# ---------------------------------------------------------------------------

def find_position_root(fid: str, focuses: dict, memo: dict) -> str:
    """Walk up relative_position_id chain to the topmost ancestor."""
    if fid in memo:
        return memo[fid]
    f = focuses.get(fid)
    if f is None or f.relative_position_id is None or f.relative_position_id not in focuses:
        memo[fid] = fid
        return fid
    root = find_position_root(f.relative_position_id, focuses, memo)
    memo[fid] = root
    return root


def compute_branch_spacing(focuses: dict):
    """
    Group focuses by position root, compute per-Y-level bounding boxes,
    and report inter-group spacing constraints.
    """
    memo = {}
    groups = {}  # root_id -> list of (x, y, fid)
    for fid, f in focuses.items():
        if f.abs_x is None or f.abs_y is None:
            continue
        root = find_position_root(fid, focuses, memo)
        groups.setdefault(root, []).append((f.abs_x, f.abs_y, fid))

    if len(groups) < 2:
        print("  Only one position group — no inter-group spacing to analyze.")
        return

    # Per-group per-Y ranges
    group_by_y = {}  # root_id -> {y -> (min_x, max_x)}
    for root_id, members in groups.items():
        by_y = {}
        for x, y, _ in members:
            if y not in by_y:
                by_y[y] = (x, x)
            else:
                by_y[y] = (min(by_y[y][0], x), max(by_y[y][1], x))
        group_by_y[root_id] = by_y

    # Sort groups by their global min X
    sorted_groups = sorted(group_by_y.items(),
                           key=lambda kv: min(r[0] for r in kv[1].values()))

    # Print per-group summary
    print("\n=== POSITION GROUPS ===\n")
    for root_id, by_y in sorted_groups:
        all_xs = [x for members in groups[root_id] for x in (members[0],)]
        count = len(groups[root_id])
        gmin, gmax = min(all_xs), max(all_xs)
        print(f"  {root_id}: {count} focuses, X=[{gmin}, {gmax}]")

    # Pairwise spacing between adjacent groups
    print("\n=== INTER-GROUP SPACING (per Y-level) ===\n")
    for i in range(len(sorted_groups) - 1):
        left_id, left_by_y = sorted_groups[i]
        right_id, right_by_y = sorted_groups[i + 1]

        all_ys = sorted(set(list(left_by_y.keys()) + list(right_by_y.keys())))
        binding_gap = None
        binding_y = None

        print(f"  {left_id}  <-->  {right_id}")
        for y in all_ys:
            l = left_by_y.get(y)
            r = right_by_y.get(y)
            if l and r:
                gap = r[0] - l[1]
                marker = ""
                if gap < COLLISION_DISTANCE:
                    marker = " ** OVERLAP **"
                elif gap == COLLISION_DISTANCE:
                    marker = " (tight)"
                if binding_gap is None or gap < binding_gap:
                    binding_gap = gap
                    binding_y = y
                print(f"    Y={y:2d}: left=[{l[0]:3d},{l[1]:3d}]  right=[{r[0]:3d},{r[1]:3d}]  gap={gap}{marker}")
            elif l:
                print(f"    Y={y:2d}: left=[{l[0]:3d},{l[1]:3d}]  right=---")
            else:
                print(f"    Y={y:2d}: left=---          right=[{r[0]:3d},{r[1]:3d}]")

        if binding_gap is not None:
            if binding_gap >= COLLISION_DISTANCE:
                status = "CLEAR"
            elif binding_gap >= 0:
                status = "TIGHT"
            else:
                status = "INTERLEAVED"
            print(f"    => Binding constraint: gap={binding_gap} at Y={binding_y} [{status}]")
            if binding_gap < COLLISION_DISTANCE:
                print(f"       (bbox overlap — run 'diagnose' to check for actual focus collisions)")
        print()


def cmd_spacing(args):
    text = Path(args.file).read_text(encoding='utf-8-sig')

    print(f"Parsing {args.file}...")
    raw_focuses = extract_focuses_raw(text)

    focuses = {}
    for entries, line_num in raw_focuses:
        f = parse_focus(entries, line_num)
        if f.id:
            focuses[f.id] = f

    print(f"  {len(focuses)} focuses.")

    completed = set(args.completed) if args.completed else None
    dlc = set(args.dlc) if args.dlc else None

    compute_absolute_positions(focuses, completed, dlc, obsolete_hide=True)
    compute_branch_spacing(focuses)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Entry point: parse CLI arguments and dispatch subcommand."""
    parser = argparse.ArgumentParser(
        prog="focus_tree.py",
        description="HOI4 Focus Tree Toolkit — parse, diagnose, and fix focus tree layouts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- diagnose ---
    p_diag = subparsers.add_parser("diagnose", help="Detect all collisions across all scenarios")
    p_diag.add_argument("file", help="Path to focus tree .txt file")
    p_diag.add_argument("--verbose", "-v", action="store_true", help="Detailed output")
    p_diag.set_defaults(func=cmd_diagnose)

    # --- fix ---
    p_fix = subparsers.add_parser("fix", help="Auto-fix ahist=OFF collisions")
    p_fix.add_argument("file", help="Path to focus tree .txt file")
    p_fix.add_argument("--dry-run", "-n", action="store_true", help="Don't write changes")
    p_fix.add_argument("--verbose", "-v", action="store_true", help="Detailed output")
    p_fix.set_defaults(func=cmd_fix)

    # --- parse ---
    p_parse = subparsers.add_parser("parse", help="Print the full tree with positions")
    p_parse.add_argument("file", help="Path to focus tree .txt file")
    p_parse.add_argument("--focus", help="Show details for a specific focus ID")
    p_parse.add_argument("--branch", help="Show all focuses in a branch (by root ID)")
    p_parse.add_argument("--json", action="store_true", help="Output as JSON")
    p_parse.set_defaults(func=cmd_parse)

    # --- overlaps ---
    p_over = subparsers.add_parser("overlaps", help="Full overlap analysis with fix suggestions")
    p_over.add_argument("file", help="Path to focus tree .txt file")
    p_over.set_defaults(func=cmd_overlaps)

    # --- simulate ---
    p_sim = subparsers.add_parser("simulate", help="Simulate a scenario with offsets")
    p_sim.add_argument("file", help="Path to focus tree .txt file")
    p_sim.add_argument("--completed", nargs="+", metavar="FOCUS_ID",
                       help="Focuses to mark as completed")
    p_sim.add_argument("--dlc", nargs="*", metavar="DLC_NAME", default=[],
                       help="DLCs owned for simulation")
    p_sim.set_defaults(func=cmd_simulate)

    # --- bboxes ---
    p_bbox = subparsers.add_parser("bboxes", help="Bounding box visualization")
    p_bbox.add_argument("file", help="Path to focus tree .txt file")
    p_bbox.set_defaults(func=cmd_bboxes)

    # --- spacing ---
    p_space = subparsers.add_parser("spacing", help="Inter-branch spacing analysis by position root")
    p_space.add_argument("file", help="Path to focus tree .txt file")
    p_space.add_argument("--completed", nargs="+", metavar="FOCUS_ID",
                         help="Focuses to mark as completed")
    p_space.add_argument("--dlc", nargs="*", metavar="DLC_NAME", default=[],
                         help="DLCs owned for simulation")
    p_space.set_defaults(func=cmd_spacing)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
