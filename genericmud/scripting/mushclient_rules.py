"""MUSHclient's trigger and alias records, and the rules it fills their text in by.

Everything here follows MUSHclient's own source, so a pack behaves the way its author saw it
behave: scripting/errors.h (return codes), Utilities.cpp (FixupEscapeSequences), evaluate.cpp
(FixSendText and FixWildcard), methods_triggers.cpp, methods_aliases.cpp and
methods_timers.cpp (option names and the GetTriggerInfo numbers), mxp/mxpinit.cpp (colour
names) and MUSHclient's default ANSI palette.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

# --- return codes: scripting/errors.h numbers these from 30001, in this order ---

_ERRORS = (
    ("eWorldOpen", "The world is already open"),
    ("eWorldClosed", "The world is closed, this action cannot be performed"),
    ("eNoNameSpecified", "No name has been specified where one is required"),
    ("eCannotPlaySound", "The sound file could not be played"),
    ("eTriggerNotFound", "The specified trigger name does not exist"),
    ("eTriggerAlreadyExists", "Attempt to add a trigger that already exists"),
    ("eTriggerCannotBeEmpty", "The trigger \"match\" string cannot be empty"),
    ("eInvalidObjectLabel", "The name of this object is invalid"),
    ("eScriptNameNotLocated", "Script name is not in the script file"),
    ("eAliasNotFound", "The specified alias name does not exist"),
    ("eAliasAlreadyExists", "Attempt to add a alias that already exists"),
    ("eAliasCannotBeEmpty", "The alias \"match\" string cannot be empty"),
    ("eCouldNotOpenFile", "Unable to open requested file"),
    ("eLogFileNotOpen", "Log file was not open"),
    ("eLogFileAlreadyOpen", "Log file was already open"),
    ("eLogFileBadWrite", "Bad write to log file"),
    ("eTimerNotFound", "The specified timer name does not exist"),
    ("eTimerAlreadyExists", "Attempt to add a timer that already exists"),
    ("eVariableNotFound", "Attempt to delete a variable that does not exist"),
    ("eCommandNotEmpty", "Attempt to use SetCommand with a non-empty command window"),
    ("eBadRegularExpression", "Bad regular expression syntax"),
    ("eTimeInvalid", "Time given to AddTimer is invalid"),
    ("eBadMapItem", "Map item contains invalid characters, or is empty"),
    ("eNoMapItems", "No items in mapper"),
    ("eUnknownOption", "Option name not found"),
    ("eOptionOutOfRange", "New value for option is out of range"),
    ("eTriggerSequenceOutOfRange", "Trigger sequence value invalid"),
    ("eTriggerSendToInvalid", "Where to send trigger text to is invalid"),
    ("eTriggerLabelNotSpecified", "Trigger label not specified/invalid for 'send to variable'"),
    ("ePluginFileNotFound", "File name specified for plugin not found"),
    ("eProblemsLoadingPlugin", "There was a parsing or other problem loading the plugin"),
    ("ePluginCannotSetOption", "Plugin is not allowed to set this option"),
    ("ePluginCannotGetOption", "Plugin is not allowed to get this option"),
    ("eNoSuchPlugin", "Requested plugin is not installed"),
    ("eNotAPlugin", "Only a plugin can do this"),
    ("eNoSuchRoutine", "Plugin does not support that subroutine (subroutine not in script)"),
    ("ePluginDoesNotSaveState", "Plugin does not support saving state"),
    ("ePluginCouldNotSaveState", "Plugin could not save state (eg. no state directory)"),
    ("ePluginDisabled", "Plugin is currently disabled"),
    ("eErrorCallingPluginRoutine", "Could not call plugin routine"),
    ("eCommandsNestedTooDeeply", "Calls to \"Execute\" nested too deeply"),
    ("eCannotCreateChatSocket", "Unable to create socket for chat connection"),
    ("eCannotLookupDomainName", "Unable to do DNS (domain name) lookup for chat connection"),
    ("eNoChatConnections", "No chat connections open"),
    ("eChatPersonNotFound", "Requested chat person not connected"),
    ("eBadParameter", "General problem with a parameter to a script call"),
    ("eChatAlreadyListening", "Already listening for incoming chats"),
    ("eChatIDNotFound", "Chat session with that ID not found"),
    ("eChatAlreadyConnected", "Already connected to that server/port"),
    ("eClipboardEmpty", "Cannot get (text from the) clipboard"),
    ("eFileNotFound", "Cannot open the specified file"),
    ("eAlreadyTransferringFile", "Already transferring a file"),
    ("eNotTransferringFile", "Not transferring a file"),
    ("eNoSuchCommand", "There is not a command of that name"),
    ("eArrayAlreadyExists", "That array already exists"),
    ("eArrayDoesNotExist", "That array does not exist"),
    ("eArrayNotEvenNumberOfValues", "Values to be imported into array are not in pairs"),
    ("eImportedWithDuplicates", "Import succeeded, however some values were overwritten"),
    ("eBadDelimiter", "Import/export delimiter must be a single character, other than backslash"),
    ("eSetReplacingExistingValue", "Array element set, existing value overwritten"),
    ("eKeyDoesNotExist", "Array key does not exist"),
    ("eCannotImport", "Cannot import because cannot find unused temporary character"),
    ("eItemInUse", "Cannot delete trigger/alias/timer because it is executing a script"),
    ("eSpellCheckNotActive", "Spell checker is not active"),
    ("eCannotAddFont", "Cannot create requested font"),
    ("ePenStyleNotValid", "Invalid settings for pen parameter"),
    ("eUnableToLoadImage", "Bitmap image could not be loaded"),
    ("eImageNotInstalled", "Image has not been loaded into window"),
    ("eInvalidNumberOfPoints", "Number of points supplied is incorrect"),
    ("eInvalidPoint", "Point is not numeric"),
    ("eHotspotPluginChanged", "Hotspot processing must all be in same plugin"),
    ("eHotspotNotInstalled", "Hotspot has not been defined for this window"),
    ("eNoSuchWindow", "Requested miniwindow does not exist"),
    ("eBrushStyleNotValid", "Invalid settings for brush parameter"),
)
ERROR_CODES: dict[str, int] = {"eOK": 0}
ERROR_DESCRIPTIONS: dict[int, str] = {0: "No error"}
for _index, (_name, _text) in enumerate(_ERRORS):
    ERROR_CODES[_name] = 30001 + _index
    ERROR_DESCRIPTIONS[30001 + _index] = _text

E_OK = 0
E_TRIGGER_NOT_FOUND = ERROR_CODES["eTriggerNotFound"]
E_TRIGGER_ALREADY_EXISTS = ERROR_CODES["eTriggerAlreadyExists"]
E_TRIGGER_CANNOT_BE_EMPTY = ERROR_CODES["eTriggerCannotBeEmpty"]
E_ALIAS_NOT_FOUND = ERROR_CODES["eAliasNotFound"]
E_ALIAS_ALREADY_EXISTS = ERROR_CODES["eAliasAlreadyExists"]
E_ALIAS_CANNOT_BE_EMPTY = ERROR_CODES["eAliasCannotBeEmpty"]
E_LOG_FILE_NOT_OPEN = ERROR_CODES["eLogFileNotOpen"]
E_TIMER_NOT_FOUND = ERROR_CODES["eTimerNotFound"]
E_TIMER_ALREADY_EXISTS = ERROR_CODES["eTimerAlreadyExists"]
E_VARIABLE_NOT_FOUND = ERROR_CODES["eVariableNotFound"]
E_BAD_REGULAR_EXPRESSION = ERROR_CODES["eBadRegularExpression"]
E_TIME_INVALID = ERROR_CODES["eTimeInvalid"]
E_UNKNOWN_OPTION = ERROR_CODES["eUnknownOption"]
E_OPTION_OUT_OF_RANGE = ERROR_CODES["eOptionOutOfRange"]
E_PLUGIN_CANNOT_SET_OPTION = ERROR_CODES["ePluginCannotSetOption"]
E_NO_SUCH_PLUGIN = ERROR_CODES["eNoSuchPlugin"]
E_NO_SUCH_ROUTINE = ERROR_CODES["eNoSuchRoutine"]
E_PLUGIN_DISABLED = ERROR_CODES["ePluginDisabled"]
E_ERROR_CALLING_PLUGIN_ROUTINE = ERROR_CODES["eErrorCallingPluginRoutine"]
E_CLIPBOARD_EMPTY = ERROR_CODES["eClipboardEmpty"]
E_NO_SUCH_COMMAND = ERROR_CODES["eNoSuchCommand"]
E_ARRAY_ALREADY_EXISTS = ERROR_CODES["eArrayAlreadyExists"]
E_ARRAY_DOES_NOT_EXIST = ERROR_CODES["eArrayDoesNotExist"]
E_ARRAY_NOT_EVEN_NUMBER_OF_VALUES = ERROR_CODES["eArrayNotEvenNumberOfValues"]
E_IMPORTED_WITH_DUPLICATES = ERROR_CODES["eImportedWithDuplicates"]
E_BAD_DELIMITER = ERROR_CODES["eBadDelimiter"]
E_SET_REPLACING_EXISTING_VALUE = ERROR_CODES["eSetReplacingExistingValue"]
E_KEY_DOES_NOT_EXIST = ERROR_CODES["eKeyDoesNotExist"]

# --- send text ---

_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_HEX_DIGITS = re.compile(r"[0-9A-Fa-f]{1,2}")
_VARIABLE_NAME = re.compile(r"[A-Za-z0-9_]*")
# What a variable's contents can't keep when they go into a regular expression.
_REGEX_SPECIAL = re.compile(r"[^A-Za-z0-9 ]")


def fixup_escape_sequences(text: str) -> str:
    """Turn the C escapes in a rule's send text into the characters they stand for.

    MUSHclient does this before anything else reads the text, and packs are written for it:
    Mush-Z sends Simulate("%1\\\\r\\\\n") so that Lua sees "\\r\\n". Any other escaped
    character stands for itself without its backslash, and a NUL ends the text, as in C.
    """
    if "\\" not in text:
        return text
    output: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        index += 1
        if character != "\\":
            output.append(character)
            continue
        if index >= len(text):
            break
        escape = text[index]
        index += 1
        if escape != "x":
            output.append(_C_ESCAPES.get(escape, escape))
            continue
        digits = _HEX_DIGITS.match(text, index)
        value = int(digits.group(0), 16) if digits is not None else 0
        if value == 0:
            break
        output.append(chr(value))
        index = digits.end()
    return "".join(output)


def fix_wildcard(value: str, *, to_script: bool, lower: bool = False) -> str:
    """A wildcard's text the way MUSHclient puts it into send text.

    Lower-cased when the rule asks for it, and escaped for a Lua string when the text is a
    script, so a quote or backslash in what the MUD sent can't break the script it lands in.
    """
    if lower:
        value = value.lower()
    if to_script:
        value = value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def _escape_for_match(value: str, *, regexp: bool) -> str:
    """A variable's contents made safe to match literally, the way MUSHclient does it."""
    value = "".join(character for character in value if character >= " ")
    if regexp:
        return _REGEX_SPECIAL.sub(lambda found: "\\" + found.group(0), value)
    return value.replace("*", "")


