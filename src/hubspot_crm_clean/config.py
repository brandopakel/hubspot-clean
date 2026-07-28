"""
Load and validate the YAML config (see config.example.yaml).

A config file only ever *supplies defaults*: a flag passed on the command line
always wins, and anything the file omits falls back to the module defaults the
audits already ship with.

Validation is deliberately strict. An unknown key is an error rather than a
silent no-op — a config file that quietly ignores `min_completness: 90` is worse
than no config at all, because it reads as though it took effect. The same goes
for wrong types and out-of-range numbers: they're reported with the dotted path
to the offending key so the fix is obvious.
"""

from pathlib import Path
from typing import NamedTuple

import yaml

from hubspot_crm_clean.audits.duplicates import DEFAULT_THRESHOLD
from hubspot_crm_clean.audits.incomplete import (
    DEFAULT_MIN_COMPLETENESS,
    DEFAULT_REQUIRED_FIELDS,
)
from hubspot_crm_clean.audits.stale import DEFAULT_ACTIVITY_FIELDS, DEFAULT_INACTIVE_DAYS
from hubspot_crm_clean.reports import ReportFormat

# Picked up automatically from the working directory when --config isn't passed.
DEFAULT_CONFIG_NAME = "config.yaml"


class ConfigError(ValueError):
    """A config file that can't be trusted: bad YAML, wrong shape, or a key we
    don't recognise. Carries a message meant to be shown to the user as-is."""


class Config(NamedTuple):
    """Audit settings, with every field resolved to a usable value."""
    threshold: int
    required_fields: list
    min_completeness: int
    inactive_days: int
    activity_fields: list
    # ReportFormat, or None to leave --format required. There is no sensible
    # built-in default here: unlike a threshold, guessing csv over JSON would be
    # picking a data shape on the user's behalf.
    default_format: object = None


# list(...) so a caller mutating a Config's list can't reach back into the
# audit modules' module-level defaults.
DEFAULTS = Config(
    threshold=DEFAULT_THRESHOLD,
    required_fields=list(DEFAULT_REQUIRED_FIELDS),
    min_completeness=DEFAULT_MIN_COMPLETENESS,
    inactive_days=DEFAULT_INACTIVE_DAYS,
    activity_fields=list(DEFAULT_ACTIVITY_FIELDS),
    default_format=None,
)


def _whole_number(value, path):
    """An int, and genuinely an int - bool is a subclass, and `inactive_days: true`
    is a typo, not a 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be a whole number, got {value!r}")
    return value


def _percentage(value, path):
    number = _whole_number(value, path)
    if not 0 <= number <= 100:
        raise ConfigError(f"{path} must be between 0 and 100, got {number}")
    return number


def _non_negative(value, path):
    number = _whole_number(value, path)
    if number < 0:
        raise ConfigError(f"{path} must be 0 or more, got {number}")
    return number


def _field_list(value, path):
    """A list of distinct, non-blank property names.

    Duplicates are rejected rather than deduped: completeness_score divides by
    the length of this list, so a repeated field silently skews every score.
    An empty list is allowed and means "check nothing" - the audits already
    treat it that way.
    """
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list of field names, got {value!r}")
    names = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"{path}[{index}] must be a non-empty field name, got {item!r}"
            )
        name = item.strip()
        if name in names:
            raise ConfigError(f"{path}[{index}] repeats the field {name!r}")
        names.append(name)
    return names


def _report_format(value, path):
    """One of the formats reports.py knows how to write."""
    choices = sorted(item.value for item in ReportFormat)
    text = value.strip().lower() if isinstance(value, str) else None
    if text not in choices:
        raise ConfigError(f"{path} must be one of {', '.join(choices)}, got {value!r}")
    return ReportFormat(text)


# rules section -> {config key: (Config field, validator)}. The single source of
# truth for what a config file may contain; the loader and the "valid keys" hint
# in error messages both read it, so they can't drift apart.
SCHEMA = {
    "duplicates": {
        "match_threshold": ("threshold", _percentage),
    },
    "incomplete": {
        "required_fields": ("required_fields", _field_list),
        "min_completeness": ("min_completeness", _percentage),
    },
    "stale": {
        "inactive_days": ("inactive_days", _non_negative),
        "activity_fields": ("activity_fields", _field_list),
    },
}

# Export settings, which are about output rather than about what counts as a
# finding - so they sit beside `rules` at the root rather than inside it.
REPORTS_SCHEMA = {
    "default_format": ("default_format", _report_format),
}

ROOT_SECTIONS = ["reports", "rules"]


def _require_mapping(value, path):
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping, got {type(value).__name__}")


def _reject_unknown(block, allowed, path):
    """Fail on keys we don't recognise, listing the ones we do.

    Keys are stringified because YAML happily parses `90: x` into an int key,
    which would otherwise blow up the sort and the join.
    """
    unknown = sorted(str(key) for key in set(block) - set(allowed))
    if unknown:
        raise ConfigError(
            f"unknown key(s) under {path}: {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(allowed))}"
        )


def _fresh_defaults():
    """The default values, with the lists copied.

    Every Config handed out owns its lists, so a caller that mutates one can't
    reach back into DEFAULTS and change what the next run starts from.
    """
    values = DEFAULTS._asdict()
    values["required_fields"] = list(values["required_fields"])
    values["activity_fields"] = list(values["activity_fields"])
    return values


def _apply_section(block, fields, path, values):
    """Validate one block of settings into `values`.

    A block that's absent - or present but empty - changes nothing. Shared by the
    `rules.*` sections and the top-level `reports` one, so every section is
    checked and reported the same way.
    """
    if block is None:
        return
    _require_mapping(block, path)
    _reject_unknown(block, fields, path)
    for key, (field, validate) in fields.items():
        value = block.get(key)
        # `is None`, not falsy: `min_completeness: 0` and `required_fields: []`
        # are meaningful settings, and `or` would swap them for the defaults.
        if value is None:
            continue
        values[field] = validate(value, f"{path}.{key}")


def parse_config(raw):
    """Turn already-parsed YAML into a Config. Raises ConfigError on anything unusable."""
    if raw is None:
        # an empty file is a valid config - it just changes nothing
        return Config(**_fresh_defaults())
    _require_mapping(raw, "the config root")
    _reject_unknown(raw, ROOT_SECTIONS, "the config root")

    values = _fresh_defaults()
    _apply_section(raw.get("reports"), REPORTS_SCHEMA, "reports", values)

    rules = raw.get("rules")
    if rules is not None:
        _require_mapping(rules, "rules")
        _reject_unknown(rules, SCHEMA, "rules")
        for section, fields in SCHEMA.items():
            _apply_section(rules.get(section), fields, f"rules.{section}", values)
    return Config(**values)


def load_config(path):
    """Read a YAML config file into a Config.

    Every failure - unreadable file, malformed YAML, bad shape - surfaces as a
    ConfigError naming the file, so the CLI can print one clean line instead of
    a traceback.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"could not read {path}: {err}") from err
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"{path} is not valid YAML: {err}") from err
    try:
        return parse_config(raw)
    except ConfigError as err:
        raise ConfigError(f"{path}: {err}") from err


def find_config(explicit=None):
    """Which config file to load, or None to run on built-in defaults.

    An explicit --config path is used as given. Otherwise ./config.yaml is picked
    up when it exists, which is what makes a config file worth having: you set
    your thresholds once instead of retyping flags.
    """
    if explicit is not None:
        return Path(explicit)
    found = Path(DEFAULT_CONFIG_NAME)
    return found if found.is_file() else None
