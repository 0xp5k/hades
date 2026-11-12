#!/usr/bin/env python3
"""Real parsers from Python's standard library, each wrapped so that bytes go in and one outcome comes out.

  python3 targets.py                      list the targets
  python3 targets.py <target> <hex>       try one input and print what happened

A parser given bad input is allowed to refuse it with the errors listed for that target (its own error class, and the
ValueError family, which includes UnicodeDecodeError). A DEFECT is anything else: an IndexError, KeyError, TypeError,
AttributeError, struct.error, RecursionError, AssertionError and so on escaping from the parser, or the parser still
running after 5 seconds. A defect's label is "<target>: <ErrorType> in <function>", naming the innermost standard-library
function the error came from; inputs with the same label are the same defect.

Inputs are at most 4096 bytes. Nothing here writes files, opens the network or executes the input.
"""
import io
import os
import signal
import sys
import sysconfig
import traceback
import warnings

LIMIT = 4096
SECONDS = 5
STDLIB = os.path.realpath(sysconfig.get_paths()["stdlib"])


def text(data):
    return data.decode("utf-8", "surrogateescape")


def two(data):
    """Targets that take two strings: the input is split at the first zero byte."""
    a, _, b = data.partition(b"\x00")
    return text(a), text(b)


def t_plistlib(data):
    import plistlib
    plistlib.loads(data)


def t_tarfile(data):
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for n, member in enumerate(tar):
            if n > 50:
                break
            if member.isfile():
                tar.extractfile(member).read(1 << 16)


def t_zipfile(data):
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist()[:20]:
            with z.open(info) as f:
                f.read(1 << 20)


def t_email(data):
    import email
    import email.policy
    msg = email.message_from_bytes(data, policy=email.policy.default)
    for part in msg.walk():
        for name in part.keys():
            part.get_all(name)
        part.get_content_type(), part.get_filename(), part.get_content_charset(), part.get_boundary()
    msg.as_bytes()


def t_addresses(data):
    import email.utils
    email.utils.getaddresses([text(data)])
    email.utils.parseaddr(text(data))


def t_maildate(data):
    import email.utils
    email.utils.parsedate_to_datetime(text(data))


def t_gettext(data):
    import gettext
    t = gettext.GNUTranslations(io.BytesIO(data))
    t.gettext("a"), t.ngettext("a", "b", 2)


def t_wave(data):
    import wave
    with wave.open(io.BytesIO(data)) as w:
        w.getparams()
        w.readframes(64)


def t_zoneinfo(data):
    import datetime
    from zoneinfo import _zoneinfo  # the pure-Python reader, not the C one
    zone = _zoneinfo.ZoneInfo.from_file(io.BytesIO(data), key="fuzz")
    for year in (1900, 1970, 2000, 2038, 2500):
        when = datetime.datetime(year, 6, 1, tzinfo=zone)
        when.utcoffset(), when.tzname(), when.dst()


def t_tomllib(data):
    import tomllib
    tomllib.loads(text(data))


def t_configparser(data):
    import configparser
    parser = configparser.ConfigParser()
    parser.read_string(text(data))
    for section in parser.sections():
        for key in parser[section]:
            parser[section].get(key)


def t_htmlparser(data):
    from html.parser import HTMLParser
    parser = HTMLParser()
    parser.feed(text(data))
    parser.close()


def t_cookies(data):
    from http import cookies
    jar = cookies.SimpleCookie()
    jar.load(text(data))
    jar.output()


def t_cookiejar(data):
    from http import cookiejar
    cookiejar.parse_ns_headers([text(data)])
    cookiejar.split_header_words([text(data)])
    cookiejar.http2time(text(data)), cookiejar.iso2time(text(data))


def t_httpheaders(data):
    import http.client
    message = http.client.parse_headers(io.BytesIO(data))
    message.get_content_type(), message.get_params(), message.get_filename()


def t_tokenize(data):
    import tokenize
    for n, _ in enumerate(tokenize.tokenize(io.BytesIO(data).readline)):
        if n > 5000:
            break