def fill_text(
    text: str,
    *,
    variables: Callable[[str], str | None] | None = None,
    wildcards: list[str] | None = None,
    named: dict[str, str] | None = None,
    label: str = "",
    for_match: bool = False,
    regexp: bool = False,
    to_script: bool = False,
    lower_wildcards: bool = False,
) -> str:
    """Fill in ``text`` the way MUSHclient's FixSendText does, in one pass.

    With ``variables``, @name becomes that variable's contents (@@ is a plain @, and a name
    that isn't set comes out empty). In a trigger's match (``for_match``) the contents are
    escaped so they match literally, unless written @!name. With ``wildcards``, %1 to %9 and
    %<name> become what the rule matched (escaped for Lua when ``to_script``), %% is a plain
    %, and %N is the rule's name. Anything else is kept as written.
    """
    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == "@" and variables is not None:
            index += 1
            if index < length and text[index] == "@":
                output.append("@")
                index += 1
                continue
            escape = for_match
            if index < length and text[index] == "!":
                escape = False
                index += 1
            name = _VARIABLE_NAME.match(text, index).group(0)
            index += len(name)
            contents = variables(name) if name else None
            if contents is None:
                continue
            if escape:
                contents = _escape_for_match(contents, regexp=regexp)
            if not for_match:
                contents = fix_wildcard(contents, to_script=to_script)
            output.append(contents)
            continue
        if character == "%" and wildcards is not None and index + 1 < length:
            following = text[index + 1]
            if following == "%":
                output.append("%")
                index += 2
                continue
            if following.isdigit():
                number = int(following)
                value = wildcards[number] if number < len(wildcards) else ""
                output.append(fix_wildcard(value, to_script=to_script, lower=lower_wildcards))
                index += 2
                continue
            if following == "<":
                end = text.find(">", index + 2)
                end = length if end < 0 else end
                name = text[index + 2:end]
                if name.isdigit():
                    number = int(name)
                    value = wildcards[number] if number < len(wildcards) else ""
                else:
                    value = (named or {}).get(name, "")
                if name:
                    output.append(fix_wildcard(value, to_script=to_script, lower=lower_wildcards))
                index = end + 1
                continue
            if following in "Nn":
                output.append(label)
                index += 2
                continue
            if following in "Cc":
                index += 2  # the clipboard, which the engine can't read
                continue
        output.append(character)
        index += 1
    return "".join(output)


