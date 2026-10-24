"""
Nix's log numbers, as IntEnums a caller can name.

`LogRecord.level` and `LogRecord.type` stay integers, and these name
the integers. An IntEnum compares equal to its number, so
`record.level <= Verbosity.INFO` needs nothing from the record.

Each number is checked against upstream's enum where a unit reads
one: `LogRecord` names all three with `@spells`, so `eval.cpp` holds
a `static_assert` per member and a switch with no `default`
(nixidae/nixidae#1052).
"""

from enum import IntEnum

from huggorm_dsl.declare import NIX_2_36, Enumerated, Local, header, words


@header(Local("nix/util/error.hh"))
@words(enumerated=Enumerated(
    "nix::Verbosity",
    spelled={"ERROR": "lvlError", "WARN": "lvlWarn", "NOTICE": "lvlNotice",
             "INFO": "lvlInfo", "TALKATIVE": "lvlTalkative",
             "CHATTY": "lvlChatty", "DEBUG": "lvlDebug", "VOMIT": "lvlVomit"},
))
class Verbosity(IntEnum):
    """How much Nix says. A message is shown when its level is at or
    below the verbosity."""

    ERROR = 0
    WARN = 1
    NOTICE = 2
    INFO = 3
    TALKATIVE = 4
    CHATTY = 5
    DEBUG = 6
    VOMIT = 7


@header(Local("nix/util/logging.hh"))
@words(enumerated=Enumerated(
    "nix::ActivityType",
    spelled={"UNKNOWN": "actUnknown", "COPY_PATH": "actCopyPath",
             "FILE_TRANSFER": "actFileTransfer", "REALISE": "actRealise",
             "COPY_PATHS": "actCopyPaths", "BUILDS": "actBuilds",
             "BUILD": "actBuild", "OPTIMISE_STORE": "actOptimiseStore",
             "VERIFY_PATHS": "actVerifyPaths", "SUBSTITUTE": "actSubstitute",
             "QUERY_PATH_INFO": "actQueryPathInfo",
             "POST_BUILD_HOOK": "actPostBuildHook",
             "BUILD_WAITING": "actBuildWaiting", "FETCH_TREE": "actFetchTree",
             "FETCH_TO_STORE": "actFetchToStore"},
))
class ActivityType(IntEnum):
    """What an activity is, as a `start` record carries it.

    Upstream's `actLast` is a second name for the last value, so it
    is not a member here."""

    UNKNOWN = 0
    COPY_PATH = 100
    FILE_TRANSFER = 101
    REALISE = 102
    COPY_PATHS = 103
    BUILDS = 104
    BUILD = 105
    OPTIMISE_STORE = 106
    VERIFY_PATHS = 107
    SUBSTITUTE = 108
    QUERY_PATH_INFO = 109
    POST_BUILD_HOOK = 110
    BUILD_WAITING = 111
    FETCH_TREE = 112
    if NIX_2_36:
        FETCH_TO_STORE = 113
        """A source path copied into the store, or hashed in a dry
        run."""


@header(Local("nix/util/logging.hh"))
@words(enumerated=Enumerated(
    "nix::ResultType",
    spelled={"FILE_LINKED": "resFileLinked",
             "BUILD_LOG_LINE": "resBuildLogLine",
             "UNTRUSTED_PATH": "resUntrustedPath",
             "CORRUPTED_PATH": "resCorruptedPath",
             "SET_PHASE": "resSetPhase", "PROGRESS": "resProgress",
             "SET_EXPECTED": "resSetExpected",
             "POST_BUILD_LOG_LINE": "resPostBuildLogLine",
             "FETCH_STATUS": "resFetchStatus",
             "FETCH_TO_STORE": "resFetchToStore"},
))
class ResultType(IntEnum):
    """What a `result` record reports inside an activity.

    Upstream's `resLast` is a second name for the last value, so it is
    not a member here."""

    FILE_LINKED = 100
    BUILD_LOG_LINE = 101
    UNTRUSTED_PATH = 102
    CORRUPTED_PATH = 103
    SET_PHASE = 104
    PROGRESS = 105
    SET_EXPECTED = 106
    POST_BUILD_LOG_LINE = 107
    FETCH_STATUS = 108
    if NIX_2_36:
        FETCH_TO_STORE = 109
        """The store path an `ActivityType.FETCH_TO_STORE` activity
        produced."""