def t_regex(data):
    import re
    re.compile(text(data))  # compiled only, never run


def t_strptime(data):
    import _strptime
    value, form = two(data)
    _strptime._strptime_datetime(__import__("datetime").datetime, value, form)


def t_punycode(data):
    data.decode("punycode")


def t_idna(data):
    data.decode("idna")
    text(data).encode("idna")


def t_ascii85(data):
    import base64
    base64.a85decode(data)
    base64.b85decode(data)


def t_pickletools(data):
    import pickletools
    for n, _ in enumerate(pickletools.genops(data)):  # decodes the opcodes only; nothing is unpickled
        if n > 5000:
            break


def t_urlparse(data):
    import urllib.parse
    parts = urllib.parse.urlsplit(text(data))
    parts.hostname, parts.port, parts.username
    urllib.parse.parse_qs(text(data))


def t_fraction(data):
    import fractions
    fractions.Fraction(text(data))


def t_shlex(data):
    import shlex
    shlex.split(text(data))


def expected():
    """What each parser may raise for bad input without it being a defect. Imports are inside so that listing is cheap."""
    import configparser, email.errors, http.client, http.cookies, pickle, plistlib, re, tarfile, tokenize, wave, zipfile, zlib
    from xml.parsers.expat import ExpatError
    return {
        "plistlib": (plistlib.InvalidFileException, ExpatError),
        "tarfile": (tarfile.TarError, EOFError),
        "zipfile": (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError),
        "email": (email.errors.MessageError,),
        "addresses": (),
        "maildate": (),
        "gettext": (OSError,),
        "wave": (wave.Error, EOFError),
        "zoneinfo": (),
        "tomllib": (),
        "configparser": (configparser.Error,),
        "htmlparser": (),
        "cookies": (http.cookies.CookieError,),
        "cookiejar": (),
        "httpheaders": (http.client.HTTPException,),
        "tokenize": (tokenize.TokenError, SyntaxError),
        "regex": (re.error,),
        "strptime": (),
        "punycode": (),
        "idna": (),
        "ascii85": (),
        "pickletools": (pickle.PickleError,),
        "urlparse": (),
        "fraction": (ZeroDivisionError,),
        "shlex": (),
    }


TARGETS = {name[2:]: fn for name, fn in sorted(globals().items()) if name.startswith("t_")}


class TimedOut(BaseException):
    pass


def check(target, data):
    """Returns (label, outcome). label is None unless the input shows a defect; outcome says in words what happened."""
    if target not in TARGETS:
        return None, f"unknown target; choose one of: {', '.join(TARGETS)}"
    if len(data) > LIMIT:
        return None, f"input is larger than {LIMIT} bytes"
    allowed = expected()[target] + (ValueError,)

    def alarm(*_):
        raise TimedOut()
    warnings.simplefilter("ignore")  # some parsers warn about odd input on standard error; that is not an outcome
    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(SECONDS)
    try:
        TARGETS[target](data)
        return None, "accepted"
    except TimedOut:
        return f"{target}: still running after {SECONDS} seconds", "defect"
    except allowed as error:
        return None, f"refused as expected ({type(error).__name__})"
    except RecursionError as error:
        frames = [f.name for f in traceback.extract_tb(error.__traceback__) if os.path.realpath(f.filename).startswith(STDLIB)]
        where = max(set(frames), key=frames.count) if frames else "?"
        return f"{target}: RecursionError in {where}", "defect"
    except Exception as error:
        frames = [f.name for f in traceback.extract_tb(error.__traceback__) if os.path.realpath(f.filename).startswith(STDLIB)]
        kind = type(error).__name__ if type(error).__module__ == "builtins" else f"{type(error).__module__}.{type(error).__name__}"
        return f"{target}: {kind} in {frames[-1] if frames else '?'}", "defect"
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("targets: " + ", ".join(TARGETS))
        sys.exit(0 if len(sys.argv) == 1 else 2)
    try:
        label, outcome = check(sys.argv[1], bytes.fromhex(sys.argv[2]))
    except ValueError:
        sys.exit("the second argument must be hex")
    print(label or outcome)