def make_regular_expression(text: str) -> str:
    """MUSHclient's MakeRegularExpression: ``text`` as a whole-line regular expression in
    MUSHclient's own spelling. Letters, digits, spaces and accented characters stay as they
    are, ``*`` matches anything, and every other character is escaped."""
    parts = ["^"]
    for character in text:
        if character == "\n":
            parts.append("\\n")
        elif character < " ":
            parts.append(f"\\x{ord(character):02x}")
        elif character.isalnum() or character == " " or ord(character) >= 0x80:
            parts.append(character)
        elif character == "*":
            parts.append("(.*?)")
        else:
            parts.append("\\" + character)
    parts.append("$")
    return "".join(parts)


def evaluate_speedwalk(directions: list[str]) -> str:
    """MUSHclient's EvaluateSpeedwalk answer: one step per line, each ending CR LF, or an
    asterisk and the reason when the speedwalk didn't make sense."""
    if not directions:
        return "*Invalid speed walk string"
    return "".join(direction + "\r\n" for direction in directions)


def sha0_hex(data: bytes) -> str:
    """MUSHclient's Hash and utils.hash: the original Secure Hash Standard (SHA-0, the
    1993 one that SHA-1 corrected), as 40 hex digits. Packs may compare these with ones
    MUSHclient made, so it has to be SHA-0, not SHA-1."""
    state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    bit_length = len(data) * 8
    data = data + b"\x80" + b"\x00" * ((55 - len(data)) % 64) + struct.pack(">Q", bit_length)
    for offset in range(0, len(data), 64):
        words = list(struct.unpack(">16I", data[offset:offset + 64]))
        for index in range(16, 80):
            # SHA-1 rotates this left by one; SHA-0 doesn't.
            words.append(words[index - 3] ^ words[index - 8] ^ words[index - 14]
                         ^ words[index - 16])
        a, b, c, d, e = state
        for index in range(80):
            if index < 20:
                mixed, constant = (b & c) | (~b & d), 0x5A827999
            elif index < 40:
                mixed, constant = b ^ c ^ d, 0x6ED9EBA1
            elif index < 60:
                mixed, constant = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
            else:
                mixed, constant = b ^ c ^ d, 0xCA62C1D6
            rotated = ((a << 5) | (a >> 27)) & 0xFFFFFFFF
            total = (rotated + mixed + e + constant + words[index]) & 0xFFFFFFFF
            e, d, c, b, a = d, c, ((b << 30) | (b >> 2)) & 0xFFFFFFFF, a, total
        state = [(old + new) & 0xFFFFFFFF for old, new in zip(state, (a, b, c, d, e), strict=True)]
    return "".join(f"{word:08x}" for word in state)


# --- colours ---


def rgb(red: int, green: int, blue: int) -> int:
    """A colour as MUSHclient's scripts see it: a Windows COLORREF, red in the low byte."""
    return red | green << 8 | blue << 16


# mxp/mxpinit.cpp, as 0xRRGGBB. Where two names share a colour, the first one here is the
# name RGBColourToName gives.
_COLOUR_NAMES = (
    ("aliceblue", 0xF0F8FF), ("antiquewhite", 0xFAEBD7), ("aqua", 0x00FFFF),
    ("aquamarine", 0x7FFFD4), ("azure", 0xF0FFFF), ("beige", 0xF5F5DC), ("bisque", 0xFFE4C4),
    ("black", 0x000000), ("blanchedalmond", 0xFFEBCD), ("blue", 0x0000FF),
    ("blueviolet", 0x8A2BE2), ("brown", 0xA52A2A), ("burlywood", 0xDEB887),
    ("cadetblue", 0x5F9EA0), ("chartreuse", 0x7FFF00), ("chocolate", 0xD2691E),
    ("coral", 0xFF7F50), ("cornflowerblue", 0x6495ED), ("cornsilk", 0xFFF8DC),
    ("crimson", 0xDC143C), ("cyan", 0x00FFFF), ("darkblue", 0x00008B), ("darkcyan", 0x008B8B),
    ("darkgoldenrod", 0xB8860B), ("darkgray", 0xA9A9A9), ("darkgrey", 0xA9A9A9),
    ("darkgreen", 0x006400), ("darkkhaki", 0xBDB76B), ("darkmagenta", 0x8B008B),
    ("darkolivegreen", 0x556B2F), ("darkorange", 0xFF8C00), ("darkorchid", 0x9932CC),
    ("darkred", 0x8B0000), ("darksalmon", 0xE9967A), ("darkseagreen", 0x8FBC8F),
    ("darkslateblue", 0x483D8B), ("darkslategray", 0x2F4F4F), ("darkslategrey", 0x2F4F4F),
    ("darkturquoise", 0x00CED1), ("darkviolet", 0x9400D3), ("deeppink", 0xFF1493),
    ("deepskyblue", 0x00BFFF), ("dimgray", 0x696969), ("dimgrey", 0x696969),
    ("dodgerblue", 0x1E90FF), ("firebrick", 0xB22222), ("floralwhite", 0xFFFAF0),
    ("forestgreen", 0x228B22), ("fuchsia", 0xFF00FF), ("gainsboro", 0xDCDCDC),
    ("ghostwhite", 0xF8F8FF), ("gold", 0xFFD700), ("goldenrod", 0xDAA520), ("gray", 0x808080),
    ("grey", 0x808080), ("green", 0x008000), ("greenyellow", 0xADFF2F), ("honeydew", 0xF0FFF0),
    ("hotpink", 0xFF69B4), ("indianred", 0xCD5C5C), ("indigo", 0x4B0082), ("ivory", 0xFFFFF0),
    ("khaki", 0xF0E68C), ("lavender", 0xE6E6FA), ("lavenderblush", 0xFFF0F5),
    ("lawngreen", 0x7CFC00), ("lemonchiffon", 0xFFFACD), ("lightblue", 0xADD8E6),
    ("lightcoral", 0xF08080), ("lightcyan", 0xE0FFFF), ("lightgoldenrodyellow", 0xFAFAD2),
    ("lightgreen", 0x90EE90), ("lightgrey", 0xD3D3D3), ("lightgray", 0xD3D3D3),
    ("lightpink", 0xFFB6C1), ("lightsalmon", 0xFFA07A), ("lightseagreen", 0x20B2AA),
    ("lightskyblue", 0x87CEFA), ("lightslategray", 0x778899), ("lightslategrey", 0x778899),
    ("lightsteelblue", 0xB0C4DE), ("lightyellow", 0xFFFFE0), ("lime", 0x00FF00),
    ("limegreen", 0x32CD32), ("linen", 0xFAF0E6), ("magenta", 0xFF00FF), ("maroon", 0x800000),
    ("mediumaquamarine", 0x66CDAA), ("mediumblue", 0x0000CD), ("mediumorchid", 0xBA55D3),
    ("mediumpurple", 0x9370DB), ("mediumseagreen", 0x3CB371), ("mediumslateblue", 0x7B68EE),
    ("mediumspringgreen", 0x00FA9A), ("mediumturquoise", 0x48D1CC),
    ("mediumvioletred", 0xC71585), ("midnightblue", 0x191970), ("mintcream", 0xF5FFFA),
    ("mistyrose", 0xFFE4E1), ("moccasin", 0xFFE4B5), ("navajowhite", 0xFFDEAD),
    ("navy", 0x000080), ("oldlace", 0xFDF5E6), ("olive", 0x808000), ("olivedrab", 0x6B8E23),
    ("orange", 0xFFA500), ("orangered", 0xFF4500), ("orchid", 0xDA70D6),
    ("palegoldenrod", 0xEEE8AA), ("palegreen", 0x98FB98), ("paleturquoise", 0xAFEEEE),
    ("palevioletred", 0xDB7093), ("papayawhip", 0xFFEFD5), ("peachpuff", 0xFFDAB9),
    ("peru", 0xCD853F), ("pink", 0xFFC0CB), ("plum", 0xDDA0DD), ("powderblue", 0xB0E0E6),
    ("purple", 0x800080), ("rebeccapurple", 0x663399), ("red", 0xFF0000),
    ("rosybrown", 0xBC8F8F), ("royalblue", 0x4169E1), ("saddlebrown", 0x8B4513),
    ("salmon", 0xFA8072), ("sandybrown", 0xF4A460), ("seagreen", 0x2E8B57),
    ("seashell", 0xFFF5EE), ("sienna", 0xA0522D), ("silver", 0xC0C0C0), ("skyblue", 0x87CEEB),
    ("slateblue", 0x6A5ACD), ("slategray", 0x708090), ("slategrey", 0x708090),
    ("snow", 0xFFFAFA), ("springgreen", 0x00FF7F), ("steelblue", 0x4682B4), ("tan", 0xD2B48C),
    ("teal", 0x008080), ("thistle", 0xD8BFD8), ("tomato", 0xFF6347), ("turquoise", 0x40E0D0),
    ("violet", 0xEE82EE), ("wheat", 0xF5DEB3), ("white", 0xFFFFFF), ("whitesmoke", 0xF5F5F5),
    ("yellow", 0xFFFF00), ("yellowgreen", 0x9ACD32),
)


def _from_hex(value: int) -> int:
    return rgb(value >> 16 & 0xFF, value >> 8 & 0xFF, value & 0xFF)


COLOUR_BY_NAME: dict[str, int] = {name: _from_hex(value) for name, value in _COLOUR_NAMES}
_NAME_BY_COLOUR: dict[int, str] = {}
for _name, _value in _COLOUR_NAMES:
    _NAME_BY_COLOUR.setdefault(_from_hex(_value), _name)

# MUSHclient's default ANSI colours, normal then bold: what GetNormalColour and
# GetBoldColour answer for 1 to 8, and what the MUD's colour codes are drawn in.
ANSI_NORMAL = (
    rgb(0, 0, 0), rgb(128, 0, 0), rgb(0, 128, 0), rgb(128, 128, 0),
    rgb(0, 0, 128), rgb(128, 0, 128), rgb(0, 128, 128), rgb(192, 192, 192),
)
ANSI_BOLD = (
    rgb(128, 128, 128), rgb(255, 0, 0), rgb(0, 255, 0), rgb(255, 255, 0),
    rgb(0, 0, 255), rgb(255, 0, 255), rgb(0, 255, 255), rgb(255, 255, 255),
)
_ANSI_INDEX = {
    "black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6,
    "white": 7,
}
_DEFAULT_TEXT = 7  # the MUD's text with no colour code: silver, or white when bold
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_HEX_COLOUR = re.compile(r"#([0-9A-Fa-f]{6})")


def colour_to_name(colour: object) -> str:
    """MUSHclient's RGBColourToName: the colour's name, or #RRGGBB when it hasn't one.

    Anything that isn't a number answers "" instead of raising: packs pass it fields they
    expect a colour in, and an error there can cost the line the pack was reprinting.
    """
    if isinstance(colour, bool) or not isinstance(colour, (int, float)):
        return ""
    value = int(colour) & 0xFFFFFF
    name = _NAME_BY_COLOUR.get(value)
    if name is not None:
        return name
    return f"#{value & 0xFF:02X}{value >> 8 & 0xFF:02X}{value >> 16 & 0xFF:02X}"


def colour_from_name(name: object) -> int:
    """MUSHclient's ColourNameToRGB: a colour name or #RRGGBB as a colour, -1 if neither."""
    text = str(name or "").strip().lower()
    found = _HEX_COLOUR.fullmatch(text)
    if found is not None:
        return _from_hex(int(found.group(1), 16))
    return COLOUR_BY_NAME.get(text, -1)


def _xterm_colour(number: int) -> int:
    if number < 8:
        return ANSI_NORMAL[number]
    if number < 16:
        return ANSI_BOLD[number - 8]
    if number < 232:
        number -= 16
        return rgb(
            _CUBE_LEVELS[number // 36], _CUBE_LEVELS[number // 6 % 6], _CUBE_LEVELS[number % 6]
        )
    level = 8 + 10 * (number - 232)
    return rgb(level, level, level)


def ansi_colour(name: str | None, *, bold: bool, foreground: bool) -> int:
    """A colour from genericMud's ANSI parser ("red", "bright_cyan", "256:N", "rgb:r,g,b",
    or None for the default) as the colour MUSHclient would have drawn it in."""
    if name is None:
        if not foreground:
            return ANSI_NORMAL[0]
        return (ANSI_BOLD if bold else ANSI_NORMAL)[_DEFAULT_TEXT]
    if name.startswith("256:"):
        try:
            return _xterm_colour(max(0, min(255, int(name[4:]))))
        except ValueError:
            return ANSI_NORMAL[_DEFAULT_TEXT]
    if name.startswith("rgb:"):
        try:
            red, green, blue = (max(0, min(255, int(part))) for part in name[4:].split(","))
        except ValueError:
            return ANSI_NORMAL[_DEFAULT_TEXT]
        return rgb(red, green, blue)
    bright = name.startswith("bright_")
    index = _ANSI_INDEX.get(name.removeprefix("bright_"), _DEFAULT_TEXT)
    return (ANSI_BOLD if bright or (bold and foreground) else ANSI_NORMAL)[index]


def style_runs(text: str, spans: list) -> list[dict[str, object]]:
    """The line's colour runs the way MUSHclient hands them to trigger scripts.

    A line always has at least one, so a pack can read ``styles[#styles]`` safely.
    """
    runs: list[dict[str, object]] = []
    for span in spans:
        if not span.text:
            continue
        runs.append({
            "text": span.text,
            "length": len(span.text),
            "textcolour": ansi_colour(span.fg, bold=span.bold, foreground=True),
            "backcolour": ansi_colour(span.bg, bold=False, foreground=False),
            "style": 1 if span.bold else 0,
        })
    if not runs:
        runs.append({
            "text": text,
            "length": len(text),
            "textcolour": ANSI_NORMAL[_DEFAULT_TEXT],
            "backcolour": ANSI_NORMAL[0],
            "style": 0,
        })
    return runs


# --- trigger and alias records ---

TRIGGER = "trigger"
ALIAS = "alias"
_SEND_TO_LAST = 14  # script after omit; MUSHclient's send_to runs from 0 to here
_LONG = (-(2**31), 2**31 - 1)

# Option names as packs pass them to Get/SetTriggerOption and Get/SetAliasOption, from
# MUSHclient's option tables. Numbers carry their allowed range.
_NUMBERS = {
    TRIGGER: {
        "clipboard_arg": (0, 10), "colour_change_type": (0, 2), "custom_colour": (0, 17),
        "lines_to_match": (0, 200), "match_style": (0, 0xFFFF), "new_style": (0, 7),
        "other_text_colour": (0, 0xFFFFFF), "other_back_colour": (0, 0xFFFFFF),
        "send_to": (0, _SEND_TO_LAST), "sequence": (0, 10000), "user": _LONG,
    },
    ALIAS: {"send_to": (0, _SEND_TO_LAST), "sequence": (0, 10000), "user": _LONG},
}
_FLAGS = {
    TRIGGER: (
        "enabled", "expand_variables", "ignore_case", "keep_evaluating", "multi_line",
        "omit_from_log", "omit_from_output", "regexp", "repeat", "sound_if_inactive",
        "lowercase_wildcard", "temporary", "one_shot",
    ),
    ALIAS: (
        "enabled", "expand_variables", "ignore_case", "omit_from_log",
        "omit_from_command_history", "omit_from_output", "regexp", "menu", "keep_evaluating",
        "echo_alias", "temporary", "one_shot",
    ),
}
_TEXTS = {
    TRIGGER: ("group", "match", "script", "sound", "send", "variable"),
    ALIAS: ("group", "match", "script", "send", "variable"),
}
# Fixed once the rule exists: changing them would mean compiling a different rule.
_READ_ONLY = frozenset({"ignore_case", "regexp"})


@dataclass(eq=False)
class PackRule:
    """One MUSHclient trigger or alias, with every option a pack can read or set."""

    kind: str  # TRIGGER or ALIAS
    name: str  # as the pack wrote it; "" when it gave none
    plugin: str  # the plugin it belongs to, or "world"
    match: str
    send: str = ""
    send_to: int = 0
    script: str = ""
    sound: str = ""
    group: str = ""
    variable: str = ""
    enabled: bool = False
    regexp: bool = False
    ignore_case: bool = False
    keep_evaluating: bool = False
    omit_from_output: bool = False
    omit_from_log: bool = False
    expand_variables: bool = False
    lowercase_wildcard: bool = False
    one_shot: bool = False
    temporary: bool = False
    sequence: int = 100
    user: int = 0
    extra: dict[str, int] = field(default_factory=dict)  # options kept for packs to read back
    label: str = ""  # how packs address it: the name in lower case, or "*trigger12"
    # What the engine and the scripts have made of it.
    engine_rule: object | None = None
    engine_name: str = ""
    pattern: str = ""  # the regular expression it is compiled to now
    handler: object | None = None  # its script function, once found
    compiled: object | None = None  # its send text compiled as Lua, for the text below
    compiled_text: str | None = None
    matched: int = 0
    invocations: int = 0
    last_matched: float = 0.0
    last_text: str = ""
    deleted: bool = False

    def definition(self) -> tuple:
        """What makes two rules the same rule: everything a pack set."""
        return (
            self.kind, self.match, self.send, self.send_to, self.script, self.sound, self.group,
            self.variable, self.enabled, self.regexp, self.ignore_case, self.keep_evaluating,
            self.omit_from_output, self.omit_from_log, self.expand_variables,
            self.lowercase_wildcard, self.one_shot, self.temporary, self.sequence, self.user,
            tuple(sorted(self.extra.items())),
        )


def known_option(kind: str, name: str) -> bool:
    return name in _NUMBERS[kind] or name in _FLAGS[kind] or name in _TEXTS[kind]


def get_option(rule: PackRule, name: str) -> object:
    """Get(Trigger|Alias)Option: numbers and yes/no options as numbers, text as text, and
    nil for a name MUSHclient doesn't have."""
    name = name.strip().lower()
    if name in _TEXTS[rule.kind]:
        return str(getattr(rule, name))
    if name in _NUMBERS[rule.kind] or name in _FLAGS[rule.kind]:
        value = getattr(rule, name) if hasattr(rule, name) else rule.extra.get(name, 0)
        return int(value)
    return None


def option_number(value: object) -> str:
    """An option value from Lua as MUSHclient receives it: true and false become y and n,
    and a number is its text."""
    if isinstance(value, bool):
        return "y" if value else "n"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def parse_option(kind: str, name: str, value: object) -> tuple[int, object]:
    """Check one Set(Trigger|Alias)Option value; (return code, the value to store)."""
    name = name.strip().lower()
    text = option_number(value)
    if name in _TEXTS[kind]:
        return E_OK, text
    if name not in _NUMBERS[kind] and name not in _FLAGS[kind]:
        return E_UNKNOWN_OPTION, None
    if name in _READ_ONLY:
        return E_PLUGIN_CANNOT_SET_OPTION, None
    if name in _FLAGS[kind] and text in ("y", "Y", "n", "N"):
        text = "1" if text in ("y", "Y") else "0"
    if not re.fullmatch(r"[+-]?\d+", text.strip()):
        return E_OPTION_OUT_OF_RANGE, None
    number = int(text)
    if name in _FLAGS[kind]:
        return E_OK, number != 0
    low, high = _NUMBERS[kind][name]
    if not low <= number <= high:
        return E_OPTION_OUT_OF_RANGE, None
    return E_OK, number


def set_option(rule: PackRule, name: str, value: object) -> None:
    """Store an option that parse_option accepted."""
    name = name.strip().lower()
    if hasattr(rule, name) and name not in ("extra", "label"):
        setattr(rule, name, value)
    else:
        rule.extra[name] = int(value)


def _info_value(rule: PackRule, key: str) -> object:
    if key.startswith("extra:"):
        return rule.extra.get(key[6:], 0)
    return getattr(rule, key)


# Get(Trigger|Alias)Info numbers -> (field, how MUSHclient hands it to Lua).
_TRIGGER_INFO = {
    1: ("match", str), 2: ("send", str), 3: ("sound", str), 4: ("script", str),
    5: ("omit_from_log", bool), 6: ("omit_from_output", bool), 7: ("keep_evaluating", bool),
    8: ("enabled", bool), 9: ("regexp", bool), 10: ("ignore_case", bool),
    11: ("extra:repeat", bool), 12: ("extra:sound_if_inactive", bool),
    13: ("expand_variables", bool), 14: ("extra:clipboard_arg", int), 15: ("send_to", int),
    16: ("sequence", int), 17: ("extra:match_style", int), 18: ("extra:new_style", int),
    19: ("extra:custom_colour", int), 20: ("invocations", int), 21: ("matched", int),
    22: ("last_matched", float), 23: ("temporary", bool), 24: ("included", bool),
    25: ("lowercase_wildcard", bool), 26: ("group", str), 27: ("variable", str),
    28: ("user", int), 29: ("extra:other_text_colour", int),
    30: ("extra:other_back_colour", int), 32: ("last_text", str), 33: ("executing", bool),
    34: ("script_valid", bool), 35: ("pcre_error", int), 36: ("one_shot", bool),
}
_ALIAS_INFO = {
    1: ("match", str), 2: ("send", str), 3: ("script", str), 4: ("omit_from_log", bool),
    5: ("omit_from_output", bool), 6: ("enabled", bool), 7: ("regexp", bool),
    8: ("ignore_case", bool), 9: ("expand_variables", bool), 10: ("invocations", int),
    11: ("matched", int), 12: ("extra:menu", int), 13: ("last_matched", float),
    14: ("temporary", bool), 15: ("included", bool), 16: ("group", str),
    17: ("variable", str), 18: ("send_to", int), 19: ("keep_evaluating", bool),
    20: ("sequence", int), 21: ("extra:echo_alias", bool),
    22: ("extra:omit_from_command_history", bool), 23: ("user", int),
    25: ("last_text", str), 26: ("executing", bool), 27: ("script_valid", bool),
    28: ("pcre_error", int), 29: ("one_shot", bool),
}


def rule_info(rule: PackRule, number: int, *, script_valid: bool) -> object:
    """Get(Trigger|Alias)Info for one number; nil for a number it doesn't have."""
    table = _TRIGGER_INFO if rule.kind == TRIGGER else _ALIAS_INFO
    entry = table.get(number)
    if entry is None:
        return None
    key, kind = entry
    if key == "included":
        value: object = False
    elif key == "executing":
        value = False
    elif key == "script_valid":
        value = script_valid
    elif key == "pcre_error":
        value = 0
    else:
        value = _info_value(rule, key)
    if kind is bool:
        return bool(value)
    if kind is int:
        return int(value)
    if kind is float:
        return float(value) if value else None
    return str(value)
